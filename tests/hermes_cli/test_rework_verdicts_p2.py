"""Rework P2/P3 core: structured verdicts, wake deadlines, review lane, lottery.

Pins the tournament-approved contracts:
* record_verdict is fail-closed on WAIVED-without-authority (structural, no env);
* APPROVE evidence verification runs WARN-mode by default (would-have-blocked
  event) and raises in enforce mode;
* cross_model is computed from run lane data, never caller-asserted;
* NEEDS_WORK propagation bounces the target to ready, escalating after 3;
* block_task stamps wake_deadline per kind; the dispatch-tick scanner emits
  deduped stall_overdue events;
* review-required blocks route to status='review' on review-lane boards only;
* the completion lottery is deterministic in board_salt.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import mission_guardrail_policy as mgp
from tests.mission_policy_fixtures import write_mission_policy


@pytest.fixture(autouse=True)
def _reset_policy_cache():
    mgp.clear_cache()
    yield
    mgp.clear_cache()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_VERDICT_ENFORCE", raising=False)
    monkeypatch.delenv("HERMES_SIDE_EFFECT_ENFORCE", raising=False)
    kb.init_db()
    write_mission_policy(home)
    return home


def _mk(conn, title="work card", **kw):
    return kb.create_task(conn, title=title, initial_status="running", **kw)


def _events(conn, task_id, kind):
    return conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchall()


# ---------------------------------------------------------------------------
# record_verdict
# ---------------------------------------------------------------------------

def test_waived_requires_authority(kanban_home):
    with kb.connect() as conn:
        t = _mk(conn)
        with pytest.raises(ValueError, match="waive_authority"):
            kb.record_verdict(conn, task_id=t, verdict="WAIVED")
        vid = kb.record_verdict(
            conn, task_id=t, verdict="WAIVED",
            waive_authority="discord:1522982212503470110",
        )
        assert vid > 0


def test_approve_warn_mode_records_would_have_blocked(kanban_home):
    with kb.connect() as conn:
        t = _mk(conn)
        # APPROVE with no evidence: WARN mode writes the row + warn event.
        vid = kb.record_verdict(conn, task_id=t, verdict="APPROVE")
        assert vid > 0
        warns = _events(conn, t, "verdict_evidence_warn")
        assert len(warns) == 1
        assert json.loads(warns[0]["payload"])["would_have_blocked"] is True


def test_approve_enforce_mode_fails_closed(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_VERDICT_ENFORCE", "enforce")
    with kb.connect() as conn:
        t = _mk(conn)
        with pytest.raises(ValueError, match="fail-closed"):
            kb.record_verdict(conn, task_id=t, verdict="APPROVE")


def test_approve_enforce_passes_with_verified_evidence(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_VERDICT_ENFORCE", "enforce")
    evidence = kanban_home / "evidence.txt"
    evidence.write_text("raw output\n", encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    with kb.connect() as conn:
        t = _mk(conn)
        vid = kb.record_verdict(
            conn, task_id=t, verdict="APPROVE",
            evidence_manifest=[{"path": str(evidence), "sha256": digest}],
        )
        assert vid > 0


def test_cross_model_computed_from_run_lanes(kanban_home):
    with kb.connect() as conn:
        target = _mk(conn, title="reviewed work")
        reviewer = _mk(conn, title="review step")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, model)"
            " VALUES (?, 'done', ?, 'claude-opus')", (target, now),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, model)"
            " VALUES (?, 'done', ?, 'gpt-5.5-codex')", (reviewer, now),
        )
        conn.commit()
        kb.record_verdict(
            conn, task_id=reviewer, verdict="NEEDS_WORK", target_task_id=target,
            # caller lies about reviewer_model; cross_model must not care
            reviewer_model="claude-opus",
        )
        row = conn.execute(
            "SELECT cross_model FROM task_verdicts WHERE task_id = ?", (reviewer,)
        ).fetchone()
        assert row["cross_model"] == 1


def test_needs_work_bounces_then_escalates(kanban_home):
    # Review-lane flow (P3): the target cycles running -> review -> ready,
    # never touching the legacy blocked bucket (whose recurrence breaker
    # correctly triages repeat same-kind blocks — separate machinery).
    slug = kb.get_current_board() or kb.DEFAULT_BOARD
    _enable_review_lane(slug)
    with kb.connect() as conn:
        target = _mk(conn, title="bouncy work")
        reviewer = _mk(conn, title="review step")
        for bounce in range(1, 4):
            assert kb.block_task(
                conn, target, reason=f"review-required: round {bounce}", kind=None
            )
            assert kb.get_task(conn, target).status == "review"
            kb.record_verdict(
                conn, task_id=reviewer, verdict="NEEDS_WORK", target_task_id=target
            )
            row = conn.execute(
                "SELECT status, needs_work_count FROM tasks WHERE id = ?", (target,)
            ).fetchone()
            assert row["needs_work_count"] == bounce
            if bounce < kb.NEEDS_WORK_ESCALATION_LIMIT:
                assert row["status"] == "ready"
                conn.execute(
                    "UPDATE tasks SET status = 'running' WHERE id = ?", (target,)
                )
                conn.commit()
            else:
                assert row["status"] in ("blocked", "triage")


def test_approve_propagation_completes_blocked_target(kanban_home):
    with kb.connect() as conn:
        target = _mk(conn, title="finished work")
        reviewer = _mk(conn, title="review step")
        kb.block_task(conn, target, reason="review-required: fixture", kind=None)
        assert kb.get_task(conn, target).status == "blocked"
        kb.record_verdict(
            conn, task_id=reviewer, verdict="APPROVE", target_task_id=target
        )
        assert kb.get_task(conn, target).status == "done"


# ---------------------------------------------------------------------------
# wake deadlines
# ---------------------------------------------------------------------------

def test_block_stamps_wake_deadline_by_kind(kanban_home):
    with kb.connect() as conn:
        t = _mk(conn)
        before = int(time.time())
        kb.block_task(conn, t, reason="waiting on human", kind="needs_input")
        row = conn.execute(
            "SELECT wake_deadline FROM tasks WHERE id = ?", (t,)
        ).fetchone()
        assert row["wake_deadline"] is not None
        assert row["wake_deadline"] >= before + kb.WAKE_DEADLINE_DEFAULTS["needs_input"] - 5


def test_scan_wake_deadlines_emits_deduped_stall_events(kanban_home):
    with kb.connect() as conn:
        t = _mk(conn)
        kb.block_task(conn, t, reason="waiting", kind="needs_input")
        conn.execute(
            "UPDATE tasks SET wake_deadline = ? WHERE id = ?",
            (int(time.time()) - 10, t),
        )
        conn.commit()
        assert kb._scan_wake_deadlines(conn) == 1
        assert kb._scan_wake_deadlines(conn) == 0  # deduped within window
        assert len(_events(conn, t, "stall_overdue")) == 1


# ---------------------------------------------------------------------------
# review lane (P3)
# ---------------------------------------------------------------------------

def _enable_review_lane(slug, salt=None):
    meta_path = kb.board_metadata_path(slug)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    meta["review_lane"] = True
    if salt:
        meta["board_salt"] = salt
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def test_review_required_routes_to_review_status_on_flagged_board(kanban_home):
    slug = kb.get_current_board() or kb.DEFAULT_BOARD
    _enable_review_lane(slug)
    with kb.connect() as conn:
        t = _mk(conn, title="gated work")
        assert kb.block_task(conn, t, reason="review-required: evidence at x", kind=None)
        task = kb.get_task(conn, t)
        assert task.status == "review"
        assert len(_events(conn, t, "review_requested")) == 1


def test_review_required_stays_blocked_without_flag(kanban_home):
    with kb.connect() as conn:
        t = _mk(conn, title="gated work legacy")
        assert kb.block_task(conn, t, reason="review-required: evidence", kind=None)
        assert kb.get_task(conn, t).status == "blocked"


# ---------------------------------------------------------------------------
# lottery (P3, C-graft)
# ---------------------------------------------------------------------------

def test_lottery_mints_deterministically(kanban_home):
    slug = kb.get_current_board() or kb.DEFAULT_BOARD
    salt = "fixed-test-salt"
    _enable_review_lane(slug, salt=salt)
    with kb.connect() as conn:
        # find one winning and one losing id by simulating draws
        def selected(task_id):
            draw = int(hashlib.sha256(f"{task_id}{salt}".encode()).hexdigest(), 16)
            return draw / float(1 << 256) < 0.20
        winner = loser = None
        for _ in range(200):
            t = _mk(conn, title="lottery probe")
            if selected(t) and winner is None:
                winner = t
            elif not selected(t) and loser is None:
                loser = t
            else:
                kb.complete_task(conn, t, result="filler")
            if winner and loser:
                break
        assert winner and loser
        kb.complete_task(conn, winner, result="done")
        kb.complete_task(conn, loser, result="done")
        audits = conn.execute(
            "SELECT id, title FROM tasks WHERE origin IS NULL AND title LIKE 'Lottery audit:%'"
        ).fetchall()
        audit_events_w = _events(conn, winner, "lottery_audit_minted")
        audit_events_l = _events(conn, loser, "lottery_audit_minted")
        assert len(audit_events_w) == 1
        assert len(audit_events_l) == 0
        assert any("Lottery audit:" in a["title"] for a in audits)


# ---------------------------------------------------------------------------
# side-effect floor (P6, warn mode)
# ---------------------------------------------------------------------------

def test_side_effect_completion_warns_without_verdict(kanban_home):
    with kb.connect() as conn:
        t = _mk(conn, title="deploy the thing")
        conn.execute(
            "UPDATE tasks SET side_effect_class = 'deploy' WHERE id = ?", (t,)
        )
        conn.commit()
        assert kb.complete_task(conn, t, result="deployed")
        warns = _events(conn, t, "side_effect_floor_warn")
        assert len(warns) == 1


def test_side_effect_completion_enforce_fails_closed(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SIDE_EFFECT_ENFORCE", "enforce")
    with kb.connect() as conn:
        t = _mk(conn, title="spend money")
        conn.execute(
            "UPDATE tasks SET side_effect_class = 'spend' WHERE id = ?", (t,)
        )
        conn.commit()
        with pytest.raises(ValueError, match="completion floor"):
            kb.complete_task(conn, t, result="spent")


# ---------------------------------------------------------------------------
# cost columns (P1)
# ---------------------------------------------------------------------------

def test_completion_metadata_lands_in_run_cost_columns(kanban_home):
    with kb.connect() as conn:
        t = _mk(conn, title="costed work")
        assert kb.complete_task(
            conn, t, result="done",
            metadata={
                "model": "claude-opus", "tokens_in": 1200, "tokens_out": 300,
                "cost_usd": 0.42, "review_mode": "cross_model",
            },
        )
        row = conn.execute(
            "SELECT model, tokens_in, tokens_out, cost_usd, review_mode "
            "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (t,)
        ).fetchone()
        assert row["model"] == "claude-opus"
        assert row["tokens_in"] == 1200
        assert row["cost_usd"] == pytest.approx(0.42)
        assert row["review_mode"] == "cross_model"


# ---------------------------------------------------------------------------
# policy triggers (P6, shadow)
# ---------------------------------------------------------------------------

def test_policy_triggers_shadow_logs_unstamped_writes(kanban_home):
    with kb.connect() as conn:
        assert kb.install_policy_stamp_triggers(conn, enforce=False) == "shadow"
        # Fresh unstamped write path: purge stamps, then raw INSERT.
        conn.execute("DELETE FROM policy_stamp")
        conn.commit()
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at)"
            " VALUES ('t_shadowprobe', 'raw write', 'todo', strftime('%s','now'))"
        )
        conn.commit()
        rows = conn.execute("SELECT * FROM policy_violations").fetchall()
        assert len(rows) >= 1
        assert rows[0]["task_id"] == "t_shadowprobe"


def test_policy_triggers_enforce_aborts_unstamped_writes(kanban_home):
    with kb.connect() as conn:
        assert kb.install_policy_stamp_triggers(conn, enforce=True) == "enforce"
        conn.execute("DELETE FROM policy_stamp")
        conn.commit()
        with pytest.raises(sqlite3_error_cls()):
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at)"
                " VALUES ('t_enfprobe', 'raw write', 'todo', strftime('%s','now'))"
            )
        # stamped path works
        kb.refresh_policy_stamp(conn)
        t = kb.create_task(conn, title="stamped write", initial_status="running")
        assert t


def sqlite3_error_cls():
    import sqlite3

    return sqlite3.IntegrityError
