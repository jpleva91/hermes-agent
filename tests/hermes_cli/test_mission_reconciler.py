"""Tests for scripts/mission_reconciler.py (Mission Engine rework P4).

Fixture board schema: the real kanban schema is created through
``kanban_db.connect(db_path=...)`` on a tmp_path DB (no live ~/.hermes
reads), then extended with the rework schema contract's ``task_verdicts``
and ``defect_fingerprints`` tables plus the contract's new ``tasks``
columns (``wake_deadline``, ``needs_work_count``).
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPT = REPO_ROOT / "scripts" / "mission_reconciler.py"

_spec = importlib.util.spec_from_file_location("mission_reconciler_under_test", SCRIPT)
recon = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = recon
_spec.loader.exec_module(recon)

from hermes_cli import kanban_db as kb  # noqa: E402

NOW = int(time.time())

#: Rework schema contract (REWORK-SPEC.md) — tables the orchestrator is
#: adding to kanban_db; the reconciler must code against them.
CONTRACT_TABLES = """
CREATE TABLE IF NOT EXISTS task_verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  run_id INTEGER,
  target_task_id TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('APPROVE','NEEDS_WORK','REJECT','WAIVED')),
  tier TEXT,
  evidence_manifest TEXT,
  reviewer_model TEXT,
  cross_model INTEGER NOT NULL DEFAULT 0,
  waive_authority TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS defect_fingerprints (
  lineage_root TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  card_id TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(lineage_root, fingerprint)
);
"""


@pytest.fixture()
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_LIVENESS_BEATS", "0")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_RECONCILER_DISABLE", raising=False)
    db_path = home / "kanban" / "boards" / "recon-test" / "kanban.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = kb.connect(db_path=db_path)
    conn.executescript(CONTRACT_TABLES)
    for ddl in (
        "ALTER TABLE tasks ADD COLUMN wake_deadline INTEGER",
        "ALTER TABLE tasks ADD COLUMN needs_work_count INTEGER DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # orchestrator already added it to SCHEMA_SQL
    conn.commit()
    yield conn, db_path
    conn.close()


def add_task(
    conn,
    tid,
    *,
    status="ready",
    title="Build widget",
    body=None,
    assignee="runtimesteward",
    created_by="missioncommander",
    created_at=None,
    completed_at=None,
):
    conn.execute(
        "INSERT INTO tasks(id, title, body, assignee, status, created_by, created_at, completed_at, priority)"
        " VALUES(?,?,?,?,?,?,?,?,0)",
        (tid, title, body, assignee, status, created_by, created_at or NOW - 7200, completed_at),
    )
    conn.commit()


def block_review_required(conn, tid, at):
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    conn.execute(
        "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES(?, 'blocked', ?, ?)",
        (tid, json.dumps({"reason": "review-required: evidence posted", "kind": "needs_input"}), at),
    )
    conn.commit()


def task_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]


# ---------------------------------------------------------------------------
# Required contract tests
# ---------------------------------------------------------------------------

def test_structured_approve_planned_and_applied(board):
    conn, _ = board
    add_task(conn, "t_aaa1")
    block_review_required(conn, "t_aaa1", NOW - 3600)
    conn.execute(
        "INSERT INTO task_verdicts(task_id, target_task_id, verdict, created_at)"
        " VALUES('t_11112222', 't_aaa1', 'APPROVE', ?)",
        (NOW - 60,),
    )
    conn.commit()

    plan = recon.build_plan(conn, now=NOW, board="recon-test")
    approve = [a for a in plan["actions"] if a["type"] == "apply-APPROVE"]
    assert len(approve) == 1
    action = approve[0]
    assert action["target"] == "t_aaa1"
    assert action["verdict_source"] == "task_verdicts"
    assert action["legacy_source"] is False

    applied = recon.apply_plan(conn, plan, now=NOW)
    done = [a for a in applied["actions"] if a["type"] == "apply-APPROVE"]
    assert done[0]["executed"] is True
    row = conn.execute("SELECT status, result FROM tasks WHERE id = 't_aaa1'").fetchone()
    assert row["status"] == "done"
    assert "APPROVE" in (row["result"] or "")


def test_duplicate_mint_suppressed_by_unique(board):
    conn, _ = board
    add_task(conn, "t_bbb1", title="Ship artifact")
    block_review_required(conn, "t_bbb1", NOW - 3600)

    plan = recon.build_plan(conn, now=NOW)
    mints = [a for a in plan["actions"] if a["type"] == "mint"]
    assert len(mints) == 1
    assert mints[0]["defect_class"] == "review-gate"
    fp = mints[0]["defect_fingerprint"]
    assert fp == recon.defect_fingerprint("t_bbb1", "review-gate", "t_bbb1")

    # Race simulation: a concurrent pass lands the fingerprint after this
    # plan was computed — the UNIQUE violation must suppress the mint.
    conn.execute(
        "INSERT INTO defect_fingerprints(lineage_root, fingerprint, card_id, created_at)"
        " VALUES('t_bbb1', ?, 't_deadbeef', ?)",
        (fp, NOW - 100),
    )
    conn.commit()
    before = task_count(conn)
    applied = recon.apply_plan(conn, plan, now=NOW)
    suppressed = [a for a in applied["actions"] if a["type"] == "suppressed-duplicate"]
    assert len(suppressed) == 1
    assert suppressed[0]["executed"] is False
    assert task_count(conn) == before  # never mint on suppression

    # A fresh plan now reports suppressed-duplicate + escalates as a stall.
    plan2 = recon.build_plan(conn, now=NOW)
    types = {a["type"] for a in plan2["actions"]}
    assert "suppressed-duplicate" in types
    assert "mint" not in types
    assert "stall-report" in types


def test_mint_executes_and_records_fingerprint(board):
    conn, _ = board
    add_task(conn, "t_ccc1", title="Produce report")
    block_review_required(conn, "t_ccc1", NOW - 3600)

    plan = recon.build_plan(conn, now=NOW)
    applied = recon.apply_plan(conn, plan, now=NOW)
    minted = [a for a in applied["actions"] if a["type"] == "mint"]
    assert len(minted) == 1 and minted[0]["executed"] is True
    card_id = minted[0]["card_id"]
    card = conn.execute("SELECT * FROM tasks WHERE id = ?", (card_id,)).fetchone()
    assert card["assignee"] == "gatewarden"
    assert card["created_by"] == "missioncommander"
    assert card["status"] == "ready"  # parentless / ready-on-creation
    fp_row = conn.execute(
        "SELECT card_id FROM defect_fingerprints WHERE lineage_root = 't_ccc1'"
    ).fetchone()
    assert fp_row["card_id"] == card_id

    # Level-triggered: next pass sees the open gate and plans nothing new.
    plan2 = recon.build_plan(conn, now=NOW)
    assert not [a for a in plan2["actions"] if a["type"] in ("mint", "suppressed-duplicate")]


def test_circuit_breaker_aborts_minting(board):
    conn, _ = board
    for i in range(25):  # >20/h from a single source trips the breaker
        add_task(conn, f"t_spam{i:02d}", status="done", title=f"spam {i}",
                 created_by="spamsource", created_at=NOW - 300)
    add_task(conn, "t_ddd1", title="Needs review")
    block_review_required(conn, "t_ddd1", NOW - 3600)

    plan = recon.build_plan(conn, now=NOW)
    assert plan["health"]["breaker"]["state"] == "tripped"
    assert "spamsource" in plan["health"]["breaker"]["tripped_sources"]

    before = task_count(conn)
    applied = recon.apply_plan(conn, plan, now=NOW)
    assert "CIRCUIT-BREAKER" in applied["alerts"]
    mint = [a for a in applied["actions"] if a["type"] == "mint"][0]
    assert mint["executed"] is False
    assert mint["reason"] == "CIRCUIT-BREAKER"
    assert task_count(conn) == before
    # A breaker abort must NOT burn the fingerprint's one legitimate attempt.
    n = conn.execute("SELECT COUNT(*) AS n FROM defect_fingerprints").fetchone()["n"]
    assert n == 0


def test_empty_board_prints_actions_and_health(board, capsys):
    conn, db_path = board
    plan = recon.build_plan(conn, now=NOW, board="recon-test")
    assert plan["actions"] == []
    assert plan["health"]["boards_scanned"] == 1
    assert plan["health"]["breaker"]["state"] == "armed"

    # No --quiet-if-empty exists: an empty plan still prints actions+health.
    rc = recon.main(["--db", str(db_path), "--plan"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["actions"] == []
    assert "health" in out
    assert out["mode"] == "plan"


def test_legacy_prose_verdict_flagged(board):
    conn, _ = board
    add_task(conn, "t_eee1", title="Implement feature")
    block_review_required(conn, "t_eee1", NOW - 3600)
    add_task(conn, "t_33334444", status="done", assignee="gatewarden",
             title="Gate review", body="Review blocked build `t_eee1`",
             completed_at=NOW - 60)
    conn.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at)"
        " VALUES('t_33334444', 'gatewarden', 'APPROVE: verified evidence refs', ?)",
        (NOW - 60,),
    )
    conn.commit()

    plan = recon.build_plan(conn, now=NOW)
    approve = [a for a in plan["actions"] if a["type"] == "apply-APPROVE"]
    assert len(approve) == 1
    assert approve[0]["target"] == "t_eee1"
    assert approve[0]["legacy_source"] is True
    assert approve[0]["verdict_source"] == "legacy-prose"
    assert plan["health"]["verdict_sources"]["legacy_prose"] >= 1


# ---------------------------------------------------------------------------
# Reconciler-specific behaviors
# ---------------------------------------------------------------------------

def test_stale_verdict_older_than_block_not_applied(board):
    """A verdict predating the card's latest block belongs to a previous
    generation — acting on it is the old sweep's livelock. Must skip."""
    conn, _ = board
    add_task(conn, "t_fff1")
    block_review_required(conn, "t_fff1", NOW - 600)  # re-blocked AFTER verdict
    conn.execute(
        "INSERT INTO task_verdicts(task_id, target_task_id, verdict, created_at)"
        " VALUES('t_55556666', 't_fff1', 'APPROVE', ?)",
        (NOW - 3600,),
    )
    conn.commit()
    plan = recon.build_plan(conn, now=NOW)
    assert not [a for a in plan["actions"] if a["type"] == "apply-APPROVE"]


