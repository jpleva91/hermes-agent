from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from scripts import mission_engine_reactive_sweep as sweep


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(board=sweep.BOARD)
    return home


def _conn():
    return kb.connect(board=sweep.BOARD)


def _mk_target(conn, *, title="Runtime fix: mission handoff", assignee="runtimesteward", body="bounded runtime fix"):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        created_by="missioncommander",
        board=sweep.BOARD,
    )


def _mk_block_gate(conn, target_id, *, title="Ready gate: runtime fix", body="Review target", summary="BLOCK verdict"):
    gate_id = kb.create_task(
        conn,
        title=title,
        body=body,
        assignee="gatewarden",
        created_by="missioncommander",
        board=sweep.BOARD,
    )
    assert kb.complete_task(
        conn,
        gate_id,
        summary=summary,
        metadata={"verdict": "BLOCK", "target_task": target_id},
    )
    return gate_id


def _created_id(result, kind):
    return next(item["task_id"] for item in result if item.get("created") and item["kind"] == kind)


def test_plans_cure_and_regate_for_block_gate_without_duplicates(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        gate_id = kb.create_task(
            conn,
            title="Ready gate: runtime fix",
            body="Review target",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        kb.add_comment(conn, gate_id, "gatewarden", "BLOCK: missing raw evidence")
        assert kb.complete_task(
            conn,
            gate_id,
            summary="BLOCK verdict",
            metadata={"verdict": "BLOCK", "target_task": target_id},
        )

        # First pass mints the cure only: a same-pass re-gate is guaranteed to
        # BLOCK on missing evidence and spawn generation N+1.
        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.assignee) for a in actions] == [("cure", "runtimesteward")]
        result = sweep.apply_actions(conn, actions)
        created = [item for item in result if item.get("created")]
        assert len(created) == 1
        cure_id = _created_id(result, "cure")

        # No evidence on the cure yet: still no re-gate, and no duplicate cure.
        assert sweep.plan_actions(conn) == []

        assert kb.complete_task(conn, cure_id, summary="cure evidence: diff, repro, test output attached", result="fixed")
        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.assignee) for a in actions] == [("regate", "gatewarden")]
        assert actions[0].parents == ()
        assert cure_id in actions[0].body
        result = sweep.apply_actions(conn, actions)
        regate_id = _created_id(result, "regate")
        regate = kb.get_task(conn, regate_id)
        assert regate is not None
        assert regate.status == "ready"
        assert not conn.execute("SELECT 1 FROM task_links WHERE child_id = ?", (regate_id,)).fetchone()

        assert sweep.plan_actions(conn) == []


def test_block_gate_target_can_be_read_from_gate_body_only(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn, title="Implement body-only target", body="runtime fix")
        gate_id = kb.create_task(
            conn,
            title="Ready gate: body-only target",
            body=f"Review blocked build `{target_id}`. Do NOT depend on it.",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            gate_id,
            summary="Gate review completed with BLOCK verdict",
            metadata={"verdict": "BLOCK"},
        )

        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.assignee) for a in actions] == [("cure", "runtimesteward")]
        assert target_id in actions[0].body
        assert "Review blocked build" in actions[0].body


def test_block_gate_target_can_be_read_from_latest_run_summary(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn, title="Implement summary target", body="runtime fix")
        gate_id = kb.create_task(
            conn,
            title="Ready gate: summary target",
            body="Review runtime implementation.",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            gate_id,
            summary=f"BLOCK: original build `{target_id}` lacks raw evidence",
            metadata={"verdict": "BLOCK"},
        )

        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.assignee) for a in actions] == [("cure", "runtimesteward")]
        assert target_id in actions[0].body


def test_block_gate_target_can_be_read_from_gate_result(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn, title="Implement result target", body="runtime fix")
        gate_id = kb.create_task(
            conn,
            title="Ready gate: result target",
            body="Review runtime implementation.",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            gate_id,
            result=f"BLOCK: runtime card `{target_id}` failed evidence contract",
            summary="BLOCK: see result field for target",
            metadata={"verdict": "BLOCK"},
        )

        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.assignee) for a in actions] == [("cure", "runtimesteward")]
        assert target_id in actions[0].body


