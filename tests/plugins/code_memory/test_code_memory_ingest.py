"""Tests for safe explicit Code Memory v0 ingestion and status."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from plugins.code_memory.ingest import CodeMemoryIndexer, CodeMemoryPolicy, resolve_workspace_path


@pytest.fixture
def hermes_profile_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "profiles" / "poscoding"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "poscoding")
    return home


def init_git_repo(root: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)


def allowed_fixture_policy(root: Path, **overrides: object) -> CodeMemoryPolicy:
    return CodeMemoryPolicy(allowed_workspace_roots=(str(root),), **overrides)


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "fixture-repo"
    root.mkdir()
    init_git_repo(root)
    (root / "pyproject.toml").write_text('[project]\nname = "hermes-agent"\n', encoding="utf-8")
    (root / "plugins" / "code_memory").mkdir(parents=True)
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    (root / "README.md").write_text("# Fixture\n\nDocs are indexed.\n", encoding="utf-8")
    (root / "ignored.txt").write_text("ignored should not index\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=super-secret-value\n", encoding="utf-8")
    (root / "binary.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    (root / "bundle.min.js").write_text("function a(){return 1};", encoding="utf-8")
    (root / "large.py").write_text("x = 'too big'\n" * 20, encoding="utf-8")
    subprocess = __import__("subprocess")
    subprocess.run(["git", "add", ".gitignore", "pyproject.toml", "src/app.py", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)
    return root


def rel_paths(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT rel_path FROM source_files WHERE skip_reason IS NULL")}
    finally:
        conn.close()


def skip_reasons(db_path: str) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT rel_path, skip_reason FROM source_files WHERE skip_reason IS NOT NULL"))
    finally:
        conn.close()


def test_fixture_repo_ingestion_indexes_allowed_files_and_counts_safe_skips(
    hermes_profile_home: Path, fixture_repo: Path
) -> None:
    result = CodeMemoryIndexer(policy=allowed_fixture_policy(fixture_repo, max_file_bytes=80, max_total_bytes=2_000)).ingest(
        workspace=fixture_repo
    )

    assert result["success"] is True
    assert set(result["indexed_paths"]) == {"README.md", "pyproject.toml", "src/app.py"}
    assert rel_paths(result["db_path"]) == {"README.md", "pyproject.toml", "src/app.py"}
    assert result["counts"]["indexed"] == 3
    assert result["counts"]["skipped"] >= 5
    reasons = skip_reasons(result["db_path"])
    assert reasons["ignored.txt"] == "gitignored"
    assert reasons[".env"] == "secret_like_path"
    assert reasons["binary.png"] == "binary"
    assert reasons["bundle.min.js"] == "generated_or_minified"
    assert reasons["large.py"] == "too_large"
    serialized = json.dumps(result) + json.dumps(reasons)
    assert "super-secret-value" not in serialized


def test_symlink_escape_and_path_traversal_fail_closed(
    hermes_profile_home: Path, fixture_repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("print('escape')\n", encoding="utf-8")
    (fixture_repo / "escape.py").symlink_to(outside)

    result = CodeMemoryIndexer(policy=allowed_fixture_policy(fixture_repo)).ingest(workspace=fixture_repo)

    assert result["success"] is True
    assert skip_reasons(result["db_path"])["escape.py"] == "symlink_escape"
    traversal = resolve_workspace_path("../outside", cwd=fixture_repo)
    assert traversal["success"] is False
    assert traversal["error"]["code"] == "path_traversal"


def test_incremental_rerun_updates_only_changed_hashes_and_marks_deleted(
    hermes_profile_home: Path, fixture_repo: Path
) -> None:
    indexer = CodeMemoryIndexer(policy=allowed_fixture_policy(fixture_repo, max_file_bytes=80, max_total_bytes=2_000))
    first = indexer.ingest(workspace=fixture_repo)
    second = indexer.ingest(workspace=fixture_repo)
    (fixture_repo / "src" / "app.py").write_text("def hello():\n    return 'changed'\n", encoding="utf-8")
    (fixture_repo / "README.md").unlink()
    third = indexer.ingest(workspace=fixture_repo)

    assert first["counts"]["indexed"] == 3
    assert second["counts"]["unchanged"] == 3
    assert second["counts"]["updated"] == 0
    assert third["counts"]["updated"] == 1
    assert third["counts"]["deleted"] == 1
    status = indexer.status(workspace_id=third["workspace"]["id"])
    assert status["success"] is True
    assert status["counts"]["active_files"] == 2
    assert status["counts"]["deleted_files"] == 1
    assert status["last_indexed_at"] is not None
    assert status["policy"]["local_only"] is True
    assert status["policy"]["include_gitignored"] is False
    assert status["db"]["path"] == third["db_path"]
    assert status["db"]["size_bytes"] > 0


def test_workspace_resolver_accepts_verified_local_paths_and_fails_closed_for_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    child = root / "src"
    child.mkdir()

    absolute = resolve_workspace_path(str(child), cwd=root)
    relative = resolve_workspace_path("src", cwd=root)
    assert absolute["success"] is True
    assert Path(absolute["path"]) == child.resolve()
    assert relative["success"] is True
    assert Path(relative["path"]) == child.resolve()

    monkeypatch.delenv("HERMES_KANBAN_WORKSPACE", raising=False)
    monkeypatch.delenv("HERMES_CLI_WORKSPACE", raising=False)
    auto = resolve_workspace_path("auto", cwd=None, workdir=None)
    assert auto["success"] is False
    assert auto["error"]["code"] == "workspace_unverifiable"


def test_include_gitignored_is_hard_disabled(hermes_profile_home: Path, fixture_repo: Path) -> None:
    result = CodeMemoryIndexer(policy=allowed_fixture_policy(fixture_repo, include_gitignored=True)).ingest(workspace=fixture_repo)

    assert result["success"] is False
    assert result["error"]["code"] == "include_gitignored_disabled"


def test_non_hermes_workspace_is_rejected_before_contents_are_read(
    hermes_profile_home: Path, tmp_path: Path
) -> None:
    private_repo = tmp_path / "not-hermes-private"
    private_repo.mkdir()
    (private_repo / "app.py").write_text("API_KEY='super-secret-value'\n", encoding="utf-8")

    result = CodeMemoryIndexer().ingest(workspace=private_repo)

    assert result["success"] is False
    assert result["error"]["code"] == "workspace_out_of_scope"
    assert result["error"]["details"]["reason"] == "not_allowed_root"
    assert "super-secret-value" not in json.dumps(result)
    assert not (hermes_profile_home / "code_memory" / "indexes").exists()


def test_non_allowlisted_workspace_with_pyproject_is_rejected_before_pyproject_read(
    hermes_profile_home: Path, tmp_path: Path
) -> None:
    private_repo = tmp_path / "private-poetry-repo"
    private_repo.mkdir()
    private_pyproject = private_repo / "pyproject.toml"
    private_pyproject.write_text('[project]\nname = "private-app"\n', encoding="utf-8")
    (private_repo / "app.py").write_text('print("private")\n', encoding="utf-8")
    original_read_text = Path.read_text

    def guarded_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == private_pyproject:
            raise RuntimeError("private pyproject content read before allowlist approval")
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", guarded_read_text):
        result = CodeMemoryIndexer().ingest(workspace=private_repo)

    assert result["success"] is False
    assert result["error"]["code"] == "workspace_out_of_scope"
    assert result["error"]["details"]["reason"] == "not_allowed_root"
    assert not (hermes_profile_home / "code_memory" / "indexes").exists()


def test_broad_home_like_workspace_root_is_rejected(
    hermes_profile_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = CodeMemoryIndexer().ingest(workspace=home)

    assert result["success"] is False
    assert result["error"]["code"] == "workspace_out_of_scope"
    assert result["error"]["details"]["reason"] == "broad_root"
    assert not (hermes_profile_home / "code_memory" / "indexes").exists()
