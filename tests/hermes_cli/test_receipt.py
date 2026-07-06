"""Rework-program P6: verifiable receipt export for agent labor.

Pins the honesty guarantee and the signing contract:

* ``provenance == "verified"`` requires BOTH an APPROVE verdict row AND a
  passing on-disk sha256 check of the evidence archive — either alone is
  ``legacy_prose``;
* sign -> verify roundtrips; a tampered receipt fails ``--verify``;
* a tampered tarball flips provenance to ``legacy_prose``;
* keys are generated on first use into the (tmp) home with 0600 perms and
  the pubkey is published to ``hermes-runtime/receipt-signing.pub``;
* missing ``task_verdicts`` table / missing optional columns are tolerated.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest

from hermes_cli import mission_artifacts as ma
from hermes_cli import receipt as rc

BOARD = "receipt-test-board"
TASK = "t_receipt01"
SALT = "a3f9c2"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated Hermes home; receipt.py must never touch the live ~/.hermes."""
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(h))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_MISSION_ARTIFACTS_ROOT", raising=False)
    monkeypatch.delenv(rc.SIGNING_ENV_KILL, raising=False)
    monkeypatch.delenv(rc.KEY_DIR_ENV, raising=False)
    monkeypatch.delenv(rc.MECHANISM_ENV, raising=False)
    return h


def _make_board(home: Path, slug: str = BOARD, *, salt: str | None = SALT,
                with_verdict_table: bool = True, minimal_columns: bool = False) -> Path:
    """Create a fixture board DB with the contract's minimal tables."""
    bdir = home / "kanban" / "boards" / slug
    bdir.mkdir(parents=True, exist_ok=True)
    meta = {"slug": slug, "name": slug, "mission_board": True}
    if salt:
        meta["board_salt"] = salt
    (bdir / "board.json").write_text(json.dumps(meta), encoding="utf-8")
    db = bdir / "kanban.db"
    conn = sqlite3.connect(db)
    if minimal_columns:
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                assignee TEXT, created_at INTEGER NOT NULL,
                started_at INTEGER, completed_at INTEGER);
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                profile TEXT, status TEXT NOT NULL, outcome TEXT,
                started_at INTEGER NOT NULL, ended_at INTEGER);
        """)
    else:
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                assignee TEXT, created_at INTEGER NOT NULL,
                started_at INTEGER, completed_at INTEGER,
                side_effect_class TEXT, origin TEXT, mission_id TEXT,
                needs_work_count INTEGER DEFAULT 0);
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                profile TEXT, step_key TEXT, status TEXT NOT NULL, outcome TEXT,
                started_at INTEGER NOT NULL, ended_at INTEGER,
                model TEXT, tokens_in INTEGER, tokens_out INTEGER,
                cost_usd REAL, review_mode TEXT);
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
                created_at INTEGER NOT NULL);
        """)
    if with_verdict_table:
        conn.executescript("""
            CREATE TABLE task_verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL, run_id INTEGER,
                target_task_id TEXT NOT NULL,
                verdict TEXT NOT NULL CHECK (verdict IN
                    ('APPROVE','NEEDS_WORK','REJECT','WAIVED')),
                tier TEXT, evidence_manifest TEXT, reviewer_model TEXT,
                cross_model INTEGER NOT NULL DEFAULT 0,
                waive_authority TEXT, created_at INTEGER NOT NULL);
        """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, created_at, started_at, "
        "completed_at) VALUES (?, 'receipt probe', 'done', 'worker-1', 100, 110, 200)",
        (TASK,),
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, "
        "ended_at) VALUES (?, 'coder', 'done', 'completed', 110, 190)",
        (TASK,),
    )
    if not minimal_columns:
        conn.execute(
            "UPDATE task_runs SET model='gpt-5.5', tokens_in=1000, tokens_out=200, "
            "cost_usd=0.42, review_mode='cross' WHERE task_id=?", (TASK,))
        conn.execute("UPDATE tasks SET side_effect_class='merge', mission_id='m_1' "
                     "WHERE id=?", (TASK,))
        conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) "
                     "VALUES (?, 'rev', 'lgtm', 150)", (TASK,))
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                     "VALUES (?, 'status', '{}', 120)", (TASK,))
    conn.commit()
    conn.close()
    return db


def _make_archive(home: Path, slug: str = BOARD, task_id: str = TASK) -> Path:
    """Build a real P0 archive (manifest + tarball) via mission_artifacts."""
    ws = home / "ws" / task_id
    ws.mkdir(parents=True)
    (ws / "evidence.txt").write_text("verdict evidence\n", encoding="utf-8")
    assert ma.archive_workspace(slug, task_id, ws, home=home)
    return home / "hermes-runtime" / "mission-artifacts" / slug / task_id


