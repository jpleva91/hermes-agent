"""Explicit CLI/tool operations for Hermes Code Memory v0.

The surface is intentionally opt-in and local/profile-scoped. It exposes
bounded JSON-compatible summaries for ingestion, status, lexical search, and
forget/delete operations without MemoryProvider prefetch or automatic context
injection.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from argparse import Namespace
from pathlib import Path
from typing import Any

from .ingest import CodeMemoryIndexer, CodeMemoryPolicy, resolve_workspace_path
from .storage import CodeMemoryStorage, _error, get_index_db_path

_MAX_SUMMARY_RESULTS = 10


def _json_compatible(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _parse_policy(policy: dict[str, Any] | None) -> CodeMemoryPolicy:
    if not policy:
        return CodeMemoryPolicy()
    allowed = set(CodeMemoryPolicy.__dataclass_fields__)  # type: ignore[attr-defined]
    return CodeMemoryPolicy(**{key: value for key, value in policy.items() if key in allowed})


def _success(operation: str, summary: str, **payload: Any) -> dict[str, Any]:
    result = {"success": True, "operation": operation, "summary": summary}
    result.update(payload)
    return _json_compatible(result)


def _bounded_status(status: dict[str, Any]) -> dict[str, Any]:
    if not status.get("success"):
        return status
    return {
        "workspace_id": status.get("workspace_id"),
        "workspace": status.get("workspace"),
        "root_path_hash": status.get("root_path_hash"),
        "last_indexed_at": status.get("last_indexed_at"),
        "db": status.get("db"),
        "counts": status.get("counts", {}),
        "skip_reasons": status.get("skip_reasons", {}),
        "freshness": status.get("freshness"),
        "warnings": status.get("warnings", []),
    }


def _bounded_search(retrieval: dict[str, Any], *, include_context_pack: bool) -> dict[str, Any]:
    if not retrieval.get("success"):
        return retrieval
    results = []
    for item in (retrieval.get("results") or [])[:_MAX_SUMMARY_RESULTS]:
        results.append(
            {
                "chunk_id": item.get("chunk_id"),
                "source_file_id": item.get("source_file_id"),
                "rel_path": item.get("rel_path"),
                "language": item.get("language"),
                "chunk_kind": item.get("chunk_kind"),
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "citation": item.get("citation"),
                "freshness": item.get("freshness"),
                "score": item.get("score"),
                "content_sha256": item.get("content_sha256"),
                "warnings": item.get("warnings", []),
                "truncated": item.get("truncated", False),
            }
        )
    payload = {
        "query": retrieval.get("query"),
        "workspace_id": retrieval.get("workspace_id"),
        "freshness_mode": retrieval.get("freshness_mode"),
        "filters": retrieval.get("filters", {}),
        "budget": retrieval.get("budget", {}),
        "counts": {**(retrieval.get("counts") or {}), "returned": len(retrieval.get("results") or [])},
        "warnings": retrieval.get("warnings", []),
        "latency_ms": retrieval.get("latency_ms"),
        "results": results,
    }
    if retrieval.get("retrieval_log_id"):
        payload["retrieval_log_id"] = retrieval["retrieval_log_id"]
    if include_context_pack:
        payload["context_pack"] = CodeMemoryStorage().format_context_pack(retrieval)
    return payload


def code_memory_ingest(
    *,
    workspace: str = "auto",
    cwd: str | None = None,
    workdir: str | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Index an explicitly verified local workspace."""

    result = CodeMemoryIndexer(policy=_parse_policy(policy)).ingest(workspace=workspace, cwd=cwd, workdir=workdir)
    if not result.get("success"):
        return _json_compatible({"operation": "code_memory_ingest", **result})
    counts = result.get("counts", {})
    workspace_info = result.get("workspace", {})
    summary = (
        "Code Memory ingest indexed "
        f"{counts.get('indexed', 0)} new, {counts.get('updated', 0)} updated, "
        f"{counts.get('unchanged', 0)} unchanged, {counts.get('skipped', 0)} skipped file(s)."
    )
    return _success(
        "code_memory_ingest",
        summary,
        workspace_id=workspace_info.get("id"),
        workspace=workspace_info,
        db_path=result.get("db_path"),
        counts=counts,
        skip_reasons=result.get("skip_reasons", {}),
        indexed_paths=result.get("indexed_paths", []),
        policy=result.get("policy", {}),
        warnings=result.get("warnings", []),
        last_indexed_at=result.get("last_indexed_at"),
    )


