"""Durable archival of task workspaces before cleanup (rework program P0).

The Mission Engine's evidence contract requires reviewers (and, downstream,
ReadyBench receipts) to be able to open the artifacts a task cited — but the
completion path historically ``rmtree``'d scratch workspaces, destroying 87%
of cited evidence paths.  This module makes archival a *precondition* of
deletion: :func:`archive_workspace` writes a sha256 manifest plus a tarball
under the hermes-runtime backup repo, verifies what it wrote, and only a
verified archive licenses the caller to delete the workspace.

Fail-safe contract:

* Archive succeeds and verifies  -> caller may delete the workspace.
* Archive fails for ANY reason   -> caller must NOT delete (fail-closed:
  a silent archive failure makes deletion impossible, not evidence loss
  possible).
* ``HERMES_ARCHIVE_ON_COMPLETE=0`` (or ``false``/``off``) -> legacy behavior
  (delete without archiving) — the burn-in kill-switch.

Layout under the archive root (default ``~/.hermes/hermes-runtime/
mission-artifacts``, override via ``HERMES_MISSION_ARTIFACTS_ROOT``)::

    <root>/<board>/<task_id>/manifest.json
    <root>/<board>/<task_id>/workspace.tar.gz   (omitted when 0 files)

The manifest records per-file relative path, size, and sha256, plus the
tarball's own sha256 — enough for a third party to verify the archived bytes
without trusting the engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tarfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional

_log = logging.getLogger(__name__)

#: Kill-switch env var — truthy-off values revert to legacy delete-without-archive.
ARCHIVE_ENV_KILL = "HERMES_ARCHIVE_ON_COMPLETE"
#: Override for the archive root directory (tests / operators).
ARCHIVE_ROOT_ENV = "HERMES_MISSION_ARTIFACTS_ROOT"

_OFF_VALUES = {"0", "false", "off", "no"}

MANIFEST_NAME = "manifest.json"
TARBALL_NAME = "workspace.tar.gz"


def archive_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return False only when the kill-switch explicitly disables archival."""
    if env is None:
        env = os.environ
    raw = (env.get(ARCHIVE_ENV_KILL) or "").strip().lower()
    return raw not in _OFF_VALUES


def default_archive_root(home: Path, env: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve the archive root: env override, else hermes-runtime subdir."""
    if env is None:
        env = os.environ
    raw = (env.get(ARCHIVE_ROOT_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(home) / "hermes-runtime" / "mission-artifacts"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_files(workspace: Path) -> list[dict[str, Any]]:
    """Walk regular files under ``workspace`` (sorted, symlinks skipped)."""
    entries: list[dict[str, Any]] = []
    for p in sorted(workspace.rglob("*")):
        try:
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(workspace).as_posix()
            entries.append({"path": rel, "size": p.stat().st_size, "sha256": _sha256_file(p)})
        except OSError:
            # Unreadable file: fail the whole archive rather than silently
            # omitting evidence — the caller then refuses to delete.
            raise
    return entries


def archive_workspace(
    board_slug: str,
    task_id: str,
    workspace_path: Path | str,
    *,
    home: Path,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Archive ``workspace_path`` for ``task_id``; True only on verified success.

    Never raises — every failure is logged and returned as ``False`` so the
    caller's fail-closed branch (skip deletion) engages.
    """
    try:
        workspace = Path(workspace_path)
        if not workspace.is_dir():
            _log.warning("mission-artifacts: workspace missing for %s: %s", task_id, workspace)
            return False
        root = default_archive_root(Path(home), env)
        dest = root / (board_slug or "unknown-board") / task_id
        dest.mkdir(parents=True, exist_ok=True)

        files = _collect_files(workspace)
        manifest: dict[str, Any] = {
            "version": 1,
            "task_id": task_id,
            "board": board_slug,
            "source_path": str(workspace),
            "archived_at": int(time.time()),
            "file_count": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "files": files,
            "tarball": None,
        }

        if files:
            tar_path = dest / TARBALL_NAME
            with tarfile.open(tar_path, "w:gz") as tf:
                for f in files:
                    tf.add(workspace / f["path"], arcname=f["path"], recursive=False)
            # Verify: member count must match the manifest exactly.
            with tarfile.open(tar_path, "r:gz") as tf:
                members = [m for m in tf.getmembers() if m.isfile()]
            if len(members) != len(files):
                _log.warning(
                    "mission-artifacts: tarball verification FAILED for %s: %d members != %d files",
                    task_id, len(members), len(files),
                )
                return False
            manifest["tarball"] = {
                "name": TARBALL_NAME,
                "size": tar_path.stat().st_size,
                "sha256": _sha256_file(tar_path),
            }

        tmp = dest / (MANIFEST_NAME + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        os.replace(tmp, dest / MANIFEST_NAME)
        # Read-back verification: the manifest we just wrote must parse.
        json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
        _log.info(
            "mission-artifacts: archived %s (%d files, %d bytes) -> %s",
            task_id, manifest["file_count"], manifest["total_bytes"], dest,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — fail-closed boundary
        _log.warning("mission-artifacts: archive FAILED for %s: %s", task_id, exc)
        return False