def test_retro_brief_mentioning_gate_and_target_is_not_semantic_cure(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn, title="Runtime cure needs real remediation", body="blocked target")
        gate_id = kb.create_task(
            conn,
            title="Ready gate: runtime cure",
            body=f"Review target `{target_id}`",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            gate_id,
            summary="BLOCK: remediation is incomplete",
            metadata={"verdict": "BLOCK", "target_task": target_id},
        )
        retro_id = kb.create_task(
            conn,
            title="Retro/close-loop brief: runtime cure status",
            body=(
                f"Close-loop retro/brief mentioning Gate Warden BLOCK `{gate_id}` "
                f"and target `{target_id}` for status tracking only."
            ),
            assignee="specsteward",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(conn, retro_id, summary="Brief posted")

        # The retro brief must not satisfy the semantic cure dedupe: a cure is
        # still planned (first pass is cure-only under the deferred-regate fix).
        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.assignee) for a in actions] == [("cure", "runtimesteward")]
        assert retro_id not in actions[0].body

        result = sweep.apply_actions(conn, actions)
        cure_id = _created_id(result, "cure")
        assert kb.complete_task(conn, cure_id, summary="cure evidence attached")
        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.assignee) for a in actions] == [("regate", "gatewarden")]
        assert cure_id in actions[0].body
        assert "<created-by:" not in actions[0].body
        assert retro_id not in actions[0].body


def test_review_required_block_gets_parentless_gate(kanban_home):
    with _conn() as conn:
        blocked_id = _mk_target(conn, title="Implement mission runtime change", body="needs review")
        assert kb.block_task(conn, blocked_id, reason="review-required: local code changed", kind="needs_input")

        actions = sweep.plan_actions(conn)
        assert len(actions) == 1
        assert actions[0].kind == "review_gate"
        assert actions[0].parents == ()
        result = sweep.apply_actions(conn, actions)
        gate_id = next(item["task_id"] for item in result if item.get("created"))
        gate = kb.get_task(conn, gate_id)
        assert gate is not None
        assert gate.assignee == "gatewarden"
        assert not conn.execute("SELECT 1 FROM task_links WHERE child_id = ?", (gate_id,)).fetchone()

        assert sweep.plan_actions(conn) == []


