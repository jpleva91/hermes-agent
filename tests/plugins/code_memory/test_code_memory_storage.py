"""Tests for the experimental Code Memory v0 SQLite/FTS storage slice."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from plugins.code_memory.storage import CodeMemoryStorage, get_index_db_path, workspace_fingerprint


@pytest.fixture
def hermes_profile_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "profiles" / "poscoding"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "poscoding")
    return home


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def test_index_path_uses_active_profile_home_not_default_home(
    hermes_profile_home: Path, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    fingerprint = workspace_fingerprint(workspace, profile_name="poscoding")
    db_path = get_index_db_path(fingerprint)
    storage = CodeMemoryStorage()
    result = storage.initialize_workspace(workspace, profile_name="poscoding")

    assert result["success"] is True
    assert db_path == hermes_profile_home / "code_memory" / "indexes" / fingerprint / "code_memory.sqlite"
    assert result["db_path"] == str(db_path)
    assert db_path.exists()
    assert not (tmp_path / "home" / ".hermes" / "state.db").exists()


def test_migrations_create_required_v0_tables_only(hermes_profile_home: Path, workspace: Path) -> None:
    storage = CodeMemoryStorage()
    result = storage.initialize_workspace(workspace, profile_name="poscoding")

    assert result["success"] is True
    conn = sqlite3.connect(result["db_path"])
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            )
        }
        assert {
            "schema_migrations",
            "workspaces",
            "source_files",
            "chunks",
            "chunks_fts",
            "retrieval_logs",
        }.issubset(tables)
        assert "chunk_embeddings" not in tables
        assert "episodes" not in tables
        assert "facts" not in tables
        assert "entity_edges" not in tables
        migration = conn.execute("SELECT version, description FROM schema_migrations").fetchone()
        assert migration == (1, "code_memory_v0_storage")
    finally:
        conn.close()


def test_workspace_fingerprints_isolate_indexes(hermes_profile_home: Path, tmp_path: Path) -> None:
    one = tmp_path / "repo-one"
    two = tmp_path / "repo-two"
    one.mkdir()
    two.mkdir()

    storage = CodeMemoryStorage()
    first = storage.initialize_workspace(one, profile_name="poscoding")
    second = storage.initialize_workspace(two, profile_name="poscoding")

    assert first["success"] is True
    assert second["success"] is True
    assert first["workspace"]["id"] != second["workspace"]["id"]
    assert first["db_path"] != second["db_path"]


def test_source_file_and_chunk_crud_with_fts_citations(
    hermes_profile_home: Path, workspace: Path
) -> None:
    storage = CodeMemoryStorage()
    initialized = storage.initialize_workspace(workspace, profile_name="poscoding")
    workspace_id = initialized["workspace"]["id"]

    source = storage.upsert_source_file(
        workspace_id=workspace_id,
        rel_path="agent/example.py",
        language="python",
        mime="text/x-python",
        size_bytes=64,
        content_sha256="file-sha-a",
        git_blob_sha="blob-a",
        git_commit="commit-a",
        mtime_ns=123,
    )
    assert source["success"] is True

    chunk = storage.upsert_chunk(
        workspace_id=workspace_id,
        source_file_id=source["source_file"]["id"],
        chunk_kind="symbol",
        content="def frobnicate_widgets():\n    return 'needle result'\n",
        content_sha256="chunk-sha-a",
        start_line=10,
        end_line=11,
        symbol_name="frobnicate_widgets",
        symbol_kind="function",
        tags=["widgets", "example"],
    )
    assert chunk["success"] is True

    results = storage.search_chunks(workspace_id=workspace_id, query="needle", limit=5)
    assert results["success"] is True
    assert results["results"] == [
        {
            "chunk_id": chunk["chunk"]["id"],
            "source_file_id": source["source_file"]["id"],
            "rel_path": "agent/example.py",
            "start_line": 10,
            "end_line": 11,
            "content_sha256": "chunk-sha-a",
            "source_content_sha256": "file-sha-a",
            "symbol_name": "frobnicate_widgets",
            "symbol_kind": "function",
            "rank": pytest.approx(results["results"][0]["rank"]),
        }
    ]

    updated = storage.upsert_chunk(
        workspace_id=workspace_id,
        source_file_id=source["source_file"]["id"],
        chunk_kind="symbol",
        content="def frobnicate_widgets():\n    return 'updated banana result'\n",
        content_sha256="chunk-sha-b",
        start_line=10,
        end_line=11,
        symbol_name="frobnicate_widgets",
        symbol_kind="function",
        tags=["widgets"],
        chunk_id=chunk["chunk"]["id"],
    )
    assert updated["success"] is True
    assert storage.search_chunks(workspace_id=workspace_id, query="needle")["results"] == []
    assert storage.search_chunks(workspace_id=workspace_id, query="banana")["results"][0]["content_sha256"] == "chunk-sha-b"

    deleted = storage.delete_chunk(chunk["chunk"]["id"])
    assert deleted == {"success": True, "deleted": {"chunks": 1, "chunks_fts": 1}}
    assert storage.search_chunks(workspace_id=workspace_id, query="banana")["results"] == []

    deleted_source = storage.delete_source_file(workspace_id=workspace_id, rel_path="agent/example.py")
    assert deleted_source["success"] is True
    assert deleted_source["deleted"]["source_files"] == 1


def test_retrieval_logging_stores_metadata_without_raw_content(
    hermes_profile_home: Path, workspace: Path
) -> None:
    storage = CodeMemoryStorage()
    initialized = storage.initialize_workspace(workspace, profile_name="poscoding")
    workspace_id = initialized["workspace"]["id"]

    result = storage.log_retrieval(
        workspace_id=workspace_id,
        query="needle",
        results=[{"chunk_id": "chunk-1", "rel_path": "a.py", "start_line": 1, "end_line": 2}],
        filters={"language": "python"},
        latency_ms=12,
        session_id="sess-1",
        tool_name="code_memory_search",
    )

    assert result["success"] is True
    conn = sqlite3.connect(initialized["db_path"])
    try:
        row = conn.execute("SELECT results_json FROM retrieval_logs WHERE id = ?", (result["log"]["id"],)).fetchone()
    finally:
        conn.close()
    stored = json.loads(row[0])
    assert stored == [{"chunk_id": "chunk-1", "rel_path": "a.py", "start_line": 1, "end_line": 2}]
    assert "content" not in json.dumps(stored)


def test_public_api_errors_are_json_compatible(hermes_profile_home: Path) -> None:
    storage = CodeMemoryStorage()

    result = storage.initialize_workspace(hermes_profile_home / "missing", profile_name="poscoding")

    assert result["success"] is False
    json.dumps(result)
    assert result["error"]["code"] == "workspace_not_found"
