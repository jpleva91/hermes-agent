"""Rework-program P0: archive-as-precondition workspace cleanup.

The engine used to ``rmtree`` scratch workspaces on completion (87% of cited
evidence paths destroyed). These tests pin the new contract:

* completion archives a verified manifest + tarball BEFORE deletion;
* archive failure preserves the workspace (fail-closed);
* ``HERMES_ARCHIVE_ON_COMPLETE=0`` restores legacy deletion (kill-switch);
* a workspace shared with another live task is never deleted from under it.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import mission_artifacts as ma
from hermes_cli import mission_guardrail_policy as mgp
from tests.mission_policy_fixtures import write_mission_policy


@pytest.fixture(autouse=True)
def _reset_mission_policy_cache():
    mgp.clear_cache()
    yield
    mgp.clear_cache()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB and the mission policy."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    write_mission_policy(home)
    return home


def _make_scratch_task(conn, home: Path, *, files: dict[str, str] | None = None) -> tuple[str, Path]:
    """Create a running task with a managed scratch workspace containing files."""
    board = kb.get_current_board() or kb.DEFAULT_BOARD
    task_id = kb.create_task(conn, title="p0 archival probe", initial_status="running")
    ws = kb.boards_root() / board / "workspaces" / task_id
    ws.mkdir(parents=True, exist_ok=True)
    default_files = {"evidence.txt": "verdict evidence\n"}
    for name, content in (default_files if files is None else files).items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    conn.execute(
        "UPDATE tasks SET workspace_kind = 'scratch', workspace_path = ? WHERE id = ?",
        (str(ws), task_id),
    )
    conn.commit()
    return task_id, ws


def _archive_dir_for(home: Path, task_id: str) -> Path | None:
    root = home / "hermes-runtime" / "mission-artifacts"
    hits = list(root.rglob(f"{task_id}/{ma.MANIFEST_NAME}"))
    return hits[0].parent if hits else None


def test_complete_archives_then_deletes(kanban_home):
    with kb.connect() as conn:
        task_id, ws = _make_scratch_task(
            conn, kanban_home, files={"evidence.txt": "raw output\n", "sub/patch.diff": "diff\n"}
        )
        assert kb.complete_task(conn, task_id, result="done")
    assert not ws.exists(), "workspace should be deleted after verified archive"
    dest = _archive_dir_for(kanban_home, task_id)
    assert dest is not None, "manifest.json must exist under mission-artifacts"
    manifest = json.loads((dest / ma.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["task_id"] == task_id
    assert manifest["file_count"] == 2
    by_path = {f["path"]: f for f in manifest["files"]}
    assert by_path["evidence.txt"]["sha256"] == hashlib.sha256(b"raw output\n").hexdigest()
    tar_path = dest / ma.TARBALL_NAME
    assert tar_path.exists()
    assert manifest["tarball"]["sha256"] == hashlib.sha256(tar_path.read_bytes()).hexdigest()
    with tarfile.open(tar_path, "r:gz") as tf:
        assert sorted(m.name for m in tf.getmembers() if m.isfile()) == ["evidence.txt", "sub/patch.diff"]


def test_archive_failure_preserves_workspace(kanban_home, monkeypatch):
    # Point the archive root at a regular FILE so mkdir(parents=True) fails.
    blocker = kanban_home / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(ma.ARCHIVE_ROOT_ENV, str(blocker / "nested"))
    with kb.connect() as conn:
        task_id, ws = _make_scratch_task(conn, kanban_home)
        assert kb.complete_task(conn, task_id, result="done")
    assert ws.exists(), "fail-closed: archive failure must preserve the workspace"
    assert (ws / "evidence.txt").exists()


def test_kill_switch_restores_legacy_delete(kanban_home, monkeypatch):
    monkeypatch.setenv(ma.ARCHIVE_ENV_KILL, "0")
    with kb.connect() as conn:
        task_id, ws = _make_scratch_task(conn, kanban_home)
        assert kb.complete_task(conn, task_id, result="done")
    assert not ws.exists(), "kill-switch: legacy deletion without archive"
    assert _archive_dir_for(kanban_home, task_id) is None, "no archive when disabled"


def test_shared_workspace_deferred_until_last_referent(kanban_home):
    with kb.connect() as conn:
        first_id, ws = _make_scratch_task(conn, kanban_home)
        # Second live task pinned to the SAME workspace path (lineage pattern).
        second_id = kb.create_task(conn, title="p0 sibling probe", initial_status="running")
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'scratch', workspace_path = ? WHERE id = ?",
            (str(ws), second_id),
        )
        conn.commit()
        assert kb.complete_task(conn, first_id, result="done")
        assert ws.exists(), "shared workspace must survive while a referent is live"
        assert kb.complete_task(conn, second_id, result="done")
    assert not ws.exists(), "last referent's completion performs the delete"
    # Archived under the LAST completer (the task whose completion licensed delete).
    assert _archive_dir_for(kanban_home, second_id) is not None


def test_empty_workspace_manifest_only(kanban_home):
    with kb.connect() as conn:
        task_id, ws = _make_scratch_task(conn, kanban_home, files={})
        assert kb.complete_task(conn, task_id, result="done")
    assert not ws.exists()
    dest = _archive_dir_for(kanban_home, task_id)
    assert dest is not None
    manifest = json.loads((dest / ma.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["file_count"] == 0
    assert manifest["tarball"] is None
    assert not (dest / ma.TARBALL_NAME).exists()