def test_structured_needs_work_recycles_card(board):
    conn, _ = board
    add_task(conn, "t_aab2", title="Fix parser")
    block_review_required(conn, "t_aab2", NOW - 3600)
    conn.execute(
        "INSERT INTO task_verdicts(task_id, target_task_id, verdict, created_at)"
        " VALUES('t_77778888', 't_aab2', 'NEEDS_WORK', ?)",
        (NOW - 60,),
    )
    conn.commit()

    plan = recon.build_plan(conn, now=NOW)
    nw = [a for a in plan["actions"] if a["type"] == "apply-NEEDS_WORK"]
    assert len(nw) == 1 and nw[0]["target"] == "t_aab2"

    before = task_count(conn)
    applied = recon.apply_plan(conn, plan, now=NOW)
    assert applied["actions"][0]["executed"] is True
    row = conn.execute("SELECT status FROM tasks WHERE id = 't_aab2'").fetchone()
    assert row["status"] == "ready"  # recycled, not re-minted
    assert task_count(conn) == before
    comments = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = 't_aab2'"
    ).fetchall()
    assert any("NEEDS_WORK" in c["body"] for c in comments)


def test_wake_overdue_promotes_todo_with_terminal_parents(board):
    conn, _ = board
    add_task(conn, "t_99990000", status="done", title="Parent", completed_at=NOW - 3600)
    add_task(conn, "t_kid00001", status="todo", title="Child work", created_at=NOW - 3600)
    conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES('t_99990000', 't_kid00001')")
    conn.commit()

    plan = recon.build_plan(conn, now=NOW)
    wake = [a for a in plan["actions"] if a["type"] == "wake-overdue"]
    assert len(wake) == 1 and wake[0]["target"] == "t_kid00001"

    recon.apply_plan(conn, plan, now=NOW)
    row = conn.execute("SELECT status FROM tasks WHERE id = 't_kid00001'").fetchone()
    assert row["status"] == "ready"