def code_memory_status(*, workspace_id: str) -> dict[str, Any]:
    """Return bounded status for a workspace id."""

    clean_workspace_id = _validate_workspace_id(workspace_id)
    if clean_workspace_id is None:
        return _json_compatible({"operation": "code_memory_status", **_error("invalid_workspace_id", "workspace_id must be a safe non-empty path segment")})
    if not get_index_db_path(clean_workspace_id).exists():
        return _json_compatible({"operation": "code_memory_status", **_error("workspace_not_initialized", "Workspace has not been initialized", workspace_id=clean_workspace_id)})
    status = CodeMemoryIndexer().status(workspace_id=clean_workspace_id)
    if not status.get("success"):
        return _json_compatible({"operation": "code_memory_status", **status})
    bounded = _bounded_status(status)
    counts = bounded.get("counts", {})
    summary = (
        f"Code Memory workspace {workspace_id}: "
        f"{counts.get('active_files', 0)} active, {counts.get('skipped_files', 0)} skipped, "
        f"{counts.get('deleted_files', 0)} deleted, freshness={bounded.get('freshness')}."
    )
    return _success("code_memory_status", summary, **bounded)


def code_memory_search(
    *,
    workspace_id: str,
    query: str,
    limit: int = 10,
    max_chars: int = 8000,
    filters: dict[str, Any] | None = None,
    freshness_mode: str = "fresh_only",
    include_context_pack: bool = True,
    log: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Return explicit bounded lexical evidence for a workspace."""

    clean_workspace_id = _validate_workspace_id(workspace_id)
    if clean_workspace_id is None:
        return _json_compatible({"operation": "code_memory_search", **_error("invalid_workspace_id", "workspace_id must be a safe non-empty path segment")})
    if not get_index_db_path(clean_workspace_id).exists():
        return _json_compatible({"operation": "code_memory_search", **_error("workspace_not_initialized", "Workspace has not been initialized", workspace_id=clean_workspace_id)})
    retrieval = CodeMemoryStorage().retrieve_code_memory(
        workspace_id=clean_workspace_id,
        query=query,
        limit=limit,
        max_chars=max_chars,
        filters=filters or {},
        freshness_mode=freshness_mode,
        session_id=session_id,
        tool_name="code_memory_search",
        log=log,
    )
    if not retrieval.get("success"):
        return _json_compatible({"operation": "code_memory_search", **retrieval})
    bounded = _bounded_search(retrieval, include_context_pack=include_context_pack)
    summary = (
        f"Code Memory search returned {bounded['counts'].get('returned', 0)} result(s) "
        f"for {query!r} using {bounded['budget'].get('used_chars', 0)}/{bounded['budget'].get('max_chars', 0)} chars."
    )
    return _success("code_memory_search", summary, **bounded)


def _validate_workspace_id(workspace_id: str) -> str | None:
    clean = str(workspace_id or "").strip()
    if not clean or "/" in clean or "\\" in clean or ".." in clean:
        return None
    return clean


def _normalize_forget_path(path: str | None) -> str | None:
    raw = str(path or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate.as_posix()


def _connect_existing_workspace(workspace_id: str) -> tuple[sqlite3.Connection, Path] | dict[str, Any]:
    clean = _validate_workspace_id(workspace_id)
    if clean is None:
        return _error("invalid_workspace_id", "workspace_id must be a safe non-empty path segment")
    db_path = get_index_db_path(clean)
    if not db_path.exists():
        return _error("workspace_not_initialized", "Workspace has not been initialized", workspace_id=clean)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        workspace = conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (clean,)).fetchone()
        if workspace is None:
            conn.close()
            return _error("workspace_not_initialized", "Workspace has not been initialized", workspace_id=clean)
    except sqlite3.Error as exc:
        conn.close()
        return _error("sqlite_error", str(exc), workspace_id=clean)
    return conn, db_path


def _matching_source_rows(conn: sqlite3.Connection, workspace_id: str, scope: str, rel_path: str | None) -> list[sqlite3.Row]:
    if scope == "workspace":
        return conn.execute("SELECT id, rel_path FROM source_files WHERE workspace_id = ? ORDER BY rel_path", (workspace_id,)).fetchall()
    assert rel_path is not None
    return conn.execute(
        """
        SELECT id, rel_path
        FROM source_files
        WHERE workspace_id = ? AND (rel_path = ? OR rel_path LIKE ? ESCAPE '\\')
        ORDER BY rel_path
        """,
        (workspace_id, rel_path, rel_path.replace("%", "\\%").replace("_", "\\_") + "/%"),
    ).fetchall()


def _count_fts_rows(conn: sqlite3.Connection, chunk_ids: list[str]) -> int:
    if not chunk_ids:
        return 0
    placeholders = ",".join("?" for _ in chunk_ids)
    return int(conn.execute(f"SELECT COUNT(*) FROM chunks_fts WHERE chunk_id IN ({placeholders})", chunk_ids).fetchone()[0])


def _forget_plan(conn: sqlite3.Connection, workspace_id: str, scope: str, rel_path: str | None) -> dict[str, Any]:
    rows = _matching_source_rows(conn, workspace_id, scope, rel_path)
    source_ids = [row["id"] for row in rows]
    chunk_ids: list[str] = []
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        chunk_ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM chunks WHERE workspace_id = ? AND source_file_id IN ({placeholders})",
                [workspace_id, *source_ids],
            ).fetchall()
        ]
    files = [row["rel_path"] for row in rows]
    counts = {
        "source_files": len(source_ids),
        "chunks": len(chunk_ids),
        "chunks_fts": _count_fts_rows(conn, chunk_ids),
        "files": len(files),
    }
    return {"source_ids": source_ids, "chunk_ids": chunk_ids, "matched_files": files, "counts": counts}


def _delete_plan(conn: sqlite3.Connection, workspace_id: str, plan: dict[str, Any]) -> dict[str, int]:
    chunk_ids = list(plan["chunk_ids"])
    source_ids = list(plan["source_ids"])
    deleted = {"source_files": 0, "chunks": 0, "chunks_fts": 0, "files": int(plan["counts"]["files"])}
    if chunk_ids:
        placeholders = ",".join("?" for _ in chunk_ids)
        deleted["chunks_fts"] = int(conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", chunk_ids).rowcount)
        deleted["chunks"] = int(conn.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", chunk_ids).rowcount)
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        deleted["source_files"] = int(
            conn.execute(
                f"DELETE FROM source_files WHERE workspace_id = ? AND id IN ({placeholders})",
                [workspace_id, *source_ids],
            ).rowcount
        )
    return deleted


def code_memory_forget(
    *,
    workspace_id: str,
    scope: str,
    path: str | None = None,
    dry_run: bool = True,
    approved: bool = False,
    verify_query: str | None = None,
) -> dict[str, Any]:
    """Dry-run or delete Code Memory rows for a path or whole workspace.

    Destructive deletion requires ``approved=True``. Path scope accepts only a
    safe relative path and never resolves against the filesystem, so forgets are
    confined to rows already stored in the selected profile's workspace DB.
    """

    clean_workspace_id = _validate_workspace_id(workspace_id)
    if clean_workspace_id is None:
        return _json_compatible({"operation": "code_memory_forget", **_error("invalid_workspace_id", "workspace_id must be a safe non-empty path segment")})
    scope = str(scope or "").strip().lower()
    if scope not in {"path", "workspace"}:
        return _json_compatible({"operation": "code_memory_forget", **_error("invalid_forget_scope", "scope must be path or workspace", scope=scope)})
    rel_path = None
    if scope == "path":
        rel_path = _normalize_forget_path(path)
        if rel_path is None:
            return _json_compatible({"operation": "code_memory_forget", **_error("invalid_forget_path", "path forget requires a safe relative path", path=path)})
    elif path:
        return _json_compatible({"operation": "code_memory_forget", **_error("invalid_forget_path", "workspace forget does not accept a path", path=path)})

    opened = _connect_existing_workspace(clean_workspace_id)
    if isinstance(opened, dict):
        return _json_compatible({"operation": "code_memory_forget", **opened})
    conn, db_path = opened
    try:
        plan = _forget_plan(conn, clean_workspace_id, scope, rel_path)
        target = {"workspace_id": clean_workspace_id, "db_path": str(db_path)}
        if rel_path is not None:
            target["path"] = rel_path
        if dry_run:
            summary = (
                f"Code Memory forget dry-run would delete {plan['counts']['source_files']} source row(s), "
                f"{plan['counts']['chunks']} chunk(s), {plan['counts']['chunks_fts']} FTS row(s)."
            )
            return _success(
                "code_memory_forget",
                summary,
                dry_run=True,
                scope=scope,
                target=target,
                would_delete=plan["counts"],
                matched_files=plan["matched_files"],
            )
        if not approved:
            return _json_compatible(
                {
                    "operation": "code_memory_forget",
                    **_error(
                        "approval_required",
                        "Destructive Code Memory forget requires approved=True or the CLI --yes flag after reviewing dry-run output",
                        dry_run_available=True,
                        would_delete=plan["counts"],
                    ),
                }
            )
        deleted = _delete_plan(conn, clean_workspace_id, plan)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return _json_compatible({"operation": "code_memory_forget", **_error("sqlite_error", str(exc), workspace_id=clean_workspace_id)})
    finally:
        conn.close()

    removed_db = False
    if scope == "workspace":
        try:
            shutil.rmtree(db_path.parent)
            removed_db = True
        except FileNotFoundError:
            removed_db = True
        except OSError:
            removed_db = False

    verification: dict[str, Any] = {}
    status = code_memory_status(workspace_id=clean_workspace_id)
    verification["status"] = status
    if verify_query:
        verification["search"] = code_memory_search(
            workspace_id=clean_workspace_id,
            query=verify_query,
            filters={},
            include_context_pack=False,
            log=False,
        )
        if rel_path is not None:
            verification["path_search"] = code_memory_search(
                workspace_id=clean_workspace_id,
                query=verify_query,
                filters={"path": rel_path},
                include_context_pack=False,
                log=False,
            )
    summary = (
        f"Code Memory forget deleted {deleted['source_files']} source row(s), "
        f"{deleted['chunks']} chunk(s), {deleted['chunks_fts']} FTS row(s)."
    )
    return _success(
        "code_memory_forget",
        summary,
        dry_run=False,
        scope=scope,
        target={"workspace_id": clean_workspace_id, "db_path": str(db_path), **({"path": rel_path} if rel_path else {})},
        deleted=deleted,
        matched_files=plan["matched_files"],
        db_removed=removed_db,
        verification=verification,
    )


def _print_result(result: dict[str, Any], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(result.get("summary") or result.get("error", {}).get("message") or json.dumps(result, sort_keys=True))
        if not result.get("success"):
            print(json.dumps(result.get("error", {}), indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


def _load_policy_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    return json.loads(value)


def setup_cli(parser: Any) -> None:
    sub = parser.add_subparsers(dest="code_memory_command", required=True)

    ingest = sub.add_parser("ingest", help="Index an explicitly verified local workspace")
    ingest.add_argument("workspace", nargs="?", default="auto")
    ingest.add_argument("--cwd")
    ingest.add_argument("--workdir")
    ingest.add_argument("--policy-json")
    ingest.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Show Code Memory workspace status")
    status.add_argument("workspace_id")
    status.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="Search explicit Code Memory lexical evidence")
    search.add_argument("workspace_id")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--max-chars", type=int, default=8000)
    search.add_argument("--filters-json")
    search.add_argument("--freshness-mode", default="fresh_only", choices=["fresh_only", "mark_stale", "include_deleted"])
    search.add_argument("--no-context-pack", action="store_true")
    search.add_argument("--no-log", action="store_true")
    search.add_argument("--json", action="store_true")

    forget = sub.add_parser("forget", help="Dry-run or delete Code Memory rows for a path/workspace")
    forget.add_argument("workspace_id")
    forget.add_argument("--scope", required=True, choices=["path", "workspace"])
    forget.add_argument("--path")
    forget.add_argument("--dry-run", action="store_true", default=True)
    forget.add_argument("--yes", action="store_true", help="Approve destructive delete; without this forget is dry-run/denied")
    forget.add_argument("--verify-query")
    forget.add_argument("--json", action="store_true")


def handle_cli(args: Namespace) -> int:
    command = getattr(args, "code_memory_command", None)
    if command == "ingest":
        return _print_result(
            code_memory_ingest(
                workspace=args.workspace,
                cwd=args.cwd,
                workdir=args.workdir,
                policy=_load_policy_json(args.policy_json),
            ),
            json_output=args.json,
        )
    if command == "status":
        return _print_result(code_memory_status(workspace_id=args.workspace_id), json_output=args.json)
    if command == "search":
        return _print_result(
            code_memory_search(
                workspace_id=args.workspace_id,
                query=args.query,
                limit=args.limit,
                max_chars=args.max_chars,
                filters=_load_policy_json(args.filters_json) or {},
                freshness_mode=args.freshness_mode,
                include_context_pack=not args.no_context_pack,
                log=not args.no_log,
            ),
            json_output=args.json,
        )
    if command == "forget":
        return _print_result(
            code_memory_forget(
                workspace_id=args.workspace_id,
                scope=args.scope,
                path=args.path,
                dry_run=not args.yes,
                approved=args.yes,
                verify_query=args.verify_query,
            ),
            json_output=args.json,
        )
    return _print_result(_error("missing_command", "code-memory subcommand is required"), json_output=True)