def test_cure_review_required_block_with_existing_regate_child_gets_parentless_review_gate(kanban_home):
    with _conn() as conn:
        cure_id = kb.create_task(
            conn,
            title="Cure: prior gate BLOCK",
            body="cure work",
            assignee="runtimesteward",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        regate_id = kb.create_task(
            conn,
            title="Re-gate: prior gate BLOCK",
            body=f"Review cure `{cure_id}` after BLOCK",
            assignee="gatewarden",
            created_by="missioncommander",
            parents=[cure_id],
            board=sweep.BOARD,
        )
        assert regate_id
        assert kb.block_task(conn, cure_id, reason="review-required: cure changed runtime", kind="needs_input")

        # The re-gate child hangs off the blocked cure itself: it is
        # unreachable, so it must NOT suppress a parentless ready gate.
        assert sweep._existing_regate_child_for_blocked(conn, cure_id) is None

        actions = sweep.plan_actions(conn)
        assert len(actions) == 1
        assert actions[0].kind == "review_gate"
        assert actions[0].parents == ()


def test_approve_verdict_is_applied_to_review_required_target_and_dual_posted(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn, title="Runtime cure awaiting approval", body="structured handoff in comments")
        assert kb.block_task(conn, target_id, reason="review-required: runtime changed", kind="needs_input")
        gate_id = kb.create_task(
            conn,
            title="Ready gate: approve runtime cure",
            body=f"Review target `{target_id}`",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            gate_id,
            summary="APPROVE: raw evidence checked",
            metadata={"verdict": "APPROVE", "target_task": target_id},
        )

        propagations = sweep.plan_approve_propagations(conn)
        assert len(propagations) == 1
        assert propagations[0].gate_id == gate_id
        assert propagations[0].target_id == target_id

        result = sweep.apply_approve_propagations(conn, propagations)
        assert result[0]["applied"] is True
        target = kb.get_task(conn, target_id)
        assert target is not None
        assert target.status == "done"
        marker = f"mission-reactive:approve:{gate_id}:{target_id}"
        assert marker in sweep._comments_text(conn, gate_id)
        assert marker in sweep._comments_text(conn, target_id)
        assert sweep.plan_approve_propagations(conn) == []


# ---------------------------------------------------------------------------
# FIX 1 [title-stacking]: minted titles strip already-minted Cure:/Re-gate:
# prefixes before re-prefixing.
# ---------------------------------------------------------------------------


def test_cure_title_strips_existing_cure_prefix(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn, title="Cure: enforce Spec 002 active-cast preflight")
        _mk_block_gate(conn, target_id)

        actions = sweep.plan_actions(conn)
        assert [a.kind for a in actions] == ["cure"]
        assert actions[0].title == "Cure: enforce Spec 002 active-cast preflight"

        result = sweep.apply_actions(conn, actions)
        cure_id = _created_id(result, "cure")
        assert kb.complete_task(conn, cure_id, summary="cure evidence attached")
        actions = sweep.plan_actions(conn)
        assert [a.kind for a in actions] == ["regate"]
        assert actions[0].title == "Re-gate: enforce Spec 002 active-cast preflight"


def test_cure_title_strips_stacked_and_uppercase_prefixes(kanban_home):
    with _conn() as conn:
        stacked_id = _mk_target(conn, title="Re-gate: CURE: Cure: fix cli rc repro")
        _mk_block_gate(conn, stacked_id, title="Ready gate: stacked prefixes")
        prefix_only_id = _mk_target(conn, title="Cure:")
        _mk_block_gate(conn, prefix_only_id, title="Ready gate: prefix only")

        actions = sweep.plan_actions(conn)
        assert sorted(a.kind for a in actions) == ["cure", "cure"]
        titles = {a.title for a in actions}
        assert "Cure: fix cli rc repro" in titles
        # Prefix-only title falls back to the raw title rather than minting an
        # empty "Cure: " card.
        assert "Cure: Cure:" in titles
        for title in titles:
            assert title.removeprefix("Cure: ").strip()  # no empty-suffix cure


def test_second_generation_regate_block_does_not_stack_title(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn, title="implement X")
        _mk_block_gate(conn, target_id)

        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        cure_id = _created_id(result, "cure")
        assert kb.complete_task(conn, cure_id, summary="cure evidence attached")
        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        regate_id = _created_id(result, "regate")

        assert kb.complete_task(
            conn,
            regate_id,
            summary=f"BLOCK: cure card `{cure_id}` has no on-card evidence yet",
            metadata={"verdict": "BLOCK"},
        )

        actions = sweep.plan_actions(conn)
        for action in actions:
            if action.kind in {"cure", "regate"}:
                assert re.match(r"^(?:cure|re-gate): (?!(?:cure|re-?gate):)", action.title, re.I)
        # Stronger: the livelock is dead entirely -- no generation N+1 at all.
        assert actions == []


# ---------------------------------------------------------------------------
# FIX 2 [per-gate-dedup-generations]: cure/regate lanes are keyed on the
# lineage root, so BLOCKed re-gates cannot mint unbounded generations.
# ---------------------------------------------------------------------------


def test_blocked_regate_does_not_mint_second_generation(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        gate_id = _mk_block_gate(conn, target_id)

        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        cure_id = _created_id(result, "cure")

        # Replay the incident: a same-minute re-gate minted by the PRE-fix code
        # (legacy per-gate key, real body builder), BLOCKed for missing evidence.
        gate_row = sweep._task(conn, gate_id)
        target_row = sweep._task(conn, target_id)
        regate_id = kb.create_task(
            conn,
            title=f"Re-gate: {str(target_row['title'])[:75]}",
            body=sweep._regate_body(gate_row, target_row, cure_id),
            assignee="gatewarden",
            created_by="missioncommander",
            idempotency_key=f"mission-reactive:regate:{gate_id}",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            regate_id,
            summary=f"BLOCK: cure card `{cure_id}` has no on-card evidence yet",
            metadata={"verdict": "BLOCK"},
        )

        assert sweep.plan_actions(conn) == []


def test_cure_and_regate_dedupe_keys_are_rooted_on_original_target(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        _mk_block_gate(conn, target_id, title="Ready gate: first concern")
        _mk_block_gate(conn, target_id, title="Ready gate: second concern")

        # Two BLOCK gates on the same target plan exactly one rooted cure lane.
        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.dedupe_key) for a in actions] == [
            ("cure", f"mission-reactive:cure:{target_id}"),
        ]

        result = sweep.apply_actions(conn, actions)
        cure_id = _created_id(result, "cure")
        assert kb.complete_task(conn, cure_id, summary="cure evidence attached")

        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.dedupe_key) for a in actions] == [
            ("regate", f"mission-reactive:regate:{target_id}"),
        ]


