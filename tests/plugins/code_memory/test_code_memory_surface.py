"""Tests for explicit Code Memory v0 CLI/tool surfaces and forget safety."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from plugins.code_memory import register
from plugins.code_memory.ingest import CodeMemoryPolicy
from plugins.code_memory.surface import (
    code_memory_forget,
    code_memory_ingest,
    code_memory_search,
    code_memory_status,
)
from plugins.code_memory.storage import CodeMemoryStorage, get_index_db_path


@pytest.fixture
def hermes_profile_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "profiles" / "poscoding"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "poscoding")
    return home


def init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "fixture-repo"
    root.mkdir()
    init_git_repo(root)
    (root / "pyproject.toml").write_text('[project]\nname = "hermes-agent"\n', encoding="utf-8")
    (root / "plugins" / "code_memory").mkdir(parents=True)
    (root / "README.md").write_text("# Fixture\n\nneedle docs\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def needle():\n    return 'haystack'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)
    return root


def _allowed_policy(root: Path) -> dict[str, Any]:
    return CodeMemoryPolicy(allowed_workspace_roots=(str(root),)).public_dict()


def _table_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def test_plugin_registers_explicit_cli_and_tool_contracts() -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.cli_commands: dict[str, Any] = {}
            self.tools: dict[str, Any] = {}

        def register_cli_command(self, **kwargs: Any) -> None:
            self.cli_commands[kwargs["name"]] = kwargs

        def register_tool(self, **kwargs: Any) -> None:
            self.tools[kwargs["name"]] = kwargs

    ctx = FakeContext()
    register(ctx)

    assert "code-memory" in ctx.cli_commands
    assert callable(ctx.cli_commands["code-memory"]["setup_fn"])
    assert callable(ctx.cli_commands["code-memory"]["handler_fn"])
    assert set(ctx.tools) == {"code_memory_ingest", "code_memory_status", "code_memory_search", "code_memory_forget"}
    for name, tool in ctx.tools.items():
        assert tool["toolset"] == "code_memory"
        assert tool["schema"]["name"] == name
        assert tool["schema"]["parameters"]["type"] == "object"
        assert callable(tool["handler"])


def test_explicit_operations_return_json_compatible_bounded_summaries(
    hermes_profile_home: Path, fixture_repo: Path
) -> None:
    ingest = code_memory_ingest(workspace=str(fixture_repo), policy=_allowed_policy(fixture_repo))

    assert ingest["success"] is True
    assert ingest["operation"] == "code_memory_ingest"
    assert ingest["summary"].startswith("Code Memory ingest indexed")
    assert "workspace_id" in ingest
    assert json.loads(json.dumps(ingest)) == ingest

    status = code_memory_status(workspace_id=ingest["workspace_id"])
    assert status["success"] is True
    assert status["operation"] == "code_memory_status"
    assert status["counts"]["active_files"] == 3
    assert len(status["summary"]) < 240

    search = code_memory_search(workspace_id=ingest["workspace_id"], query="needle", limit=5, max_chars=200)
    assert search["success"] is True
    assert search["operation"] == "code_memory_search"
    assert search["counts"]["returned"] >= 1
    assert search["budget"]["used_chars"] <= 200
    assert all("content" not in item for item in search["results"]), "summary results must not leak full snippets"
    assert "context_pack" in search


def test_forget_path_dry_run_reports_exact_rows_chunks_files_without_deleting(
    hermes_profile_home: Path, fixture_repo: Path
) -> None:
    ingest = code_memory_ingest(workspace=str(fixture_repo), policy=_allowed_policy(fixture_repo))
    workspace_id = ingest["workspace_id"]
    db_path = get_index_db_path(workspace_id)

    dry_run = code_memory_forget(workspace_id=workspace_id, scope="path", path="src", dry_run=True)

    assert dry_run["success"] is True
    assert dry_run["dry_run"] is True
    assert dry_run["scope"] == "path"
    assert dry_run["target"]["path"] == "src"
    assert dry_run["would_delete"] == {"source_files": 1, "chunks": 1, "chunks_fts": 1, "files": 1}
    assert dry_run["matched_files"] == ["src/app.py"]
    assert _table_count(db_path, "source_files") == 3
    assert _table_count(db_path, "chunks") == 3
    assert _table_count(db_path, "chunks_fts") == 3


def test_forget_path_requires_approval_and_verifies_search_removed(
    hermes_profile_home: Path, fixture_repo: Path
) -> None:
    ingest = code_memory_ingest(workspace=str(fixture_repo), policy=_allowed_policy(fixture_repo))
    workspace_id = ingest["workspace_id"]

    denied = code_memory_forget(workspace_id=workspace_id, scope="path", path="src", dry_run=False, approved=False)
    assert denied["success"] is False
    assert denied["error"]["code"] == "approval_required"

    deleted = code_memory_forget(
        workspace_id=workspace_id,
        scope="path",
        path="src",
        dry_run=False,
        approved=True,
        verify_query="needle",
    )

    assert deleted["success"] is True
    assert deleted["deleted"] == {"source_files": 1, "chunks": 1, "chunks_fts": 1, "files": 1}
    assert deleted["verification"]["status"]["counts"]["active_files"] == 2
    assert deleted["verification"]["search"]["counts"]["returned"] == 1
    assert deleted["verification"]["path_search"]["counts"]["returned"] == 0


def test_forget_workspace_delete_is_profile_scoped(
    hermes_profile_home: Path, fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingest = code_memory_ingest(workspace=str(fixture_repo), policy=_allowed_policy(fixture_repo))
    workspace_id = ingest["workspace_id"]
    original_db = get_index_db_path(workspace_id)

    other_home = tmp_path / "profiles" / "other"
    other_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    monkeypatch.setenv("HERMES_PROFILE", "other")
    other_storage = CodeMemoryStorage()
    other = other_storage.initialize_workspace(fixture_repo, profile_name="other", config=_allowed_policy(fixture_repo))
    assert other["success"] is True
    other_db = Path(other["db_path"])

    monkeypatch.setenv("HERMES_HOME", str(hermes_profile_home))
    monkeypatch.setenv("HERMES_PROFILE", "poscoding")
    deleted = code_memory_forget(workspace_id=workspace_id, scope="workspace", dry_run=False, approved=True)

    assert deleted["success"] is True
    assert deleted["deleted"]["source_files"] == 3
    assert not original_db.exists()
    assert other_db.exists(), "workspace delete must not affect another profile's index"


def test_forget_invalid_path_fails_closed(hermes_profile_home: Path, fixture_repo: Path) -> None:
    ingest = code_memory_ingest(workspace=str(fixture_repo), policy=_allowed_policy(fixture_repo))

    result = code_memory_forget(workspace_id=ingest["workspace_id"], scope="path", path="../outside", dry_run=True)

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_forget_path"
