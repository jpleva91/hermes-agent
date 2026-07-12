"""Tests for read-only Kanban loop-readiness diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run_kanban(*argv: str) -> int:
    root = argparse.ArgumentParser(prog="hermes")
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    ns = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(ns)


def test_compute_loop_readiness_scores_core_rubric_signals(kanban_home):
    """A board with review gates, durable workspaces, links, handoff evidence,
    retry evidence, and runtime bounds should score those signals without writes.
    """
    with kb.connect() as conn:
        parent = kb.create_task(
            conn,
            title="implement feature",
            body="Human gates: review-required for code changes; no production deploys.",
            assignee="coder",
            workspace_kind="worktree",
            workspace_path="/tmp/wt/feature",
            max_runtime_seconds=1800,
            max_retries=2,
        )
        child = kb.create_task(
            conn,
            title="review feature",
            body="Review card approves or blocks implementation.",
            assignee="reviewer",
            parents=[parent],
            workspace_kind="dir",
            workspace_path="/tmp/reviews",
            max_runtime_seconds=900,
        )
        kb.add_comment(
            conn,
            parent,
            "coder",
            "review-required handoff: changed_files=['a.py']; tests_run=3; diff_path=/tmp/wt/feature; residual_risk=low",
        )
        kb.block_task(conn, parent, reason="review-required: feature implemented; needs eyes")
        kb.add_comment(conn, child, "reviewer", "approved: reviewed /tmp/wt/feature and tests pass")
        kb.complete_task(
            conn,
            child,
            summary="approved implementation after reviewing diff_path=/tmp/wt/feature",
            metadata={"approved": True, "evidence": "/tmp/wt/feature", "tests_run": 3},
        )
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 1, last_failure_error = ? WHERE id = ?",
            ("previous attempt timed out", parent),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, step_key, status, started_at, ended_at, outcome, summary, metadata, error) "
            "VALUES (?, ?, NULL, 'crashed', 100, 120, 'crashed', NULL, NULL, ?)",
            (parent, "coder", "Traceback: boom"),
        )

        report = kd.compute_loop_readiness_report_from_connection(conn, board_slug="default")

    checks = {check["id"]: check for check in report["checks"]}
    assert report["max_score"] == 16
    assert report["score"] >= 12
    assert report["level"] in {"L2", "L3"}
    assert checks["review_gate"]["score"] == 2
    assert checks["durable_workspace"]["score"] == 2
    assert checks["dependency_shape"]["score"] >= 1
    assert any("deadlock" in warning for warning in checks["dependency_shape"]["warnings"])
    assert checks["handoff_quality"]["score"] == 2
    assert checks["retry_crash_evidence"]["score"] == 2
    assert checks["budget_run_log"]["score"] >= 1
    assert any(item["task_id"] == parent for item in report["tasks"])


def test_diagnostics_readiness_json_cli_is_read_only(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="docs spike",
            body="Loop readiness: Level target L1. Budget: max_runtime_seconds=600.",
            assignee="writer",
            workspace_kind="scratch",
            max_runtime_seconds=600,
        )
        kb.add_comment(conn, tid, "writer", "sources: website/docs/example.md; no-op reason documented")
        before = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "runs": conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0],
        }

    assert _run_kanban("diagnostics", "--readiness", "--json") == 0
    out = capsys.readouterr().out
    data = json.loads(out)

    with kb.connect() as conn:
        after = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            "runs": conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0],
        }

    assert after == before
    assert data["board"] == "default"
    assert data["read_only"] is True
    assert {check["id"] for check in data["checks"]} >= {
        "review_gate",
        "durable_workspace",
        "dependency_shape",
        "human_gates_denylist",
        "handoff_quality",
        "retry_crash_evidence",
        "budget_run_log",
    }


def test_diagnostics_readiness_human_output_mentions_level_and_warnings(kanban_home, capsys):
    with kb.connect() as conn:
        kb.create_task(conn, title="scratch artifact loop", body="Creates deliverable artifact", workspace_kind="scratch")

    assert _run_kanban("diagnostics", "--readiness") == 0
    out = capsys.readouterr().out

    assert "Loop readiness" in out
    assert "Level:" in out
    assert "durable_workspace" in out
    assert "Budget/run-log" in out


def test_loop_readiness_durable_workspace_ignores_completed_preflight_evidence_card(kanban_home):
    """Completed audit/preflight evidence cards can cite durable files without needing a durable worker workspace."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="preflight audit evidence",
            body="Read-only audit card: inspected docs, source files, and /tmp scratch logs before score.",
            assignee="posops",
            workspace_kind="scratch",
            max_runtime_seconds=600,
        )
        kb.add_comment(
            conn,
            tid,
            "posops",
            "Durable evidence is in board comment #34 and docs/readiness.md; no artifact was produced in the scratch workspace.",
        )
        kb.complete_task(
            conn,
            tid,
            summary="Read-only audit completed; evidence preserved in comments and run metadata.",
            metadata={"evidence": "board comment #34", "sources": ["docs/readiness.md"], "checks_run": ["diagnostics --readiness --json"]},
        )

        report = kd.compute_loop_readiness_report_from_connection(conn, board_slug="default", task_id=tid)

    durable = {check["id"]: check for check in report["checks"]}["durable_workspace"]
    assert not any(tid in warning for warning in durable["warnings"])