def test_legacy_per_gate_key_cards_still_dedupe(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        gate_id = _mk_block_gate(conn, target_id)

        kb.create_task(
            conn,
            title="Cure: Runtime fix: mission handoff",
            body="legacy pre-fix cure card",
            assignee="runtimesteward",
            created_by="missioncommander",
            idempotency_key=f"mission-reactive:cure:{gate_id}",
            board=sweep.BOARD,
        )
        kb.create_task(
            conn,
            title="Re-gate: Runtime fix: mission handoff",
            body="legacy pre-fix regate card",
            assignee="gatewarden",
            created_by="missioncommander",
            idempotency_key=f"mission-reactive:regate:{gate_id}",
            board=sweep.BOARD,
        )

        assert sweep.plan_actions(conn) == []


def test_legacy_two_generation_chain_stays_dormant(kanban_home):
    """Models the dormant spec-198 chain: pre-fix per-gate keys, stacked titles."""
    with _conn() as conn:
        target_id = _mk_target(conn, title="finish spec-198 docs")
        gate1_id = _mk_block_gate(conn, target_id, title="Ready gate: spec-198 docs")
        gate1 = sweep._task(conn, gate1_id)
        target = sweep._task(conn, target_id)

        cure1_id = kb.create_task(
            conn,
            title="Cure: finish spec-198 docs",
            body=sweep._cure_body(gate1, target, "no on-card evidence yet"),
            assignee="runtimesteward",
            created_by="missioncommander",
            idempotency_key=f"mission-reactive:cure:{gate1_id}",
            board=sweep.BOARD,
        )
        regate1_id = kb.create_task(
            conn,
            title="Re-gate: finish spec-198 docs",
            body=sweep._regate_body(gate1, target, cure1_id),
            assignee="gatewarden",
            created_by="missioncommander",
            idempotency_key=f"mission-reactive:regate:{gate1_id}",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            regate1_id,
            summary=f"BLOCK: cure card `{cure1_id}` has no on-card evidence yet",
            metadata={"verdict": "BLOCK"},
        )

        regate1 = sweep._task(conn, regate1_id)
        cure1 = sweep._task(conn, cure1_id)
        cure2_id = kb.create_task(
            conn,
            title="Cure: Cure: finish spec-198 docs",
            body=sweep._cure_body(regate1, cure1, "generation-2 mint"),
            assignee="runtimesteward",
            created_by="missioncommander",
            idempotency_key=f"mission-reactive:cure:{regate1_id}",
            board=sweep.BOARD,
        )
        kb.create_task(
            conn,
            title="Re-gate: Cure: finish spec-198 docs",
            body=sweep._regate_body(regate1, cure1, cure2_id),
            assignee="gatewarden",
            created_by="missioncommander",
            idempotency_key=f"mission-reactive:regate:{regate1_id}",
            board=sweep.BOARD,
        )

        assert sweep._lineage_root(conn, cure1_id) == target_id
        assert sweep._lineage_root(conn, cure2_id) == target_id
        assert sweep.plan_actions(conn) == []


def test_archived_cure_reopens_exactly_one_rooted_lane(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        _mk_block_gate(conn, target_id)

        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        cure_id = _created_id(result, "cure")
        assert kb.archive_task(conn, cure_id)

        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.dedupe_key) for a in actions] == [
            ("cure", f"mission-reactive:cure:{target_id}"),
        ]
        assert not actions[0].title.startswith("Cure: Cure:")
        sweep.apply_actions(conn, actions)
        assert sweep.plan_actions(conn) == []


