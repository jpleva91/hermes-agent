from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from scripts import mission_engine_reactive_sweep as sweep
from scripts import mission_closeout_digest as digest


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(board=sweep.BOARD)
    return home


def _conn():
    return kb.connect(board=sweep.BOARD)


def _mk_origin(conn, *, title="Implement feature X", assignee="runtimesteward"):
    return kb.create_task(
        conn, title=title, body="the real mission work", assignee=assignee,
        created_by="missioncommander", board=sweep.BOARD,
    )


def _mk_done_gate(conn, target_id, *, verdict="APPROVE"):
    gate_id = kb.create_task(
        conn, title="Ready gate: review feature X", body=f"Review target `{target_id}`",
        assignee="gatewarden", created_by="missioncommander", board=sweep.BOARD,
    )
    kb.complete_task(conn, gate_id, summary=f"{verdict}: raw evidence checked",
                     metadata={"verdict": verdict, "target_task": target_id})
    return gate_id


def test_detects_reviewed_mission_origin_closeout(kanban_home):
    with _conn() as conn:
        build_id = _mk_origin(conn)
        _mk_done_gate(conn, build_id)              # it went through a gate
        kb.complete_task(conn, build_id, summary="done")

        closeouts = digest.detect_closeouts(conn, set())
        assert [r["id"] for r in closeouts] == [build_id]
        text = digest.compose_digest(conn, kb_get_row(conn, build_id))
        assert build_id in text and "Closeout Digest" in text


def test_ignores_gate_and_cure_subwork(kanban_home):
    with _conn() as conn:
        build_id = _mk_origin(conn)
        gate_id = _mk_done_gate(conn, build_id)
        kb.complete_task(conn, build_id, summary="done")
        cure_id = kb.create_task(conn, title="Cure: finish feature X", body="cure work",
                                 assignee="runtimesteward", created_by="missioncommander", board=sweep.BOARD)
        kb.complete_task(conn, cure_id, summary="done")

        ids = {r["id"] for r in digest.detect_closeouts(conn, set())}
        assert gate_id not in ids           # gate card is sub-work
        assert cure_id not in ids           # cure card is sub-work
        assert build_id in ids


def test_ignores_unreviewed_done_card(kanban_home):
    with _conn() as conn:
        build_id = _mk_origin(conn)         # no gate ever references it
        kb.complete_task(conn, build_id, summary="done")
        assert digest.detect_closeouts(conn, set()) == []


def test_init_seeds_baseline_and_suppresses_history(kanban_home):
    with _conn() as conn:
        build_id = _mk_origin(conn)
        _mk_done_gate(conn, build_id)
        kb.complete_task(conn, build_id, summary="done")

    assert digest.main(["--init"]) == 0      # seed baseline
    state = digest._load_state()
    assert build_id in state["digested"] and state["initialized"] is True

    with _conn() as conn:                    # already digested -> not re-detected
        assert digest.detect_closeouts(conn, set(state["digested"])) == []


def test_dry_run_reports_without_delivering(kanban_home, monkeypatch):
    sent = []
    monkeypatch.setattr(digest, "_deliver", lambda text, target: sent.append((text, target)) or {"success": True})
    with _conn() as conn:
        build_id = _mk_origin(conn)
        _mk_done_gate(conn, build_id)
        kb.complete_task(conn, build_id, summary="done")

    assert digest.main([]) == 0              # dry-run (no --send)
    assert sent == []                        # never delivered
    assert digest._load_state()["digested"] == []   # dry-run does not advance state


def kb_get_row(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