def test_loop_readiness_durable_workspace_ignores_ready_fixture_prose_without_artifact_intent(kanban_home):
    """Ready fixture/non-spawnable residue is not artifact-producing just because generic prose says files/docs/tmp."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="fixture residue for invalid lane",
            body="Fixture row from importer tests; mentions docs, file handles, and /tmp/example only as historical prose.",
            assignee="alice",
            workspace_kind="scratch",
        )

        report = kd.compute_loop_readiness_report_from_connection(conn, board_slug="default", task_id=tid)

    durable = {check["id"]: check for check in report["checks"]}["durable_workspace"]
    assert not any(tid in warning for warning in durable["warnings"])


def test_loop_readiness_durable_workspace_warns_for_active_artifact_producing_scratch_task(kanban_home):
    """Active artifact-producing cards still need a durable workspace path."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="generate readiness report artifact",
            body="Create a deliverable artifact and write docs/config files for the loop readiness dashboard.",
            assignee="coder",
            workspace_kind="scratch",
        )

        report = kd.compute_loop_readiness_report_from_connection(conn, board_slug="default", task_id=tid)

    durable = {check["id"]: check for check in report["checks"]}["durable_workspace"]
    assert any(tid in warning for warning in durable["warnings"])


def test_loop_readiness_missing_human_gates_cannot_emit_l3(kanban_home):
    """Even a strong workflow is not L3 without explicit human gate/denylist evidence."""
    with kb.connect() as conn:
        parent = kb.create_task(
            conn,
            title="implement bounded feature",
            body="Loop readiness: Level target L3. Budget/run log: max_runtime_seconds=1800, max retries=2.",
            assignee="coder",
            workspace_kind="worktree",
            workspace_path="/tmp/wt/bounded-feature",
            max_runtime_seconds=1800,
            max_retries=2,
        )
        child = kb.create_task(
            conn,
            title="review bounded feature",
            body="Review the implementation diff.",
            assignee="reviewer",
            parents=[parent],
            workspace_kind="dir",
            workspace_path="/tmp/reviews",
            max_runtime_seconds=900,
        )
        kb.add_comment(
            conn,
            parent,
            "coder",
            "review-required handoff: changed_files=['feature.py']; tests_run=5; tests_passed=5; diff_path=/tmp/wt/bounded-feature; residual_risk=low",
        )
        kb.block_task(conn, parent, reason="review-required: feature implemented; needs eyes")
        kb.add_comment(conn, child, "reviewer", "approved: reviewed diff_path=/tmp/wt/bounded-feature and tests pass")
        kb.complete_task(
            conn,
            child,
            summary="approved implementation after reviewing diff_path=/tmp/wt/bounded-feature",
            metadata={"approved": True, "evidence": "/tmp/wt/bounded-feature", "tests_run": 5},
        )
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 1, last_failure_error = ? WHERE id = ?",
            ("previous attempt timed out", parent),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, step_key, status, started_at, ended_at, outcome, summary, metadata, error) "
            "VALUES (?, ?, NULL, 'crashed', 100, 120, 'crashed', NULL, NULL, ?)",
            (parent, "coder", "Traceback: boom"),
        )

        report = kd.compute_loop_readiness_report_from_connection(conn, board_slug="default")

    checks = {check["id"]: check for check in report["checks"]}
    assert checks["human_gates_denylist"]["score"] < 2
    assert report["level"] != "L3"