def _add_verdict(home: Path, verdict: str = "APPROVE", *, slug: str = BOARD,
                 task_id: str = TASK, evidence_manifest: str | None = None) -> None:
    db = home / "kanban" / "boards" / slug / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO task_verdicts (task_id, run_id, target_task_id, verdict, tier, "
        "evidence_manifest, reviewer_model, cross_model, created_at) "
        "VALUES (?, 1, ?, ?, 'tier2', ?, 'reviewer-model', 1, 160)",
        (task_id, task_id, verdict, evidence_manifest),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Provenance: the honesty guarantee
# --------------------------------------------------------------------------

def test_verified_requires_verdict_and_hash_pass(home):
    _make_board(home)
    _make_archive(home)
    _add_verdict(home, "APPROVE")
    r = rc.build_receipt(TASK, board=BOARD)
    assert r["provenance"] == "verified"
    assert r["verification"]["sha256_ok"] is True
    assert r["verification"]["sha256_failed"] == []
    assert len(r["verification"]["archive_paths_checked"]) == 1


def test_no_verdict_is_legacy_prose_even_with_intact_archive(home):
    _make_board(home)
    _make_archive(home)
    r = rc.build_receipt(TASK, board=BOARD)
    assert r["provenance"] == "legacy_prose"
    assert "no APPROVE verdict" in r["provenance_reason"]
    # The archive itself still verified — provenance stays honest anyway.
    assert r["verification"]["sha256_ok"] is True


def test_non_approve_verdicts_do_not_verify(home):
    _make_board(home)
    _make_archive(home)
    _add_verdict(home, "NEEDS_WORK")
    r = rc.build_receipt(TASK, board=BOARD)
    assert r["provenance"] == "legacy_prose"


def test_verdict_without_any_evidence_is_legacy_prose(home):
    _make_board(home)  # no archive at all
    _add_verdict(home, "APPROVE")
    r = rc.build_receipt(TASK, board=BOARD)
    assert r["provenance"] == "legacy_prose"
    assert "no machine-checkable evidence" in r["provenance_reason"]
    assert r["evidence"]["manifest"] is None


def test_tampered_tarball_flips_provenance(home):
    _make_board(home)
    adir = _make_archive(home)
    _add_verdict(home, "APPROVE")
    tar = adir / ma.TARBALL_NAME
    tar.write_bytes(tar.read_bytes() + b"tampered")
    r = rc.build_receipt(TASK, board=BOARD)
    assert r["provenance"] == "legacy_prose"
    assert r["verification"]["sha256_ok"] is False
    assert r["verification"]["sha256_failed"][0]["error"] == "sha256 mismatch"


def test_verdict_evidence_manifest_entries_are_hashed(home):
    _make_board(home)
    adir = _make_archive(home)
    extra = adir.parent / "cited.txt"
    extra.write_text("cited artifact\n", encoding="utf-8")
    good = hashlib.sha256(b"cited artifact\n").hexdigest()
    _add_verdict(home, "APPROVE", evidence_manifest=json.dumps(
        [{"path": str(extra), "sha256": good}]))
    r = rc.build_receipt(TASK, board=BOARD)
    assert r["provenance"] == "verified"
    assert len(r["verification"]["archive_paths_checked"]) == 2
    # Now break the cited file: provenance must flip.
    extra.write_text("swapped\n", encoding="utf-8")
    r2 = rc.build_receipt(TASK, board=BOARD)
    assert r2["provenance"] == "legacy_prose"


# --------------------------------------------------------------------------
# Tolerance: missing tables / columns (the live DBs predate the schema)
# --------------------------------------------------------------------------

def test_missing_verdict_table_yields_empty_chain(home):
    _make_board(home, with_verdict_table=False)
    _make_archive(home)
    r = rc.build_receipt(TASK, board=BOARD)
    assert r["verdicts"] == []
    assert r["provenance"] == "legacy_prose"


def test_missing_optional_columns_tolerated(home):
    _make_board(home, minimal_columns=True, with_verdict_table=False, salt=None)
    r = rc.build_receipt(TASK, board=BOARD)
    assert r["task"]["title"] == "receipt probe"
    assert "side_effect_class" not in r["task"]
    assert r["runs"][0]["profile"] == "coder"
    assert "model" not in r["runs"][0]
    assert r["comments_count"] == 0 and r["events_count"] == 0
    assert r["lottery"] is None


def test_missing_task_errors(home):
    _make_board(home)
    with pytest.raises(rc.ReceiptError):
        rc.build_receipt("t_nope", board=BOARD)


# --------------------------------------------------------------------------
# Content: runs / counts / lottery
# --------------------------------------------------------------------------

def test_receipt_content_and_lottery(home):
    _make_board(home)
    r = rc.build_receipt(TASK, board=BOARD)
    assert r["task"]["side_effect_class"] == "merge"
    assert r["task"]["mission_id"] == "m_1"
    run = r["runs"][0]
    assert (run["model"], run["tokens_in"], run["tokens_out"], run["cost_usd"]) == \
        ("gpt-5.5", 1000, 200, 0.42)
    assert r["comments_count"] == 1 and r["events_count"] == 1
    lot = r["lottery"]
    digest = hashlib.sha256((TASK + SALT).encode()).hexdigest()
    assert lot["hash"] == digest
    assert lot["threshold"] == 0.20
    assert lot["selected"] == (int(digest, 16) / float(1 << 256) < 0.20)


# --------------------------------------------------------------------------
# Signing: keygen, roundtrip, tamper detection
# --------------------------------------------------------------------------

def test_keygen_into_tmp_home_with_published_pub(home):
    _make_board(home)
    r = rc.sign_receipt(rc.build_receipt(TASK, board=BOARD))
    sig = r["signature"]
    assert sig is not None
    assert sig["mechanism"] in {"cryptography", "minisign", "openssl"}
    assert sig["signature_b64"] and sig["pubkey_b64_or_path"]
    key = home / "receipt-signing" / rc.KEY_NAME
    pub = home / "receipt-signing" / rc.PUB_NAME
    assert key.exists() and pub.exists()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    published = home / "hermes-runtime" / "receipt-signing.pub"
    assert published.read_bytes() == pub.read_bytes()


def test_sign_verify_roundtrip(home, tmp_path):
    _make_board(home)
    _make_archive(home)
    _add_verdict(home, "APPROVE")
    receipt = rc.sign_receipt(rc.build_receipt(TASK, board=BOARD))
    out = rc.write_receipt(receipt, out=str(tmp_path / "r.receipt.json"))
    ok, problems = rc.verify_receipt(out)
    assert ok, problems


def test_tampered_receipt_fails_verify(home, tmp_path):
    _make_board(home)
    receipt = rc.sign_receipt(rc.build_receipt(TASK, board=BOARD))
    out = rc.write_receipt(receipt, out=str(tmp_path / "r.receipt.json"))
    doc = json.loads(out.read_text(encoding="utf-8"))
    doc["task"]["title"] = "forged title"
    out.write_text(json.dumps(doc), encoding="utf-8")
    ok, problems = rc.verify_receipt(out)
    assert not ok
    assert any("signature" in p for p in problems)


def test_verify_fails_when_verified_evidence_tampered_after_signing(home, tmp_path):
    _make_board(home)
    adir = _make_archive(home)
    _add_verdict(home, "APPROVE")
    receipt = rc.sign_receipt(rc.build_receipt(TASK, board=BOARD))
    assert receipt["provenance"] == "verified"
    out = rc.write_receipt(receipt, out=str(tmp_path / "r.receipt.json"))
    tar = adir / ma.TARBALL_NAME
    tar.write_bytes(tar.read_bytes() + b"post-hoc tamper")
    ok, problems = rc.verify_receipt(out)
    assert not ok
    assert any("evidence re-check failed" in p for p in problems)


def test_unsigned_receipt_fails_verify(home, tmp_path, monkeypatch):
    monkeypatch.setenv(rc.SIGNING_ENV_KILL, "0")  # kill-switch: unsigned mode
    _make_board(home)
    receipt = rc.sign_receipt(rc.build_receipt(TASK, board=BOARD))
    assert receipt["signature"] is None
    out = rc.write_receipt(receipt, out=str(tmp_path / "r.receipt.json"))
    ok, problems = rc.verify_receipt(out)
    assert not ok
    assert any("unsigned" in p for p in problems)


# --------------------------------------------------------------------------
# CLI entrypoint
# --------------------------------------------------------------------------

def test_cli_build_then_verify_exit_codes(home, tmp_path, capsys):
    _make_board(home)
    _make_archive(home)
    _add_verdict(home, "APPROVE")
    out = tmp_path / "cli.receipt.json"
    assert rc.main([TASK, "--board", BOARD, "--out", str(out)]) == 0
    stdout = capsys.readouterr().out
    assert "provenance: verified" in stdout
    assert rc.main(["--verify", str(out)]) == 0
    # Tamper -> exit 1 (the third-party check).
    doc = json.loads(out.read_text(encoding="utf-8"))
    doc["provenance"] = "verified_but_forged"
    out.write_text(json.dumps(doc), encoding="utf-8")
    assert rc.main(["--verify", str(out)]) == 1


def test_cli_default_out_dir(home, capsys):
    _make_board(home)
    assert rc.main([TASK, "--board", BOARD]) == 0
    expected = (home / "hermes-runtime" / "receipts" / BOARD /
                f"{TASK}.receipt.json")
    assert expected.exists()
    receipt = json.loads(expected.read_text(encoding="utf-8"))
    assert receipt["provenance"] == "legacy_prose"