def test_wake_deadline_stall_report(board):
    conn, _ = board
    add_task(conn, "t_stall01", title="Waiting on operator")
    conn.execute("UPDATE tasks SET status = 'blocked', wake_deadline = ? WHERE id = 't_stall01'", (NOW - 500,))
    conn.execute(
        "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES(?, 'blocked', ?, ?)",
        ("t_stall01", json.dumps({"reason": "waiting on sudo", "kind": "capability"}), NOW - 7200),
    )
    conn.commit()

    plan = recon.build_plan(conn, now=NOW)
    stalls = [a for a in plan["actions"] if a["type"] == "stall-report"]
    assert len(stalls) == 1
    assert stalls[0]["target"] == "t_stall01"
    assert stalls[0]["deadline_basis"] == "wake_deadline"
    assert stalls[0]["overdue_seconds"] >= 500
    # stall-report is report-only, even under --apply
    applied = recon.apply_plan(conn, plan, now=NOW)
    row = conn.execute("SELECT status FROM tasks WHERE id = 't_stall01'").fetchone()
    assert row["status"] == "blocked"
    assert applied["actions"][0]["report_only"] is True


def test_shadow_compare_agrees_on_review_gate(board):
    conn, _ = board
    add_task(conn, "t_bbb2", title="Deliver module")
    block_review_required(conn, "t_bbb2", NOW - 3600)
    plan = recon.build_plan(conn, now=NOW)
    shadow = recon.shadow_compare(conn, plan)
    assert shadow["available"] is True
    assert {"intent": "review-gate", "lineage_root": "t_bbb2"} in shadow["both"]


def test_disable_env_kill_switch(board, monkeypatch, capsys):
    _, db_path = board
    monkeypatch.setenv("HERMES_RECONCILER_DISABLE", "1")
    rc = recon.main(["--db", str(db_path), "--apply"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["actions"] == []
    assert out["health"]["disabled"] is True