def test_loop_readiness_review_gate_ignores_generic_review_prose(kanban_home):
    """Review instructions in task prose are not reviewer approval evidence."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="implementation awaiting review",
            body=(
                "Implementation card. A reviewer should inspect the review card and approve or block later. "
                "Human gates: no production pushes; secrets/auth/payments require human approval."
            ),
            assignee="coder",
            workspace_kind="worktree",
            workspace_path="/tmp/wt/awaiting-review",
            max_runtime_seconds=1200,
        )
        kb.add_comment(
            conn,
            tid,
            "coder",
            "review-required handoff: changed_files=['feature.py']; tests_run=2; diff_path=/tmp/wt/awaiting-review",
        )
        kb.block_task(conn, tid, reason="review-required: implementation needs reviewer approval")

        report = kd.compute_loop_readiness_report_from_connection(conn, board_slug="default", task_id=tid)

    checks = {check["id"]: check for check in report["checks"]}
    assert checks["review_gate"]["score"] == 1
    assert checks["review_gate"]["status"] == "partial"


@pytest.mark.parametrize(
    "generic_comment",
    [
        "scores the approved 7-check rubric: kanban_show handoff, structured closeout metadata, review gate",
        "source cards include an approved docs-first rubric and a review approval card",
    ],
)
def test_loop_readiness_review_gate_ignores_generic_approved_rubric_prose(kanban_home, generic_comment):
    """Approved source/rubric prose is not reviewer approval evidence."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="implementation with approved source prose",
            body="Human gates: review-required for code; no production deploys.",
            assignee="coder",
            workspace_kind="worktree",
            workspace_path="/tmp/wt/source-prose",
            max_runtime_seconds=1200,
        )
        kb.add_comment(
            conn,
            tid,
            "coder",
            "review-required handoff: changed_files=['feature.py']; tests_run=2; diff_path=/tmp/wt/source-prose",
        )
        kb.add_comment(conn, tid, "coder", generic_comment)
        kb.block_task(conn, tid, reason="review-required: implementation needs reviewer approval")

        report = kd.compute_loop_readiness_report_from_connection(conn, board_slug="default", task_id=tid)

    checks = {check["id"]: check for check in report["checks"]}
    assert checks["review_gate"]["score"] == 1
    assert checks["review_gate"]["status"] == "partial"


@pytest.mark.parametrize(
    "negative_verdict",
    [
        "approved: false",
        "approved: no",
        "approved: blocking issues found",
        '{"approved": false, "findings": ["blocking issue"]}',
    ],
)
def test_loop_readiness_review_gate_rejects_negative_approval_fields(kanban_home, negative_verdict):
    """Explicit non-approval verdict fields must not satisfy the review gate."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="implementation with rejected review",
            body="Human gates: review-required for code; no production deploys.",
            assignee="coder",
            workspace_kind="worktree",
            workspace_path="/tmp/wt/rejected-review",
            max_runtime_seconds=1200,
        )
        kb.add_comment(
            conn,
            tid,
            "coder",
            "review-required handoff: changed_files=['feature.py']; tests_run=2; diff_path=/tmp/wt/rejected-review",
        )
        kb.add_comment(conn, tid, "reviewer", negative_verdict)
        kb.block_task(conn, tid, reason="review-required: implementation needs reviewer approval")

        report = kd.compute_loop_readiness_report_from_connection(conn, board_slug="default", task_id=tid)

    checks = {check["id"]: check for check in report["checks"]}
    assert checks["review_gate"]["score"] == 1
    assert checks["review_gate"]["status"] == "partial"


def test_loop_readiness_review_gate_accepts_explicit_positive_approval_field(kanban_home):
    """Explicit positive verdict fields still satisfy the review gate."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="implementation with approved review",
            body="Human gates: review-required for code; no production deploys.",
            assignee="coder",
            workspace_kind="worktree",
            workspace_path="/tmp/wt/approved-review",
            max_runtime_seconds=1200,
        )
        kb.add_comment(
            conn,
            tid,
            "coder",
            "review-required handoff: changed_files=['feature.py']; tests_run=2; diff_path=/tmp/wt/approved-review",
        )
        kb.add_comment(conn, tid, "reviewer", "approved: true")
        kb.block_task(conn, tid, reason="review-required: implementation needs reviewer approval")

        report = kd.compute_loop_readiness_report_from_connection(conn, board_slug="default", task_id=tid)

    checks = {check["id"]: check for check in report["checks"]}
    assert checks["review_gate"]["score"] == 2
    assert checks["review_gate"]["status"] == "ok"
