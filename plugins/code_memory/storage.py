"""SQLite/FTS5 storage adapter for Hermes Code Memory v0.

The v0 storage layer is deliberately local-only and profile-aware. It stores one
SQLite index per workspace fingerprint under::

    $HERMES_HOME/code_memory/indexes/<workspace_fingerprint>/code_memory.sqlite

It does not use ``state.db``, does not create vector/embedding tables, and does
not integrate with ``MemoryProvider.prefetch``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_DB_FILENAME = "code_memory.sqlite"
_SCHEMA_VERSION = 1
_SCHEMA_DESCRIPTION = "code_memory_v0_storage"

_REQUIRED_CHUNK_KINDS = {
    "symbol",
    "section",
    "test",
    "config",
    "doc",
    "fallback_window",
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_compatible(value: Any) -> Any:
    """Round-trip through JSON so public results avoid non-serializable values."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "error": {"code": code, "message": message},
    }
    if details:
        payload["error"]["details"] = _json_compatible(details)
    return payload


def _git_value(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _repo_metadata(root: Path) -> dict[str, str | None]:
    return {
        "repo_url": _git_value(root, "config", "--get", "remote.origin.url"),
        "git_remote": _git_value(root, "remote", "get-url", "origin"),
        "git_head": _git_value(root, "rev-parse", "HEAD"),
    }


def _profile_name(explicit: str | None = None) -> str:
    return (explicit or os.environ.get("HERMES_PROFILE") or "default").strip() or "default"


def workspace_fingerprint(
    root_path: str | Path,
    *,
    profile_name: str | None = None,
    repo_url: str | None = None,
    git_remote: str | None = None,
    git_head: str | None = None,
) -> str:
    """Return the stable v0 workspace id/fingerprint.

    The fingerprint includes the active profile name and resolved root path so
    two profiles indexing the same local checkout do not collide. Git metadata
    is included when available but omitted values remain stable as empty strings.
    """
    root = Path(root_path).expanduser().resolve()
    profile = _profile_name(profile_name)
    if repo_url is None or git_remote is None or git_head is None:
        meta = _repo_metadata(root)
        repo_url = repo_url if repo_url is not None else meta["repo_url"]
        git_remote = git_remote if git_remote is not None else meta["git_remote"]
        git_head = git_head if git_head is not None else meta["git_head"]
    material = _json_dumps(
        {
            "profile_name": profile,
            "root_path": str(root),
            "repo_url": repo_url or "",
            "git_remote": git_remote or "",
            "git_head": git_head or "",
        }
    )
    return _sha256_text(material)[:32]


def get_index_db_path(workspace_id: str) -> Path:
    """Return the profile-aware Code Memory DB path for a workspace id."""
    safe_workspace_id = str(workspace_id).strip()
    if not safe_workspace_id or "/" in safe_workspace_id or ".." in safe_workspace_id:
        raise ValueError("workspace_id must be a non-empty path segment")
    return get_hermes_home() / "code_memory" / "indexes" / safe_workspace_id / _DB_FILENAME


class CodeMemoryStorage:
    """Local SQLite/FTS5 storage adapter for Code Memory v0."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path).expanduser() if db_path is not None else None

    def initialize_workspace(
        self,
        root_path: str | Path,
        *,
        profile_name: str | None = None,
        config: dict[str, Any] | None = None,
        repo_url: str | None = None,
        git_remote: str | None = None,
        git_head: str | None = None,
    ) -> dict[str, Any]:
        try:
            root = Path(root_path).expanduser().resolve(strict=True)
        except FileNotFoundError:
            return _error("workspace_not_found", f"Workspace path does not exist: {root_path}")
        if not root.is_dir():
            return _error("workspace_not_directory", f"Workspace path is not a directory: {root}")

        profile = _profile_name(profile_name)
        meta = _repo_metadata(root)
        repo_url = repo_url if repo_url is not None else meta["repo_url"]
        git_remote = git_remote if git_remote is not None else meta["git_remote"]
        git_head = git_head if git_head is not None else meta["git_head"]
        workspace_id = workspace_fingerprint(
            root,
            profile_name=profile,
            repo_url=repo_url,
            git_remote=git_remote,
            git_head=git_head,
        )
        db_path = self._resolve_db_path(workspace_id)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect(db_path)
        try:
            self._migrate(conn)
            now = _now_iso()
            workspace = {
                "id": workspace_id,
                "profile_name": profile,
                "root_path": str(root),
                "root_path_hash": _sha256_text(str(root)),
                "repo_url": repo_url,
                "git_remote": git_remote,
                "git_head": git_head,
                "created_at": now,
                "last_indexed_at": None,
                "config_json": _json_dumps(config or {}),
            }
            conn.execute(
                """
                INSERT INTO workspaces (
                    id, profile_name, root_path, root_path_hash, repo_url,
                    git_remote, git_head, created_at, last_indexed_at, config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    profile_name=excluded.profile_name,
                    root_path=excluded.root_path,
                    root_path_hash=excluded.root_path_hash,
                    repo_url=excluded.repo_url,
                    git_remote=excluded.git_remote,
                    git_head=excluded.git_head,
                    config_json=excluded.config_json
                """,
                (
                    workspace["id"],
                    workspace["profile_name"],
                    workspace["root_path"],
                    workspace["root_path_hash"],
                    workspace["repo_url"],
                    workspace["git_remote"],
                    workspace["git_head"],
                    workspace["created_at"],
                    workspace["last_indexed_at"],
                    workspace["config_json"],
                ),
            )
            conn.commit()
            row = self._workspace_row(conn, workspace_id)
            return {"success": True, "workspace": row, "db_path": str(db_path)}
        except sqlite3.Error as exc:
            conn.rollback()
            return _error("sqlite_error", str(exc), db_path=str(db_path))
        finally:
            conn.close()

    def upsert_source_file(
        self,
        *,
        workspace_id: str,
        rel_path: str,
        language: str | None = None,
        mime: str | None = None,
        size_bytes: int,
        content_sha256: str,
        git_blob_sha: str | None = None,
        git_commit: str | None = None,
        mtime_ns: int | None = None,
        source_file_id: str | None = None,
        skip_reason: str | None = None,
    ) -> dict[str, Any]:
        if not rel_path or Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
            return _error("invalid_rel_path", "rel_path must be a safe relative path", rel_path=rel_path)
        if size_bytes < 0:
            return _error("invalid_size", "size_bytes must be non-negative", size_bytes=size_bytes)
        source_file_id = source_file_id or _sha256_text(f"{workspace_id}:{rel_path}")[:32]
        indexed_at = _now_iso()
        conn = self._connect_workspace(workspace_id)
        try:
            if not self._workspace_exists(conn, workspace_id):
                return _error("workspace_not_initialized", "Workspace has not been initialized", workspace_id=workspace_id)
            conn.execute(
                """
                INSERT INTO source_files (
                    id, workspace_id, rel_path, language, mime, size_bytes,
                    content_sha256, git_blob_sha, git_commit, mtime_ns,
                    indexed_at, deleted_at, skip_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(workspace_id, rel_path) DO UPDATE SET
                    language=excluded.language,
                    mime=excluded.mime,
                    size_bytes=excluded.size_bytes,
                    content_sha256=excluded.content_sha256,
                    git_blob_sha=excluded.git_blob_sha,
                    git_commit=excluded.git_commit,
                    mtime_ns=excluded.mtime_ns,
                    indexed_at=excluded.indexed_at,
                    deleted_at=NULL,
                    skip_reason=excluded.skip_reason
                """,
                (
                    source_file_id,
                    workspace_id,
                    rel_path,
                    language,
                    mime,
                    int(size_bytes),
                    content_sha256,
                    git_blob_sha,
                    git_commit,
                    mtime_ns,
                    indexed_at,
                    skip_reason,
                ),
            )
            conn.commit()
            source = self._source_file_row(conn, workspace_id, rel_path)
            return {"success": True, "source_file": source}
        except sqlite3.Error as exc:
            conn.rollback()
            return _error("sqlite_error", str(exc), workspace_id=workspace_id)
        finally:
            conn.close()

    def delete_source_file(
        self,
        *,
        workspace_id: str,
        rel_path: str,
        hard_delete: bool = False,
    ) -> dict[str, Any]:
        conn = self._connect_workspace(workspace_id)
        try:
            source = self._source_file_row(conn, workspace_id, rel_path)
            if source is None:
                return {"success": True, "deleted": {"source_files": 0, "chunks": 0, "chunks_fts": 0}}
            chunk_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM chunks WHERE workspace_id = ? AND source_file_id = ?",
                    (workspace_id, source["id"]),
                ).fetchall()
            ]
            fts_deleted = 0
            chunks_deleted = 0
            if chunk_ids:
                placeholders = ",".join("?" for _ in chunk_ids)
                fts_deleted = conn.execute(
                    f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", chunk_ids
                ).rowcount
                chunks_deleted = conn.execute(
                    f"DELETE FROM chunks WHERE id IN ({placeholders})", chunk_ids
                ).rowcount
            if hard_delete:
                source_deleted = conn.execute(
                    "DELETE FROM source_files WHERE workspace_id = ? AND rel_path = ?",
                    (workspace_id, rel_path),
                ).rowcount
            else:
                source_deleted = conn.execute(
                    "UPDATE source_files SET deleted_at = ? WHERE workspace_id = ? AND rel_path = ?",
                    (_now_iso(), workspace_id, rel_path),
                ).rowcount
            conn.commit()
            return {
                "success": True,
                "deleted": {
                    "source_files": source_deleted,
                    "chunks": chunks_deleted,
                    "chunks_fts": fts_deleted,
                },
            }
        except sqlite3.Error as exc:
            conn.rollback()
            return _error("sqlite_error", str(exc), workspace_id=workspace_id)
        finally:
            conn.close()

    def upsert_chunk(
        self,
        *,
        workspace_id: str,
        source_file_id: str,
        chunk_kind: str,
        content: str,
        content_sha256: str,
        start_line: int,
        end_line: int,
        start_byte: int | None = None,
        end_byte: int | None = None,
        symbol_name: str | None = None,
        symbol_kind: str | None = None,
        tags: list[str] | None = None,
        chunk_id: str | None = None,
    ) -> dict[str, Any]:
        if chunk_kind not in _REQUIRED_CHUNK_KINDS:
            return _error("invalid_chunk_kind", "Unsupported chunk_kind", chunk_kind=chunk_kind)
        if start_line <= 0 or end_line < start_line:
            return _error("invalid_line_range", "Line range must be positive and ordered")
        tags = tags or []
        chunk_id = chunk_id or _sha256_text(
            f"{workspace_id}:{source_file_id}:{start_line}:{end_line}:{content_sha256}"
        )[:32]
        now = _now_iso()
        conn = self._connect_workspace(workspace_id)
        try:
            source = self._source_file_by_id(conn, workspace_id, source_file_id)
            if source is None:
                return _error("source_file_not_found", "Source file has not been indexed", source_file_id=source_file_id)
            conn.execute(
                """
                INSERT INTO chunks (
                    id, workspace_id, source_file_id, chunk_kind, symbol_name,
                    symbol_kind, start_line, end_line, start_byte, end_byte,
                    content, content_sha256, tags_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    workspace_id=excluded.workspace_id,
                    source_file_id=excluded.source_file_id,
                    chunk_kind=excluded.chunk_kind,
                    symbol_name=excluded.symbol_name,
                    symbol_kind=excluded.symbol_kind,
                    start_line=excluded.start_line,
                    end_line=excluded.end_line,
                    start_byte=excluded.start_byte,
                    end_byte=excluded.end_byte,
                    content=excluded.content,
                    content_sha256=excluded.content_sha256,
                    tags_json=excluded.tags_json,
                    updated_at=excluded.updated_at
                """,
                (
                    chunk_id,
                    workspace_id,
                    source_file_id,
                    chunk_kind,
                    symbol_name,
                    symbol_kind,
                    int(start_line),
                    int(end_line),
                    start_byte,
                    end_byte,
                    content,
                    content_sha256,
                    _json_dumps(tags),
                    now,
                    now,
                ),
            )
            conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
            conn.execute(
                """
                INSERT INTO chunks_fts (chunk_id, workspace_id, source_file_id, rel_path, content, symbol_name, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, workspace_id, source_file_id, source["rel_path"], content, symbol_name or "", " ".join(tags)),
            )
            conn.commit()
            return {"success": True, "chunk": self._chunk_row(conn, chunk_id)}
        except sqlite3.Error as exc:
            conn.rollback()
            return _error("sqlite_error", str(exc), workspace_id=workspace_id)
        finally:
            conn.close()

    def delete_chunk(self, chunk_id: str) -> dict[str, Any]:
        workspace_id = self._workspace_id_for_chunk(chunk_id)
        if workspace_id is None:
            return {"success": True, "deleted": {"chunks": 0, "chunks_fts": 0}}
        conn = self._connect_workspace(workspace_id)
        try:
            fts_deleted = conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,)).rowcount
            chunks_deleted = conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,)).rowcount
            conn.commit()
            return {"success": True, "deleted": {"chunks": chunks_deleted, "chunks_fts": fts_deleted}}
        except sqlite3.Error as exc:
            conn.rollback()
            return _error("sqlite_error", str(exc), chunk_id=chunk_id)
        finally:
            conn.close()

    def search_chunks(self, *, workspace_id: str, query: str, limit: int = 10) -> dict[str, Any]:
        if not query.strip():
            return _error("empty_query", "query must not be empty")
        safe_limit = max(1, min(int(limit), 50))
        conn = self._connect_workspace(workspace_id)
        try:
            rows = conn.execute(
                """
                SELECT
                    c.id AS chunk_id,
                    c.source_file_id,
                    s.rel_path,
                    c.start_line,
                    c.end_line,
                    c.content_sha256,
                    s.content_sha256 AS source_content_sha256,
                    c.symbol_name,
                    c.symbol_kind,
                    bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.chunk_id
                JOIN source_files s ON s.id = c.source_file_id
                WHERE chunks_fts MATCH ?
                  AND c.workspace_id = ?
                  AND s.workspace_id = ?
                  AND s.deleted_at IS NULL
                ORDER BY rank
                LIMIT ?
                """,
                (query, workspace_id, workspace_id, safe_limit),
            ).fetchall()
            results = [dict(row) for row in rows]
            return {"success": True, "query": query, "workspace_id": workspace_id, "results": results}
        except sqlite3.OperationalError as exc:
            return _error("fts_query_error", str(exc), query=query)
        finally:
            conn.close()

    def log_retrieval(
        self,
        *,
        workspace_id: str,
        query: str,
        results: list[dict[str, Any]],
        filters: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        session_id: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        sanitized_results = [self._sanitize_retrieval_result(item) for item in results]
        log_id = uuid.uuid4().hex
        conn = self._connect_workspace(workspace_id)
        try:
            conn.execute(
                """
                INSERT INTO retrieval_logs (
                    id, workspace_id, session_id, query, tool_name,
                    filters_json, results_json, latency_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    workspace_id,
                    session_id,
                    query,
                    tool_name,
                    _json_dumps(filters or {}),
                    _json_dumps(sanitized_results),
                    latency_ms,
                    _now_iso(),
                ),
            )
            conn.commit()
            return {
                "success": True,
                "log": {
                    "id": log_id,
                    "workspace_id": workspace_id,
                    "result_count": len(sanitized_results),
                },
            }
        except sqlite3.Error as exc:
            conn.rollback()
            return _error("sqlite_error", str(exc), workspace_id=workspace_id)
        finally:
            conn.close()

    def _resolve_db_path(self, workspace_id: str) -> Path:
        return self._db_path if self._db_path is not None else get_index_db_path(workspace_id)

    def _connect_workspace(self, workspace_id: str) -> sqlite3.Connection:
        db_path = self._resolve_db_path(workspace_id)
        return self._connect(db_path)

    @staticmethod
    def _connect(db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                profile_name TEXT NOT NULL,
                root_path TEXT NOT NULL,
                root_path_hash TEXT NOT NULL,
                repo_url TEXT,
                git_remote TEXT,
                git_head TEXT,
                created_at TEXT NOT NULL,
                last_indexed_at TEXT,
                config_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_files (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                rel_path TEXT NOT NULL,
                language TEXT,
                mime TEXT,
                size_bytes INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                git_blob_sha TEXT,
                git_commit TEXT,
                mtime_ns INTEGER,
                indexed_at TEXT NOT NULL,
                deleted_at TEXT,
                skip_reason TEXT,
                UNIQUE(workspace_id, rel_path)
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                source_file_id TEXT NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
                chunk_kind TEXT NOT NULL,
                symbol_name TEXT,
                symbol_kind TEXT,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                start_byte INTEGER,
                end_byte INTEGER,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                workspace_id UNINDEXED,
                source_file_id UNINDEXED,
                rel_path,
                content,
                symbol_name,
                tags,
                tokenize = 'unicode61'
            );

            CREATE TABLE IF NOT EXISTS retrieval_logs (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                session_id TEXT,
                query TEXT NOT NULL,
                tool_name TEXT,
                filters_json TEXT NOT NULL,
                results_json TEXT NOT NULL,
                latency_ms INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_source_files_workspace_rel_path
                ON source_files(workspace_id, rel_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_workspace_source
                ON chunks(workspace_id, source_file_id);
            CREATE INDEX IF NOT EXISTS idx_retrieval_logs_workspace_created
                ON retrieval_logs(workspace_id, created_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
            (_SCHEMA_VERSION, _now_iso(), _SCHEMA_DESCRIPTION),
        )
        conn.commit()

    @staticmethod
    def _workspace_exists(conn: sqlite3.Connection, workspace_id: str) -> bool:
        return conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is not None

    @staticmethod
    def _workspace_row(conn: sqlite3.Connection, workspace_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _source_file_row(conn: sqlite3.Connection, workspace_id: str, rel_path: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM source_files WHERE workspace_id = ? AND rel_path = ?",
            (workspace_id, rel_path),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _source_file_by_id(conn: sqlite3.Connection, workspace_id: str, source_file_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM source_files WHERE workspace_id = ? AND id = ? AND deleted_at IS NULL",
            (workspace_id, source_file_id),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _chunk_row(conn: sqlite3.Connection, chunk_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["tags"] = json.loads(data.pop("tags_json") or "[]")
        return data

    def _workspace_id_for_chunk(self, chunk_id: str) -> str | None:
        if self._db_path is not None:
            conn = self._connect(self._db_path)
            try:
                row = conn.execute("SELECT workspace_id FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
                return row[0] if row else None
            finally:
                conn.close()

        indexes = get_hermes_home() / "code_memory" / "indexes"
        if not indexes.exists():
            return None
        for db_path in indexes.glob(f"*/{_DB_FILENAME}"):
            conn = self._connect(db_path)
            try:
                row = conn.execute("SELECT workspace_id FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
                if row:
                    return row[0]
            except sqlite3.Error:
                continue
            finally:
                conn.close()
        return None

    @staticmethod
    def _sanitize_retrieval_result(result: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "chunk_id",
            "source_file_id",
            "rel_path",
            "start_line",
            "end_line",
            "content_sha256",
            "source_content_sha256",
            "symbol_name",
            "symbol_kind",
            "rank",
            "freshness",
        }
        return _json_compatible({key: value for key, value in result.items() if key in allowed})
