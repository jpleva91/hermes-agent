"""Safe explicit local ingestion for Hermes Code Memory v0.

This module is intentionally local-only. It indexes only paths explicitly
resolved to a verified local directory and never performs remote calls,
embeddings, reranking, MemoryProvider prefetch, or automatic context injection.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .storage import CodeMemoryStorage, _error, _json_dumps, _now_iso


_ALLOWED_EXTENSIONS = {
    ".bash",
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".rst",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

_DENYLIST_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}

_SECRET_PATH_PARTS = {
    ".env",
    ".env.local",
    ".envrc",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "secrets.json",
}
_SECRET_SUBSTRINGS = ("secret", "token", "apikey", "api_key", "password", "passwd", "private_key")
_GENERATED_SUBSTRINGS = ("generated", "vendor", "bundle")


@dataclass(frozen=True)
class CodeMemoryPolicy:
    """Safety policy for Code Memory v0 local ingestion."""

    max_file_bytes: int = 512_000
    max_total_bytes: int = 8_000_000
    include_gitignored: bool = False
    allowed_extensions: tuple[str, ...] = tuple(sorted(_ALLOWED_EXTENSIONS))
    local_only: bool = True
    allowed_workspace_roots: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _structured_path(path: str | Path) -> str:
    return str(path)


def _candidate_from_auto(cwd: str | Path | None, workdir: str | Path | None) -> Path | None:
    candidates = [workdir, os.environ.get("HERMES_KANBAN_WORKSPACE"), os.environ.get("HERMES_CLI_WORKSPACE"), cwd]
    resolved: list[Path] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            path = Path(candidate).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if path.is_dir():
            resolved.append(path)
    if not resolved:
        return None
    first = resolved[0]
    # Fail closed if supplied local anchors disagree about the filesystem root.
    if any(path != first for path in resolved[1:]):
        return None
    return first


def _current_hermes_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_broad_workspace_root(root: Path) -> bool:
    candidates = {root.anchor}
    try:
        candidates.add(str(Path.home().expanduser().resolve(strict=True)))
    except (OSError, RuntimeError):
        pass
    return str(root) in candidates


def _is_hermes_repo_root(root: Path) -> bool:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        pyproject_text = pyproject.read_text(encoding="utf-8", errors="ignore")[:4096]
    except OSError:
        return False
    if 'name = "hermes-agent"' not in pyproject_text and "name = 'hermes-agent'" not in pyproject_text:
        return False
    return (root / "hermes_constants.py").is_file() or (root / "plugins" / "code_memory").is_dir()


def _policy_allowed_workspace_roots(policy: CodeMemoryPolicy) -> list[Path]:
    raw_roots = [str(_current_hermes_repo_root()), *policy.allowed_workspace_roots]
    resolved: list[Path] = []
    for raw_root in raw_roots:
        try:
            root = Path(raw_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root.is_dir() and _is_hermes_repo_root(root):
            resolved.append(root)
    return resolved


def enforce_workspace_scope(root: Path, policy: CodeMemoryPolicy) -> dict[str, Any]:
    """Fail closed unless ``root`` is an explicitly allowed Hermes repo root."""

    if _is_broad_workspace_root(root):
        return _error(
            "workspace_out_of_scope",
            "Code Memory v0 only ingests explicitly allowed Hermes repository roots",
            path=str(root),
            reason="broad_root",
        )
    allowed_roots = _policy_allowed_workspace_roots(policy)
    if root not in allowed_roots:
        return _error(
            "workspace_out_of_scope",
            "Code Memory v0 only ingests explicitly allowed Hermes repository roots",
            path=str(root),
            reason="not_allowed_root",
        )
    return {"success": True}


def resolve_workspace_path(
    workspace: str | Path,
    *,
    cwd: str | Path | None = None,
    workdir: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve an explicit local workspace path, failing closed on ambiguity.

    Accepted inputs:
    - absolute local directory path;
    - relative path that remains under verified ``cwd``/``workdir``;
    - ``auto`` only when one unambiguous local directory can be verified from
      cwd/workdir/HERMES_KANBAN_WORKSPACE/HERMES_CLI_WORKSPACE.
    """

    raw = str(workspace).strip()
    if not raw:
        return _error("workspace_required", "workspace path is required")
    if raw == "auto":
        auto = _candidate_from_auto(cwd, workdir)
        if auto is None:
            return _error("workspace_unverifiable", "auto workspace could not be verified on a single local filesystem")
        return {"success": True, "path": str(auto), "mode": "auto"}

    base = None
    if cwd is not None:
        try:
            base = Path(cwd).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return _error("cwd_unverifiable", "cwd could not be verified", cwd=_structured_path(cwd), reason=type(exc).__name__)
    elif workdir is not None:
        try:
            base = Path(workdir).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return _error(
                "workdir_unverifiable", "workdir could not be verified", workdir=_structured_path(workdir), reason=type(exc).__name__
            )

    input_path = Path(raw).expanduser()
    if input_path.is_absolute():
        candidate = input_path
    else:
        if base is None:
            return _error("workspace_unverifiable", "relative workspace requires a verified cwd or workdir")
        if ".." in input_path.parts:
            return _error("path_traversal", "workspace path may not traverse above the verified base")
        candidate = base / input_path

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return _error("workspace_not_found", "workspace path does not exist", path=_structured_path(candidate))
    except (OSError, RuntimeError) as exc:
        return _error("workspace_unverifiable", "workspace path could not be verified", path=_structured_path(candidate), reason=type(exc).__name__)
    if not resolved.is_dir():
        return _error("workspace_not_directory", "workspace path is not a directory", path=str(resolved))
    if base is not None and not input_path.is_absolute() and not _is_relative_to(resolved, base):
        return _error("path_traversal", "workspace path resolved outside the verified base")
    return {"success": True, "path": str(resolved), "mode": "absolute" if input_path.is_absolute() else "relative"}