def test_regate_semantic_root_match_does_not_suppress_initial_regate(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        # Original gate is titled "Ready gate: ..." and its body names the
        # target: the title-scoped 're-gate:' semantic matcher must NOT treat
        # it as an existing re-gate.
        gate_id = kb.create_task(
            conn,
            title="Ready gate: runtime fix",
            body=f"Review target `{target_id}` and verdict.",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            gate_id,
            summary="BLOCK verdict",
            metadata={"verdict": "BLOCK", "target_task": target_id},
        )

        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        cure_id = _created_id(result, "cure")
        assert kb.complete_task(conn, cure_id, summary="cure evidence attached")

        actions = sweep.plan_actions(conn)
        assert [a.kind for a in actions] == ["regate"]


# ---------------------------------------------------------------------------
# FIX 3 [simultaneous-cure-regate-mint]: the re-gate is deferred until the
# cure lane has on-card evidence.
# ---------------------------------------------------------------------------


def test_regate_deferred_until_cure_exists_and_has_evidence(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        _mk_block_gate(conn, target_id)

        actions = sweep.plan_actions(conn)
        assert [(a.kind, a.assignee) for a in actions] == [("cure", "runtimesteward")]
        sweep.apply_actions(conn, actions)

        # Cure exists but has no evidence: regate stays deferred, no dup cure.
        assert sweep.plan_actions(conn) == []


def test_regate_minted_after_cure_completes(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        _mk_block_gate(conn, target_id)
        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        cure_id = _created_id(result, "cure")

        assert kb.complete_task(conn, cure_id, summary="cure evidence: diff+repro+tests", result="fixed")

        actions = sweep.plan_actions(conn)
        assert len(actions) == 1
        action = actions[0]
        assert action.kind == "regate"
        assert action.assignee == "gatewarden"
        assert action.parents == ()
        assert cure_id in action.body
        assert "<created-by:" not in action.body

        result = sweep.apply_actions(conn, actions)
        regate_id = _created_id(result, "regate")
        regate = kb.get_task(conn, regate_id)
        assert regate is not None
        assert regate.status == "ready"
        assert not conn.execute("SELECT 1 FROM task_links WHERE child_id = ?", (regate_id,)).fetchone()
        assert sweep.plan_actions(conn) == []


def test_review_required_cure_gets_exactly_one_gatewarden_review(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        _mk_block_gate(conn, target_id)
        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        cure_id = _created_id(result, "cure")

        assert kb.block_task(conn, cure_id, reason="review-required: cure changed runtime", kind="needs_input")

        # Exactly one Gate Warden review card in a single pass (regate XOR
        # review_gate) -- the loop-1/loop-2 same-pass double-gate regression.
        actions = sweep.plan_actions(conn)
        assert len(actions) == 1
        assert actions[0].assignee == "gatewarden"
        assert actions[0].kind == "regate"

        sweep.apply_actions(conn, actions)
        assert sweep.plan_actions(conn) == []


def test_no_generation_storm_from_evidence_starved_block(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        _mk_block_gate(conn, target_id)

        actions = sweep.plan_actions(conn)
        assert [a.kind for a in actions] == ["cure"]
        sweep.apply_actions(conn, actions)

        # The cure never produces evidence; repeated sweeps stay silent.
        for _ in range(5):
            assert sweep.plan_actions(conn) == []
        stacked = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE title LIKE 'Cure: Cure:%'").fetchone()
        assert stacked["n"] == 0


def test_existing_review_gate_for_cure_suppresses_regate(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        _mk_block_gate(conn, target_id)
        cure_id = kb.create_task(
            conn,
            title="Cure: Runtime fix: mission handoff",
            body="remediation work",
            assignee="runtimesteward",
            created_by="missioncommander",
            idempotency_key=f"mission-reactive:cure:{target_id}",
            board=sweep.BOARD,
        )
        assert kb.complete_task(conn, cure_id, summary="cure evidence attached")
        # A loop-2 style review gate already references the cure.
        kb.create_task(
            conn,
            title="Ready gate: review-required handoff for Runtime fix: mission handoff",
            body=f"Review cure card `{cure_id}` raw evidence.",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )

        actions = sweep.plan_actions(conn)
        assert all(a.kind != "regate" for a in actions)
        assert actions == []


# ---------------------------------------------------------------------------
# FIX 4 [superseded-gate-replay]: done/archived targets and superseded gates
# no longer replay cure/re-gate work.
# ---------------------------------------------------------------------------


def test_block_gate_with_done_target_plans_nothing(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        assert kb.complete_task(conn, target_id, summary="closed by Mission Commander")
        _mk_block_gate(conn, target_id)

        assert sweep.plan_actions(conn) == []


def test_block_gate_with_archived_target_plans_nothing(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        assert kb.archive_task(conn, target_id)
        _mk_block_gate(conn, target_id)

        assert sweep.plan_actions(conn) == []


def test_older_block_gate_superseded_by_newer_gate_on_same_target(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        assert kb.block_task(conn, target_id, reason="review-required: runtime changed", kind="needs_input")
        old_gate_id = _mk_block_gate(conn, target_id, title="Ready gate: first concern")
        new_gate_id = _mk_block_gate(conn, target_id, title="Ready gate: second concern")
        # Rows are created within the same second; force strict ordering.
        conn.execute(
            "UPDATE tasks SET created_at = created_at + 60, completed_at = COALESCE(completed_at, created_at) + 60 WHERE id = ?",
            (new_gate_id,),
        )

        old_gate = sweep._task(conn, old_gate_id)
        new_gate = sweep._task(conn, new_gate_id)
        assert sweep._superseded_by_newer_gate(conn, old_gate, target_id) is True
        assert sweep._superseded_by_newer_gate(conn, new_gate, target_id) is False

        actions = sweep.plan_actions(conn)
        cures = [a for a in actions if a.kind == "cure"]
        assert len(cures) == 1
        assert cures[0].dedupe_key == f"mission-reactive:cure:{target_id}"
        # The newer gate owns the lane: its id (not the superseded one) is in
        # the cure body evidence header.
        assert new_gate_id in cures[0].body
        assert old_gate_id not in cures[0].body


def test_archiving_minted_cure_and_regate_does_not_rearm_old_block_gate(kanban_home):
    """Incident replay: manual archive of storm cards must not re-arm the gate."""
    with _conn() as conn:
        target_id = _mk_target(conn)
        assert kb.block_task(conn, target_id, reason="review-required: runtime changed", kind="needs_input")
        _mk_block_gate(conn, target_id, title="Ready gate: original block")

        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        cure_id = _created_id(result, "cure")
        assert kb.archive_task(conn, cure_id)

        approve_gate_id = kb.create_task(
            conn,
            title="Ready gate: approve after manual closure",
            body=f"Review target `{target_id}`",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            approve_gate_id,
            summary="APPROVE: raw evidence checked",
            metadata={"verdict": "APPROVE", "target_task": target_id},
        )
        conn.execute(
            "UPDATE tasks SET created_at = created_at + 120, completed_at = COALESCE(completed_at, created_at) + 120 WHERE id = ?",
            (approve_gate_id,),
        )
        sweep.apply_approve_propagations(conn, sweep.plan_approve_propagations(conn))
        target = kb.get_task(conn, target_id)
        assert target is not None and target.status == "done"

        assert sweep.plan_actions(conn) == []


def test_regate_completing_with_block_does_not_mint_next_generation_when_prior_cure_archived(kanban_home):
    with _conn() as conn:
        target_id = _mk_target(conn)
        _mk_block_gate(conn, target_id)
        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        cure_id = _created_id(result, "cure")
        assert kb.complete_task(conn, cure_id, summary="cure evidence attached")
        result = sweep.apply_actions(conn, sweep.plan_actions(conn))
        regate_id = _created_id(result, "regate")

        assert kb.complete_task(
            conn,
            regate_id,
            summary=f"BLOCK: cure card `{cure_id}` evidence insufficient",
            metadata={"verdict": "BLOCK"},
        )
        assert kb.archive_task(conn, cure_id)
        assert kb.complete_task(conn, target_id, summary="chain closed manually")

        actions = sweep.plan_actions(conn)
        assert all("Cure: Cure:" not in a.title for a in actions)
        assert actions == []


# ---------------------------------------------------------------------------
# FIX 5 [blocked-scan-dead-predicate-reachability]: dead predicates removed,
# dedupe is reachability-aware (satisfied parents == reachable).
# ---------------------------------------------------------------------------


def test_reachable_regate_with_satisfied_parents_suppresses_duplicate_review_gate(kanban_home):
    with _conn() as conn:
        done_parent_id = _mk_target(conn, title="Finished prerequisite")
        assert kb.complete_task(conn, done_parent_id, summary="done")
        blocked_id = kb.create_task(
            conn,
            title="Cure: prior gate BLOCK",
            body="cure work",
            assignee="runtimesteward",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        regate_id = kb.create_task(
            conn,
            title="Re-gate: prior gate BLOCK",
            body=f"Review cure `{blocked_id}` after BLOCK",
            assignee="gatewarden",
            created_by="missioncommander",
            parents=[done_parent_id],
            board=sweep.BOARD,
        )
        assert kb.block_task(conn, blocked_id, reason="review-required: cure changed runtime", kind="needs_input")

        # All parents satisfied => the gate is reachable and suppresses a
        # duplicate parentless ready gate.
        assert sweep._existing_regate_child_for_blocked(conn, blocked_id) == regate_id
        assert sweep.plan_actions(conn) == []


def test_archived_blocked_card_is_not_scanned(kanban_home):
    with _conn() as conn:
        blocked_id = _mk_target(conn, title="Implement mission runtime change")
        assert kb.block_task(conn, blocked_id, reason="review-required: local code changed", kind="needs_input")
        assert kb.archive_task(conn, blocked_id)

        assert sweep.plan_actions(conn) == []


# ---------------------------------------------------------------------------
# FIX [audit #4]: APPROVE propagation resolves a re-gate through the cure/re-gate
# lineage to the ORIGINAL blocked build, not the (done) cure card, so the build
# is not stranded blocked-with-its-approval-on-the-wrong-card forever.
# ---------------------------------------------------------------------------


def test_approve_propagation_resolves_regate_target_to_lineage_root(kanban_home):
    with _conn() as conn:
        build_id = _mk_target(conn, title="Implement mission architecture", body="the real build work")
        assert kb.block_task(conn, build_id, reason="review-required: build changed runtime", kind="needs_input")

        # Cure card whose body points at the original build (matches the lineage
        # regex), then completes and goes done -- the shape that stranded t_1d942c15.
        cure_id = kb.create_task(
            conn,
            title="Cure: Implement mission architecture",
            body=f"Mission Commander reactive cure card for Gate Warden BLOCK `t_00000000` against `{build_id}`.",
            assignee="runtimesteward",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(conn, cure_id, summary="cure applied", metadata={"verdict": "APPROVE"})

        # Re-gate whose immediate target (metadata target_task) is the DONE cure card.
        regate_id = kb.create_task(
            conn,
            title="Re-gate: Implement mission architecture",
            body=f"reactive re-gate for cure card `{cure_id}` after Gate Warden BLOCK `t_00000000` on target `{build_id}`.",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn,
            regate_id,
            summary="APPROVE: raw evidence checked",
            metadata={"verdict": "APPROVE", "target_task": cure_id},
        )

        propagations = sweep.plan_approve_propagations(conn)
        assert len(propagations) == 1
        assert propagations[0].gate_id == regate_id
        # The fix: resolves to the original build, NOT the done cure card.
        assert propagations[0].target_id == build_id
        assert propagations[0].target_id != cure_id

        sweep.apply_approve_propagations(conn, propagations)
        build = kb.get_task(conn, build_id)
        assert build is not None and build.status == "done"


# ---------------------------------------------------------------------------
# FIX [audit #5]: a 0-action sweep must be distinguishable from a converged
# board. detect_stalls() surfaces review-required-blocked cards with no open
# gate (frozen, awaiting a human) vs. a genuinely converged board.
# ---------------------------------------------------------------------------


def test_detect_stalls_flags_frozen_review_required_with_only_a_done_gate(kanban_home):
    with _conn() as conn:
        assert sweep.detect_stalls(conn) == []  # empty board is converged, not stalled

        build_id = _mk_target(conn, title="Frozen build awaiting human")
        assert kb.block_task(conn, build_id, reason="review-required: awaiting gate", kind="needs_input")
        # A gate that already ran and is DONE (BLOCK) is not an OPEN gate.
        gate_id = kb.create_task(
            conn,
            title="Ready gate: frozen build",
            body=f"Review target `{build_id}`",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert kb.complete_task(
            conn, gate_id, summary="BLOCK: missing evidence", metadata={"verdict": "BLOCK", "target_task": build_id}
        )

        stalls = sweep.detect_stalls(conn)
        assert [s["task_id"] for s in stalls] == [build_id]
        assert stalls[0]["lineage_root"] == build_id


def test_detect_stalls_ignores_card_with_an_open_gate(kanban_home):
    with _conn() as conn:
        build_id = _mk_target(conn, title="Build under active review")
        assert kb.block_task(conn, build_id, reason="review-required: awaiting gate", kind="needs_input")
        # A non-terminal (todo/ready/running) gate referencing the card -> not stalled.
        kb.create_task(
            conn,
            title="Ready gate: active review",
            body=f"Review target `{build_id}`",
            assignee="gatewarden",
            created_by="missioncommander",
            board=sweep.BOARD,
        )
        assert sweep.detect_stalls(conn) == []
