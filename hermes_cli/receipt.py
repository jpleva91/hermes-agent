"""Verifiable receipt export for agent labor (rework program P6).

Assembles a **self-contained, signed receipt JSON** for a kanban task — the
ReadyBench "receipt for agent labor" artifact.  A receipt bundles:

* the task row (title / status / assignee / timestamps, plus
  ``side_effect_class`` / ``mission_id`` etc. when those columns exist);
* its runs from ``task_runs`` (incl. model / token / cost columns when
  present);
* the verdict chain from ``task_verdicts`` (``task_id = ?`` OR
  ``target_task_id = ?``; missing table tolerated -> empty chain);
* the P0 evidence archive manifest (``mission-artifacts/<board>/<task_id>/
  manifest.json``) copied verbatim, plus an **actual on-disk sha256
  verification** of the archived tarball and every verdict evidence entry;
* a board-salt lottery record (``sha256(task_id + board_salt)``, threshold
  0.20) when ``board.json`` carries ``board_salt``;
* comment / event **counts** only — receipts are verdicts + evidence, not
  transcripts.

Honesty guarantee — ``provenance``:

* ``"verified"``   — at least one ``APPROVE`` verdict row exists AND at least
  one evidence hash was actually checked AND **every** checked sha256
  matched the bytes on disk.
* ``"legacy_prose"`` — anything else.  A receipt without machine-checkable
  evidence NEVER claims verification; the two values are never conflated.

Signing (D1 default: automated local ed25519 key).  Mechanism detection
order: the ``cryptography`` package -> the ``minisign`` binary -> openssl
``pkeyutl`` with ed25519 support.  The chosen mechanism is recorded in the
receipt.  Keypair lives at ``<home>/receipt-signing/receipt.key`` (0600) and
``receipt.pub``; the public key is also published to
``<home>/hermes-runtime/receipt-signing.pub``.  The signature covers the
canonical JSON bytes (sorted keys, no whitespace) of everything except the
``signature`` field.

CLI::

    python -m hermes_cli.receipt <task_id> [--board <slug>] [--out <path>]
    python -m hermes_cli.receipt --verify <receipt.json>   # exits 0/1

Env:

* ``HERMES_RECEIPT_SIGNING=0``    — kill-switch: emit unsigned receipts
  (``signature: null``); unsigned receipts always fail ``--verify``.
* ``HERMES_RECEIPT_KEY_DIR``      — override the signing-key directory.
* ``HERMES_RECEIPT_MECHANISM``    — force ``cryptography`` / ``minisign`` /
  ``openssl`` instead of auto-detection.
* ``HERMES_KANBAN_HOME`` / ``HERMES_KANBAN_DB`` — board DB resolution
  overrides (same semantics as :mod:`hermes_cli.kanban_db`).
* ``HERMES_MISSION_ARTIFACTS_ROOT`` — archive root override (shared with
  :mod:`hermes_cli.mission_artifacts`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli import mission_artifacts as _ma

_log = logging.getLogger(__name__)

RECEIPT_VERSION = 1
LOTTERY_THRESHOLD = 0.20

SIGNING_ENV_KILL = "HERMES_RECEIPT_SIGNING"
KEY_DIR_ENV = "HERMES_RECEIPT_KEY_DIR"
MECHANISM_ENV = "HERMES_RECEIPT_MECHANISM"

_OFF_VALUES = {"0", "false", "off", "no"}

KEY_NAME = "receipt.key"
PUB_NAME = "receipt.pub"
PUBLISHED_PUB_RELPATH = Path("hermes-runtime") / "receipt-signing.pub"

#: Task columns copied into the receipt when present (order preserved).
_TASK_FIELDS = (
    "id", "title", "status", "assignee", "priority", "created_by",
    "created_at", "started_at", "completed_at", "result",
    "workspace_kind", "workspace_path", "branch_name",
    "side_effect_class", "origin", "mission_id", "closeout_targets",
    "needs_work_count", "wake_deadline",
)

#: Run columns copied into the receipt when present.
_RUN_FIELDS = (
    "id", "task_id", "profile", "step_key", "status", "outcome",
    "started_at", "ended_at",
    "model", "tokens_in", "tokens_out", "cost_usd", "review_mode",
)

#: Verdict columns copied into the receipt when present.
_VERDICT_FIELDS = (
    "id", "task_id", "run_id", "target_task_id", "verdict", "tier",
    "evidence_manifest", "reviewer_model", "cross_model",
    "waive_authority", "created_at",
)


class ReceiptError(RuntimeError):
    """Raised when a receipt cannot be assembled or signed."""


# --------------------------------------------------------------------------
# Path resolution (env-overridable so tests never touch the live ~/.hermes)
# --------------------------------------------------------------------------

def hermes_home(env: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve the Hermes root (``~/.hermes`` in standard deployments)."""
    if env is None:
        env = os.environ
    raw = (env.get("HERMES_KANBAN_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        from hermes_constants import get_default_hermes_root
        return get_default_hermes_root()
    except Exception:  # pragma: no cover — hermes_constants always importable in-repo
        return Path.home() / ".hermes"


def board_db_path(board: str, env: Optional[Mapping[str, str]] = None) -> Path:
    """Return the board's ``kanban.db`` path (``HERMES_KANBAN_DB`` pins it)."""
    if env is None:
        env = os.environ
    pin = (env.get("HERMES_KANBAN_DB") or "").strip()
    if pin:
        return Path(pin).expanduser()
    home = hermes_home(env)
    if board == "default":
        # Pre-boards back-compat layout (see kanban_db module docstring).
        return home / "kanban.db"
    return home / "kanban" / "boards" / board / "kanban.db"


def board_metadata(board: str, env: Optional[Mapping[str, str]] = None) -> dict:
    """Best-effort ``board.json`` contents; ``{}`` when absent/malformed."""
    home = hermes_home(env)
    path = home / "kanban" / "boards" / board / "board.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _archive_dir(board: str, task_id: str, env: Optional[Mapping[str, str]] = None) -> Path:
    root = _ma.default_archive_root(hermes_home(env), env)
    return root / board / task_id


# --------------------------------------------------------------------------
# DB reading (tolerant: missing tables/columns never crash a receipt)
# --------------------------------------------------------------------------

def _connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ReceiptError(f"board database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    """Run a query; a missing table yields ``[]`` instead of an error."""
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise


def _count(conn: sqlite3.Connection, table: str, task_id: str) -> int:
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _pick(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict:
    """Project ``row`` onto ``fields``, silently skipping absent columns."""
    return {k: row[k] for k in fields if k in row.keys()}


# --------------------------------------------------------------------------
# Evidence hashing
# --------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _evidence_checks(
    manifest: Optional[dict],
    verdicts: list[dict],
    archive_dir: Optional[str],
    archive_root: Optional[str],
) -> list[tuple[str, str]]:
    """Return ``[(abs_path, expected_sha256), ...]`` to verify on disk.

    Sources: the P0 archive manifest's tarball hash, plus every entry of
    every verdict's ``evidence_manifest`` (paths resolved against the
    mission-artifacts root when relative).
    """
    checks: list[tuple[str, str]] = []
    if manifest and isinstance(manifest.get("tarball"), dict) and archive_dir:
        sha = manifest["tarball"].get("sha256")
        name = manifest["tarball"].get("name") or _ma.TARBALL_NAME
        if sha:
            checks.append((str(Path(archive_dir) / name), str(sha)))
    for v in verdicts:
        entries = v.get("evidence_manifest")
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except json.JSONDecodeError:
                entries = None
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict) or not e.get("path") or not e.get("sha256"):
                continue
            p = Path(str(e["path"]))
            if not p.is_absolute() and archive_root:
                p = Path(archive_root) / p
            checks.append((str(p), str(e["sha256"])))
    return checks


def _run_checks(checks: list[tuple[str, str]]) -> dict:
    """Hash every check target; missing files count as failures."""
    checked: list[str] = []
    failed: list[dict] = []
    for path_s, expected in checks:
        checked.append(path_s)
        p = Path(path_s)
        if not p.is_file():
            failed.append({"path": path_s, "expected": expected, "actual": None,
                           "error": "missing"})
            continue
        actual = _sha256_file(p)
        if actual != expected:
            failed.append({"path": path_s, "expected": expected, "actual": actual,
                           "error": "sha256 mismatch"})
    return {
        "archive_paths_checked": checked,
        "sha256_ok": bool(checked) and not failed,
        "sha256_failed": failed,
    }


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------

def _signing_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    if env is None:
        env = os.environ
    return (env.get(SIGNING_ENV_KILL) or "").strip().lower() not in _OFF_VALUES


def _have_cryptography() -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
        return True
    except Exception:
        return False


def _have_openssl_ed25519() -> bool:
    try:
        out = subprocess.run(
            ["openssl", "list", "-public-key-algorithms"],
            capture_output=True, text=True, timeout=15,
        )
        return out.returncode == 0 and "ed25519" in out.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def detect_signing_mechanism(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """First available of ``cryptography`` -> ``minisign`` -> ``openssl``."""
    if env is None:
        env = os.environ
    forced = (env.get(MECHANISM_ENV) or "").strip().lower()
    if forced:
        return forced
    if _have_cryptography():
        return "cryptography"
    if shutil.which("minisign"):
        return "minisign"
    if _have_openssl_ed25519():
        return "openssl"
    return None


def key_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    if env is None:
        env = os.environ
    raw = (env.get(KEY_DIR_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return hermes_home(env) / "receipt-signing"


def _publish_pubkey(pub_path: Path, env: Optional[Mapping[str, str]] = None) -> None:
    """Copy the public key to the published hermes-runtime location."""
    try:
        dest = hermes_home(env) / PUBLISHED_PUB_RELPATH
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pub_path.read_bytes())
    except OSError as exc:  # publication is best-effort; signing still valid
        _log.warning("receipt: could not publish pubkey: %s", exc)


def ensure_keypair(mechanism: str, env: Optional[Mapping[str, str]] = None) -> tuple[Path, Path]:
    """Generate (first use) or reuse the local keypair; returns (key, pub)."""
    kdir = key_dir(env)
    key, pub = kdir / KEY_NAME, kdir / PUB_NAME
    if key.exists() and pub.exists():
        return key, pub
    kdir.mkdir(parents=True, exist_ok=True)
    if mechanism == "cryptography":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        priv = ed25519.Ed25519PrivateKey.generate()
        key.write_bytes(priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        pub.write_bytes(priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
    elif mechanism == "minisign":
        res = subprocess.run(
            ["minisign", "-G", "-f", "-W", "-p", str(pub), "-s", str(key)],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode != 0:
            raise ReceiptError(f"minisign keygen failed: {res.stderr.strip()}")
    elif mechanism == "openssl":
        res = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode != 0:
            raise ReceiptError(f"openssl keygen failed: {res.stderr.strip()}")
        res = subprocess.run(
            ["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode != 0:
            raise ReceiptError(f"openssl pubkey export failed: {res.stderr.strip()}")
    else:
        raise ReceiptError(f"unknown signing mechanism: {mechanism!r}")
    os.chmod(key, 0o600)
    _publish_pubkey(pub, env)
    _log.info("receipt: generated %s keypair at %s", mechanism, kdir)
    return key, pub


def canonical_bytes(receipt: Mapping[str, Any]) -> bytes:
    """Canonical JSON of everything except ``signature`` (sorted, compact)."""
    body = {k: v for k, v in receipt.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sign_bytes(mechanism: str, data: bytes, key: Path, pub: Path) -> tuple[str, str]:
    """Sign ``data``; return ``(signature_b64, pubkey_b64)``."""
    if mechanism == "cryptography":
        from cryptography.hazmat.primitives import serialization
        priv = serialization.load_pem_private_key(key.read_bytes(), password=None)
        sig = priv.sign(data)
        raw_pub = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(sig).decode(), base64.b64encode(raw_pub).decode()
    if mechanism == "minisign":
        with tempfile.TemporaryDirectory() as td:
            msg = Path(td) / "msg"
            sigf = Path(td) / "msg.minisig"
            msg.write_bytes(data)
            res = subprocess.run(
                ["minisign", "-S", "-s", str(key), "-m", str(msg), "-x", str(sigf)],
                capture_output=True, text=True, timeout=30,
            )
            if res.returncode != 0:
                raise ReceiptError(f"minisign sign failed: {res.stderr.strip()}")
            return (base64.b64encode(sigf.read_bytes()).decode(),
                    base64.b64encode(pub.read_bytes()).decode())
    if mechanism == "openssl":
        with tempfile.TemporaryDirectory() as td:
            msg = Path(td) / "msg"
            sigf = Path(td) / "sig"
            msg.write_bytes(data)
            res = subprocess.run(
                ["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin",
                 "-in", str(msg), "-out", str(sigf)],
                capture_output=True, text=True, timeout=30,
            )
            if res.returncode != 0:
                raise ReceiptError(f"openssl sign failed: {res.stderr.strip()}")
            return (base64.b64encode(sigf.read_bytes()).decode(),
                    base64.b64encode(pub.read_bytes()).decode())
    raise ReceiptError(f"unknown signing mechanism: {mechanism!r}")


def _verify_bytes(mechanism: str, data: bytes, signature_b64: str, pubkey_b64: str) -> bool:
    try:
        sig = base64.b64decode(signature_b64)
        pub_raw = base64.b64decode(pubkey_b64)
    except Exception:
        return False
    if mechanism == "cryptography":
        try:
            from cryptography.hazmat.primitives.asymmetric import ed25519
            ed25519.Ed25519PublicKey.from_public_bytes(pub_raw).verify(sig, data)
            return True
        except Exception:  # InvalidSignature, malformed key, missing package
            return False
    if mechanism == "minisign":
        with tempfile.TemporaryDirectory() as td:
            msg, sigf, pubf = Path(td) / "msg", Path(td) / "msg.minisig", Path(td) / "pub"
            msg.write_bytes(data)
            sigf.write_bytes(sig)
            pubf.write_bytes(pub_raw)
            try:
                res = subprocess.run(
                    ["minisign", "-V", "-p", str(pubf), "-m", str(msg), "-x", str(sigf)],
                    capture_output=True, text=True, timeout=30,
                )
                return res.returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
    if mechanism == "openssl":
        with tempfile.TemporaryDirectory() as td:
            msg, sigf, pubf = Path(td) / "msg", Path(td) / "sig", Path(td) / "pub.pem"
            msg.write_bytes(data)
            sigf.write_bytes(sig)
            pubf.write_bytes(pub_raw)
            try:
                res = subprocess.run(
                    ["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(pubf),
                     "-rawin", "-in", str(msg), "-sigfile", str(sigf)],
                    capture_output=True, text=True, timeout=30,
                )
                return res.returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
    return False


def sign_receipt(receipt: dict, env: Optional[Mapping[str, str]] = None) -> dict:
    """Attach the ``signature`` block (or ``None`` when disabled/unavailable)."""
    if env is None:
        env = os.environ
    if not _signing_enabled(env):
        _log.warning("receipt: signing disabled via %s — emitting UNSIGNED receipt",
                     SIGNING_ENV_KILL)
        receipt["signature"] = None
        return receipt
    mechanism = detect_signing_mechanism(env)
    if not mechanism:
        _log.warning("receipt: no ed25519 mechanism available — emitting UNSIGNED "
                     "receipt (it will fail --verify)")
        receipt["signature"] = None
        return receipt
    key, pub = ensure_keypair(mechanism, env)
    sig_b64, pub_b64 = _sign_bytes(mechanism, canonical_bytes(receipt), key, pub)
    receipt["signature"] = {
        "mechanism": mechanism,
        "pubkey_b64_or_path": pub_b64,
        "signature_b64": sig_b64,
        "signed_at": int(time.time()),
    }
    return receipt


# --------------------------------------------------------------------------
# Receipt assembly
# --------------------------------------------------------------------------

def _lottery_record(task_id: str, meta: Mapping[str, Any]) -> Optional[dict]:
    salt = meta.get("board_salt")
    if not salt:
        return None
    digest = hashlib.sha256((task_id + str(salt)).encode("utf-8")).hexdigest()
    value = int(digest, 16) / float(1 << 256)
    return {
        "algorithm": "sha256(task_id + board_salt)",
        "hash": digest,
        "value": value,
        "threshold": LOTTERY_THRESHOLD,
        "selected": value < LOTTERY_THRESHOLD,
    }


def build_receipt(task_id: str, board: str = "default",
                  env: Optional[Mapping[str, str]] = None) -> dict:
    """Assemble the unsigned receipt dict for ``task_id`` on ``board``."""
    if env is None:
        env = os.environ
    db = board_db_path(board, env)
    conn = _connect_ro(db)
    try:
        task_rows = _rows(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task_rows:
            raise ReceiptError(f"task {task_id!r} not found on board {board!r} ({db})")
        task = _pick(task_rows[0], _TASK_FIELDS)
        runs = [
            _pick(r, _RUN_FIELDS)
            for r in _rows(conn, "SELECT * FROM task_runs WHERE task_id = ? "
                                 "ORDER BY started_at, id", (task_id,))
        ]
        verdicts = [
            _pick(v, _VERDICT_FIELDS)
            for v in _rows(conn, "SELECT * FROM task_verdicts WHERE task_id = ? "
                                 "OR target_task_id = ? ORDER BY created_at, id",
                           (task_id, task_id))
        ]
        comments_count = _count(conn, "task_comments", task_id)
        events_count = _count(conn, "task_events", task_id)
    finally:
        conn.close()

    # Evidence: archive manifest copied verbatim when present.
    adir = _archive_dir(board, task_id, env)
    archive_root = _ma.default_archive_root(hermes_home(env), env)
    manifest: Optional[dict] = None
    manifest_path = adir / _ma.MANIFEST_NAME
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("receipt: unreadable manifest for %s: %s", task_id, exc)
            manifest = None

    evidence = {
        "archive_root": str(archive_root),
        "archive_dir": str(adir) if manifest is not None else None,
        "manifest": manifest,
    }

    verification = _run_checks(
        _evidence_checks(manifest, verdicts, evidence["archive_dir"], str(archive_root))
    )

    approved = any(v.get("verdict") == "APPROVE" for v in verdicts)
    hashes_checked = bool(verification["archive_paths_checked"])
    hashes_ok = hashes_checked and not verification["sha256_failed"]
    if approved and hashes_ok:
        provenance, reason = "verified", (
            "APPROVE verdict present and all evidence hashes verified on disk")
    else:
        provenance = "legacy_prose"
        parts = []
        if not approved:
            parts.append("no APPROVE verdict row")
        if not hashes_checked:
            parts.append("no machine-checkable evidence hashes")
        elif verification["sha256_failed"]:
            parts.append(f"{len(verification['sha256_failed'])} evidence hash "
                         f"check(s) failed")
        reason = "; ".join(parts) or "unverified"

    meta = board_metadata(board, env)
    return {
        "receipt_version": RECEIPT_VERSION,
        "generator": "hermes_cli.receipt",
        "generated_at": int(time.time()),
        "board": board,
        "task_id": task_id,
        "task": task,
        "runs": runs,
        "verdicts": verdicts,
        "comments_count": comments_count,
        "events_count": events_count,
        "evidence": evidence,
        "lottery": _lottery_record(task_id, meta),
        "verification": verification,
        "provenance": provenance,
        "provenance_reason": reason,
    }


def default_out_dir(board: str, env: Optional[Mapping[str, str]] = None) -> Path:
    return hermes_home(env) / "hermes-runtime" / "receipts" / board


def write_receipt(receipt: dict, out: Optional[str] = None,
                  env: Optional[Mapping[str, str]] = None) -> Path:
    """Write the (signed) receipt JSON; returns the path written."""
    if out:
        out_path = Path(out).expanduser()
        if out_path.is_dir():
            out_path = out_path / f"{receipt['task_id']}.receipt.json"
    else:
        out_dir = default_out_dir(receipt["board"], env)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{receipt['task_id']}.receipt.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------
# Third-party verification (--verify): signature + evidence re-check
# --------------------------------------------------------------------------

def verify_receipt(path: Path | str) -> tuple[bool, list[str]]:
    """Re-check signature and evidence hashes of a receipt file.

    Returns ``(ok, problems)``.  Rules:

    * the signature must be present and must verify over the canonical
      bytes of the receipt body (any tampering fails);
    * when the receipt claims ``provenance == "verified"``, every recorded
      evidence hash must STILL verify on disk (missing files fail);
    * a ``legacy_prose`` receipt passes on signature alone — it never
      claimed machine-checkable evidence.
    """
    problems: list[str] = []
    try:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"unreadable receipt: {exc}"]
    if not isinstance(receipt, dict):
        return False, ["receipt is not a JSON object"]

    sig = receipt.get("signature")
    if not isinstance(sig, dict):
        problems.append("receipt is unsigned")
    else:
        mechanism = sig.get("mechanism", "")
        ok = _verify_bytes(
            mechanism, canonical_bytes(receipt),
            sig.get("signature_b64", ""), sig.get("pubkey_b64_or_path", ""),
        )
        if not ok:
            problems.append(f"signature verification FAILED (mechanism={mechanism})")

    if receipt.get("provenance") == "verified":
        evidence = receipt.get("evidence") or {}
        checks = _evidence_checks(
            evidence.get("manifest"),
            receipt.get("verdicts") or [],
            evidence.get("archive_dir"),
            evidence.get("archive_root"),
        )
        result = _run_checks(checks)
        if not result["archive_paths_checked"]:
            problems.append("provenance=verified but no evidence hashes recorded")
        for f in result["sha256_failed"]:
            problems.append(f"evidence re-check failed: {f['path']} ({f['error']})")

    return not problems, problems


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hermes_cli.receipt",
        description="Export or verify a signed Mission Engine task receipt (P6).",
    )
    parser.add_argument("task_id", nargs="?", help="task id to export a receipt for")
    parser.add_argument("--board", default="default", help="board slug (default: default)")
    parser.add_argument("--out", default=None, help="output file or directory")
    parser.add_argument("--verify", metavar="RECEIPT_JSON", default=None,
                        help="verify an existing receipt instead of exporting")
    args = parser.parse_args(argv)

    if args.verify:
        ok, problems = verify_receipt(args.verify)
        if ok:
            print(f"OK: {args.verify} — signature and evidence verified")
            return 0
        print(f"FAIL: {args.verify}")
        for p in problems:
            print(f"  - {p}")
        return 1

    if not args.task_id:
        parser.error("task_id is required (or use --verify)")
    try:
        receipt = build_receipt(args.task_id, board=args.board)
        sign_receipt(receipt)
        out_path = write_receipt(receipt, out=args.out)
    except ReceiptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sig = receipt.get("signature") or {}
    print(f"receipt: {out_path}")
    print(f"provenance: {receipt['provenance']} ({receipt['provenance_reason']})")
    v = receipt["verification"]
    print(f"verification: {len(v['archive_paths_checked'])} path(s) checked, "
          f"sha256_ok={v['sha256_ok']}, {len(v['sha256_failed'])} failed")
    print(f"signing: {sig.get('mechanism') or 'UNSIGNED'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    raise SystemExit(main())