class CodeMemoryIndexer:
    """Incremental safe indexer for verified local Code Memory workspaces."""

    def __init__(self, *, storage: CodeMemoryStorage | None = None, policy: CodeMemoryPolicy | None = None) -> None:
        self.storage = storage or CodeMemoryStorage()
        self.policy = policy or CodeMemoryPolicy()

    def ingest(
        self,
        *,
        workspace: str | Path = "auto",
        cwd: str | Path | None = None,
        workdir: str | Path | None = None,
    ) -> dict[str, Any]:
        if self.policy.include_gitignored:
            return _error("include_gitignored_disabled", "include_gitignored is hard-disabled for Code Memory v0")
        resolved = resolve_workspace_path(workspace, cwd=cwd, workdir=workdir)
        if not resolved["success"]:
            return resolved
        root = Path(resolved["path"])
        scope = enforce_workspace_scope(root, self.policy)
        if not scope["success"]:
            return scope
        init = self.storage.initialize_workspace(root, config=self.policy.public_dict())
        if not init["success"]:
            return init
        workspace_id = init["workspace"]["id"]
        db_path = init["db_path"]
        previous = self._active_source_hashes(workspace_id)
        seen_active: set[str] = set()
        indexed_paths: list[str] = []
        skipped: dict[str, int] = {}
        counts = {"indexed": 0, "updated": 0, "unchanged": 0, "skipped": 0, "deleted": 0}
        warnings: list[str] = []
        total_bytes = 0
        git_commit = _git_value(root, "rev-parse", "HEAD")

        for path in self._walk(root):
            rel_path = path.relative_to(root).as_posix()
            decision = self._classify(root, path, rel_path, total_bytes)
            reason = decision.get("skip_reason")
            size = int(decision.get("size_bytes", 0))
            if reason:
                counts["skipped"] += 1
                skipped[reason] = skipped.get(reason, 0) + 1
                self._record_skip(workspace_id, rel_path, reason, size)
                continue
            content = decision["content"]
            content_bytes = decision["content_bytes"]
            total_bytes += len(content_bytes)
            content_sha = _sha256_bytes(content_bytes)
            existing = previous.get(rel_path)
            seen_active.add(rel_path)
            if existing and existing["content_sha256"] == content_sha and existing.get("mtime_ns") == path.stat().st_mtime_ns:
                counts["unchanged"] += 1
                indexed_paths.append(rel_path)
                continue
            source = self.storage.upsert_source_file(
                workspace_id=workspace_id,
                rel_path=rel_path,
                language=self._language(path),
                mime=mimetypes.guess_type(path.name)[0] or "text/plain",
                size_bytes=len(content_bytes),
                content_sha256=content_sha,
                git_blob_sha=_git_blob(root, path),
                git_commit=git_commit,
                mtime_ns=path.stat().st_mtime_ns,
                skip_reason=None,
            )
            if not source["success"]:
                warnings.append(f"failed_to_index:{rel_path}:{source['error']['code']}")
                continue
            self.storage.upsert_chunk(
                workspace_id=workspace_id,
                source_file_id=source["source_file"]["id"],
                chunk_kind=self._chunk_kind(path),
                content=content,
                content_sha256=content_sha,
                start_line=1,
                end_line=max(1, content.count("\n") + (0 if content.endswith("\n") else 1)),
                tags=["code_memory_v0", self._language(path) or "text"],
                chunk_id=_sha256_text(f"{workspace_id}:{rel_path}:fallback_window")[:32],
            )
            indexed_paths.append(rel_path)
            if existing:
                counts["updated"] += 1
            else:
                counts["indexed"] += 1

        for rel_path in sorted(set(previous) - seen_active):
            deleted = self.storage.delete_source_file(workspace_id=workspace_id, rel_path=rel_path)
            if deleted.get("success"):
                counts["deleted"] += int(deleted.get("deleted", {}).get("source_files", 0))

        self._mark_indexed(workspace_id)
        status = self.status(workspace_id=workspace_id)
        return {
            "success": True,
            "workspace": status.get("workspace", init["workspace"]),
            "db_path": db_path,
            "counts": counts,
            "skip_reasons": skipped,
            "indexed_paths": sorted(indexed_paths),
            "policy": self.policy.public_dict(),
            "last_indexed_at": status.get("last_indexed_at"),
            "warnings": warnings,
        }

    def status(self, *, workspace_id: str) -> dict[str, Any]:
        conn = self.storage._connect_workspace(workspace_id)
        try:
            workspace = self.storage._workspace_row(conn, workspace_id)
            if workspace is None:
                return _error("workspace_not_initialized", "Workspace has not been initialized", workspace_id=workspace_id)
            db_path = self.storage._resolve_db_path(workspace_id)
            active = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM source_files WHERE workspace_id = ? AND deleted_at IS NULL AND skip_reason IS NULL",
                (workspace_id,),
            ).fetchone()
            skipped = conn.execute(
                "SELECT COUNT(*) FROM source_files WHERE workspace_id = ? AND deleted_at IS NULL AND skip_reason IS NOT NULL",
                (workspace_id,),
            ).fetchone()[0]
            deleted = conn.execute(
                "SELECT COUNT(*) FROM source_files WHERE workspace_id = ? AND deleted_at IS NOT NULL",
                (workspace_id,),
            ).fetchone()[0]
            stale = self._count_stale(workspace)
            skip_rows = conn.execute(
                "SELECT skip_reason, COUNT(*) FROM source_files WHERE workspace_id = ? AND deleted_at IS NULL AND skip_reason IS NOT NULL GROUP BY skip_reason",
                (workspace_id,),
            ).fetchall()
            policy = self.policy.public_dict()
            try:
                stored_policy = __import__("json").loads(workspace.get("config_json") or "{}")
                if stored_policy:
                    policy.update(stored_policy)
            except Exception:
                pass
            return {
                "success": True,
                "workspace_id": workspace_id,
                "workspace": workspace,
                "root_path_hash": workspace["root_path_hash"],
                "last_indexed_at": workspace["last_indexed_at"],
                "db": {"path": str(db_path), "size_bytes": db_path.stat().st_size if db_path.exists() else 0},
                "counts": {
                    "active_files": int(active[0]),
                    "active_bytes": int(active[1]),
                    "skipped_files": int(skipped),
                    "deleted_files": int(deleted),
                    "stale_files": int(stale),
                },
                "skip_reasons": {row[0]: row[1] for row in skip_rows},
                "policy": policy,
                "freshness": "fresh" if stale == 0 else "stale",
                "warnings": [] if policy.get("local_only") and not policy.get("include_gitignored") else ["unsafe_policy"],
            }
        finally:
            conn.close()

    def _walk(self, root: Path) -> Iterable[Path]:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [name for name in dirnames if name not in _DENYLIST_DIRS]
            for filename in sorted(filenames):
                yield Path(dirpath) / filename

    def _classify(self, root: Path, path: Path, rel_path: str, total_bytes: int) -> dict[str, Any]:
        try:
            stat = path.lstat()
        except OSError:
            return {"skip_reason": "unreadable", "size_bytes": 0}
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except OSError:
                return {"skip_reason": "symlink_escape", "size_bytes": 0}
            return {"skip_reason": "symlink_escape" if not _is_relative_to(target, root) else "symlink", "size_bytes": 0}
        size = stat.st_size
        if self._is_gitignored(root, rel_path):
            return {"skip_reason": "gitignored", "size_bytes": size}
        if self._is_secret_like(rel_path):
            return {"skip_reason": "secret_like_path", "size_bytes": size}
        try:
            data = path.read_bytes()
        except OSError:
            return {"skip_reason": "unreadable", "size_bytes": size}
        if self._is_binary(data):
            return {"skip_reason": "binary", "size_bytes": size}
        if path.suffix.lower() not in set(self.policy.allowed_extensions):
            return {"skip_reason": "unsupported_extension", "size_bytes": size}
        if size > self.policy.max_file_bytes:
            return {"skip_reason": "too_large", "size_bytes": size}
        if total_bytes + size > self.policy.max_total_bytes:
            return {"skip_reason": "aggregate_limit", "size_bytes": size}
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"skip_reason": "binary", "size_bytes": size}
        if self._is_generated_or_minified(path, content):
            return {"skip_reason": "generated_or_minified", "size_bytes": size}
        return {"content": content, "content_bytes": data, "size_bytes": size}

    def _record_skip(self, workspace_id: str, rel_path: str, reason: str, size_bytes: int) -> None:
        self.storage.upsert_source_file(
            workspace_id=workspace_id,
            rel_path=rel_path,
            language=None,
            mime=None,
            size_bytes=max(0, size_bytes),
            content_sha256=_sha256_text(f"skip:{rel_path}:{reason}:{size_bytes}"),
            mtime_ns=None,
            skip_reason=reason,
        )

    def _active_source_hashes(self, workspace_id: str) -> dict[str, dict[str, Any]]:
        conn = self.storage._connect_workspace(workspace_id)
        try:
            rows = conn.execute(
                "SELECT rel_path, content_sha256, mtime_ns FROM source_files WHERE workspace_id = ? AND deleted_at IS NULL AND skip_reason IS NULL",
                (workspace_id,),
            ).fetchall()
            return {row["rel_path"]: dict(row) for row in rows}
        finally:
            conn.close()

    def _mark_indexed(self, workspace_id: str) -> None:
        conn = self.storage._connect_workspace(workspace_id)
        try:
            conn.execute("UPDATE workspaces SET last_indexed_at = ? WHERE id = ?", (_now_iso(), workspace_id))
            conn.commit()
        finally:
            conn.close()

    def _count_stale(self, workspace: dict[str, Any]) -> int:
        root = Path(workspace["root_path"])
        workspace_id = workspace["id"]
        conn = self.storage._connect_workspace(workspace_id)
        try:
            count = 0
            rows = conn.execute(
                "SELECT rel_path, content_sha256 FROM source_files WHERE workspace_id = ? AND deleted_at IS NULL AND skip_reason IS NULL",
                (workspace_id,),
            ).fetchall()
            for row in rows:
                path = root / row["rel_path"]
                if not path.exists():
                    count += 1
                    continue
                try:
                    if _sha256_bytes(path.read_bytes()) != row["content_sha256"]:
                        count += 1
                except OSError:
                    count += 1
            return count
        finally:
            conn.close()

    @staticmethod
    def _is_binary(data: bytes) -> bool:
        return b"\x00" in data[:8192]

    @staticmethod
    def _is_secret_like(rel_path: str) -> bool:
        parts = [part.lower() for part in Path(rel_path).parts]
        name = parts[-1]
        normalized = name.replace("-", "_")
        return any(part in _SECRET_PATH_PARTS for part in parts) or any(token in normalized for token in _SECRET_SUBSTRINGS)

    @staticmethod
    def _is_generated_or_minified(path: Path, content: str) -> bool:
        name = path.name.lower()
        if ".min." in name or any(token in name for token in _GENERATED_SUBSTRINGS):
            return True
        if "@generated" in content[:2048].lower() or "do not edit" in content[:2048].lower():
            return True
        lines = content.splitlines() or [content]
        if len(lines) <= 2 and any(len(line) > 500 for line in lines):
            return True
        return False

    @staticmethod
    def _language(path: Path) -> str | None:
        mapping = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".md": "markdown",
            ".rst": "rst",
            ".json": "json",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".toml": "toml",
            ".sh": "shell",
        }
        return mapping.get(path.suffix.lower())

    @staticmethod
    def _chunk_kind(path: Path) -> str:
        if path.suffix.lower() in {".md", ".rst", ".txt"}:
            return "doc"
        if path.suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}:
            return "config"
        return "fallback_window"

    @staticmethod
    def _is_gitignored(root: Path, rel_path: str) -> bool:
        if not (root / ".git").exists():
            return False
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-q", "--", rel_path],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0


def _git_value(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args], check=False, capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _git_blob(root: Path, path: Path) -> str | None:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return None
    return _git_value(root, "hash-object", "--", rel)
