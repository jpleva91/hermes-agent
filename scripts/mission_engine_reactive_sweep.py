#!/usr/bin/env python3
"""Spec 002 Mission Engine reactive Kanban sweep.

Scans the fable-emulation-workflow board for completed Gate Warden BLOCK
verdicts and review-required blocked implementation cards, then idempotently
mints the missing cure/re-gate or parentless ready-gate cards.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_db as kb  # noqa: E402

BOARD = "fable-emulation-workflow"
AUTHOR = "missioncommander"
GATEWARDEN = "gatewarden"
SPEC_KIT_PACKET = "/home/red/.hermes/specs/002-mission-engine/spec.md"
SOURCE_PACKET = "/home/red/.hermes/specs/002-mission-engine/evidence-contract.md"
EVIDENCE_CONTRACT = SOURCE_PACKET
ACTIVE_CAST = {
    "missioncommander",
    "specsteward",
    "sourcecartographer",
    "claudecodeconductor",
    "codexoperator",
    "gatewarden",
    "runtimesteward",
}
ARCHIVED = "archived"


@dataclass(frozen=True)
class Action:
    kind: str
    dedupe_key: str
    title: str
    body: str
    assignee: str
    parents: tuple[str, ...] = ()
    priority: int = 90


@dataclass(frozen=True)
class ApprovePropagation:
    gate_id: str
    target_id: str
    summary: str


def _rows(conn, sql: str, params: Iterable[Any] = ()):  # sqlite Row iterator helper
    return conn.execute(sql, tuple(params)).fetchall()


def _json_loads(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _task(conn, task_id: str):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def _parents(conn, task_id: str) -> list[str]:
    return [r["parent_id"] for r in _rows(conn, "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,))]


def _has_unsatisfied_parents(conn, task_id: str) -> bool:
    """True when any parent is not done/archived (mirrors kanban recompute_ready)."""
    row = conn.execute(
        "SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def _comments_text(conn, task_id: str) -> str:
    rows = _rows(conn, "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,))
    return "\n".join(str(r["body"] or "") for r in rows)


def _gate_card_and_run_text(conn, gate_id: str) -> str:
    """Gate title/body plus canonical completion handoff fields.

    This is the high-confidence target source after structured metadata. It
    intentionally excludes comments so an incidental task id in discussion does
    not outrank a target named in the card body or closing run handoff.
    """
    gate = _task(conn, gate_id)
    parts: list[str] = []
    if gate:
        parts.extend([
            str(gate["title"] or ""),
            str(gate["body"] or ""),
            str(gate["result"] or ""),
        ])
    run = _latest_run(conn, gate_id)
    if run:
        parts.append(str(run["summary"] or ""))
    return "\n".join(part for part in parts if part)


def _gate_evidence_text(conn, gate_id: str) -> str:
    """Searchable human evidence for a Gate Warden task.

    Early/manual bootstrap gates sometimes put the target handle only in the
    card body (for example: ``Review blocked build `t_...```) and complete with
    structured verdict metadata but no comments/result text. Treat body/title and
    latest run handoff fields as first-class gate evidence so the sweep can still
    route the cure.
    """
    parts: list[str] = []
    card_and_run = _gate_card_and_run_text(conn, gate_id)
    if card_and_run:
        parts.append(card_and_run)
    comments = _comments_text(conn, gate_id)
    if comments:
        parts.append(comments)
    return "\n".join(part for part in parts if part)


def _latest_run(conn, task_id: str):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _latest_block_reason(conn, task_id: str) -> str:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    payload = _json_loads(row["payload"]) if row else None
    if isinstance(payload, dict):
        return str(payload.get("reason") or "")
    return ""


def _block_metadata(conn, gate_id: str) -> dict[str, Any]:
    run = _latest_run(conn, gate_id)
    metadata = _json_loads(run["metadata"] if run else None)
    return metadata if isinstance(metadata, dict) else {}


def _is_block_verdict(conn, gate_row) -> bool:
    """Return True only when the gate's latest verdict is still BLOCK.

    Gate cards can accumulate earlier `BLOCK:` comments from not-ready retries and
    later complete with an APPROVE. The latest structured run/card result must
    override stale historical BLOCK comments; otherwise the sweep re-opens cured
    gates and creates duplicate cure/regate storms.
    """
    metadata = _block_metadata(conn, gate_row["id"])
    verdict = str(metadata.get("verdict") or "").upper()
    if verdict == "APPROVE":
        return False
    if verdict == "BLOCK":
        return True

    latest_fields = _gate_card_and_run_text(conn, gate_row["id"])
    if re.search(r"(^|\n)\s*APPROVE\b", latest_fields, re.IGNORECASE):
        return False
    if re.search(r"(^|\n)\s*BLOCK\s*:", latest_fields, re.IGNORECASE):
        return True

    text = _comments_text(conn, gate_row["id"])
    return bool(re.search(r"(^|\n)\s*BLOCK\s*:", text, re.IGNORECASE))


def _is_approve_verdict(conn, gate_row) -> bool:
    """Return True when the gate's latest verdict is APPROVE.

    The sweep treats APPROVE and BLOCK as equally actionable verdicts:
    BLOCK mints cure work, while APPROVE closes the corresponding
    review-required target. Structured run metadata is authoritative; latest
    card/run handoff text is the fallback for bootstrap/manual gates.
    """
    metadata = _block_metadata(conn, gate_row["id"])
    verdict = str(metadata.get("verdict") or "").upper()
    if verdict == "APPROVE":
        return True
    if verdict == "BLOCK":
        return False

    latest_fields = _gate_card_and_run_text(conn, gate_row["id"])
    if re.search(r"(^|\n)\s*BLOCK\s*:", latest_fields, re.IGNORECASE):
        return False
    if re.search(r"(^|\n)\s*APPROVE\b", latest_fields, re.IGNORECASE):
        return True

    text = _comments_text(conn, gate_row["id"])
    return bool(re.search(r"(^|\n)\s*APPROVE\b", text, re.IGNORECASE))


def _target_task_for_gate(conn, gate_id: str, metadata: dict[str, Any]) -> str | None:
    for key in ("target_task", "target", "blocked_task", "blocked_build", "runtime_card"):
        value = metadata.get(key)
        if isinstance(value, str) and re.fullmatch(r"t_[0-9a-f]+", value):
            return value

    primary_text = _gate_card_and_run_text(conn, gate_id)
    comments_text = _comments_text(conn, gate_id)
    # Prefer explicit target-ish prose before falling back to the first task id.
    explicit_patterns = (
        r"target[_ -]task[`:\s]+(t_[0-9a-f]+)",
        r"runtime card [`']?(t_[0-9a-f]+)",
        r"review (?:blocked build|target) [`']?(t_[0-9a-f]+)`?",
        r"original (?:build|runtime).*?`(t_[0-9a-f]+)`",
        r"on original build [`']?(t_[0-9a-f]+)",
    )
    for text in (primary_text, comments_text):
        for pattern in explicit_patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1)
    for text in (primary_text, comments_text):
        for tid in re.findall(r"t_[0-9a-f]+", text):
            if tid != gate_id and _task(conn, tid):
                return tid
    return None


def _nonarchived_with_key(conn, key: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != ? ORDER BY created_at DESC LIMIT 1",
        (key, ARCHIVED),
    ).fetchone()
    return row["id"] if row else None


_CURE_LINEAGE_RE = re.compile(
    r"cure card for Gate Warden BLOCK `t_[0-9a-f]+` against `(t_[0-9a-f]+)`"
)
_REGATE_LINEAGE_RE = re.compile(
    r"Gate Warden BLOCK `t_[0-9a-f]+` on target `(t_[0-9a-f]+)`"
)


def _lineage_root(conn, task_id: str) -> str:
    """Follow sweep-minted cure/re-gate cards back to the original target.

    Sweep-authored bodies embed their immediate target ("... against `t_x`"
    for cures, "... on target `t_x`" for re-gates). Walking those pointers
    yields the original blocked card, so cure/regate idempotency keys stay
    stable across BLOCK -> cure -> re-gate -> BLOCK generations instead of
    minting a fresh generation per gate id.
    """
    current = task_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        row = _task(conn, current)
        if not row:
            break
        body = str(row["body"] or "")
        match = _CURE_LINEAGE_RE.search(body) or _REGATE_LINEAGE_RE.search(body)
        if not match or match.group(1) == current:
            break
        current = match.group(1)
    return current


_NON_CURE_TITLE_RE = re.compile(
    r"\b(?:retro(?:spective)?|close[- ]loop|brief|status|summary|digest|report|rollup)\b",
    re.IGNORECASE,
)
_CURE_SEMANTIC_RE = re.compile(
    r"\b(?:cure|remediat(?:e|es|ed|ion)|fix(?:es|ed)?|repair|patch|resolve[sd]?)\b",
    re.IGNORECASE,
)

_MINTED_TITLE_PREFIX_RE = re.compile(r"^(?:(?:cure|re-?gate)\s*:\s*)+", re.IGNORECASE)


def _base_title(raw) -> str:
    """Strip stacked sweep-minted 'Cure:'/'Re-gate:' prefixes before re-prefixing."""
    title = str(raw or "").strip()
    stripped = _MINTED_TITLE_PREFIX_RE.sub("", title).strip()
    return stripped or title


def _is_semantic_cure_candidate(row, *, source_id: str, root_id: str | None = None) -> bool:
    """Return True only for real remediation cards, not status/retro briefs."""
    title = str(row["title"] or "")
    body = str(row["body"] or "")
    assignee = str(row["assignee"] or "")
    title_lower = title.lower()
    if source_id not in body:
        # Cross-generation dedupe: an earlier sweep-minted cure names an older
        # gate id, never the current one. Accept it when it names the lineage
        # root and is unambiguously a cure card by title.
        if not (root_id and root_id in body and title_lower.startswith("cure:")):
            return False
    if assignee in {GATEWARDEN, AUTHOR}:
        return False
    if title_lower.startswith(("ready gate:", "gate review:", "re-gate:", "acceptance rerun:")):
        return False
    if _NON_CURE_TITLE_RE.search(title):
        return False
    return bool(_CURE_SEMANTIC_RE.search(title) or _CURE_SEMANTIC_RE.search(body))


def _existing_semantic(conn, *, kind: str, source_id: str, target_id: str | None = None) -> str | None:
    candidates = _rows(
        conn,
        """
        SELECT id, title, body, status, assignee
          FROM tasks
         WHERE status != ?
           AND (body LIKE ? OR body LIKE ?)
         ORDER BY created_at DESC
        """,
        (ARCHIVED, f"%{source_id}%", f"%{target_id or source_id}%"),
    )
    for row in candidates:
        title = str(row["title"] or "").lower()
        body = str(row["body"] or "")
        assignee = str(row["assignee"] or "")
        if kind == "cure" and _is_semantic_cure_candidate(row, source_id=source_id, root_id=target_id):
            return row["id"]
        if (
            kind == "regate"
            and assignee == GATEWARDEN
            and (
                (source_id in body and ("gate" in title or "review" in title))
                # Cross-generation dedupe: an earlier sweep-minted re-gate names
                # an older gate id, never the current one. Accept it when it
                # names the lineage root and is unambiguously a re-gate by
                # title (never "Ready gate: ..." originals).
                or (target_id is not None and target_id in body and title.startswith("re-gate:"))
            )
        ):
            return row["id"]
        if kind == "review_gate" and assignee == GATEWARDEN and (target_id or source_id) in body and not _has_unsatisfied_parents(conn, row["id"]):
            return row["id"]
    return None


def _existing_regate_child_for_blocked(conn, blocked_id: str) -> str | None:
    """Return an existing REACHABLE Gate Warden review for a blocked task.

    A Gate Warden child whose parent is the blocked task is unreachable: the
    child cannot run until the blocked target is done, while the target is
    waiting for review. Such a child must not suppress a parentless ready gate.

    Reachable means parentless OR all parents done/archived (mirrors kanban
    recompute_ready promotion): a gate whose parents are all satisfied is
    'ready' and reviews the blocked target, so it suppresses a duplicate
    parentless ready gate.
    """
    row = conn.execute(
        """
        SELECT c.id
          FROM tasks c
         WHERE c.status != ?
           AND c.assignee = ?
           AND c.body LIKE ?
           AND (lower(c.title) LIKE '%re-gate:%' OR lower(c.title) LIKE '%gate%' OR lower(c.title) LIKE '%review%')
           AND NOT EXISTS (SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id WHERE l.child_id = c.id AND p.status NOT IN ('done', 'archived'))
         ORDER BY c.created_at DESC
         LIMIT 1
        """,
        (ARCHIVED, GATEWARDEN, f"%{blocked_id}%"),
    ).fetchone()
    return row["id"] if row else None


def _is_review_required_blocked(conn, row) -> bool:
    if str(row["status"] or "") != "blocked":
        return False
    run = _latest_run(conn, row["id"])
    reason = _latest_block_reason(conn, row["id"])
    summary = str(run["summary"] or "") if run else ""
    return reason.startswith("review-required:") or summary.startswith("review-required:")


def _cure_has_on_card_evidence(conn, cure_id: str) -> bool:
    """True once the cure lane has produced reviewable on-card evidence.

    A re-gate minted before this point is guaranteed to BLOCK ("no on-card
    remediation evidence yet"), which the next sweep pass turns into cure
    generation N+1. Defer the re-gate until the cure card is done, has
    self-blocked with a review-required handoff, or has a run handoff summary.
    """
    cure = _task(conn, cure_id)
    if not cure:
        return False
    if str(cure["status"] or "") == "done":
        return True
    if _is_review_required_blocked(conn, cure):
        return True
    run = _latest_run(conn, cure_id)
    return bool(run and str(run["summary"] or "").strip())


_TERMINAL_TARGET_STATUSES = {"done", ARCHIVED}


def _superseded_by_newer_gate(conn, gate, target_id: str) -> bool:
    """True when a newer done Gate Warden card verdicts the same target.

    Any newer verdict supersedes: APPROVE ends the lineage (propagation
    closes the target), while a newer BLOCK owns its own cure/re-gate
    generation. Either way the older BLOCK must not replay cards. Ordering
    uses COALESCE(completed_at, created_at) so a gate created earlier but
    verdicted later still supersedes; ties are not superseded (strict >).
    """
    newer = _rows(
        conn,
        "SELECT * FROM tasks WHERE assignee = ? AND status = 'done' AND id != ? "
        "AND COALESCE(completed_at, created_at) > COALESCE(?, ?)",
        (GATEWARDEN, gate["id"], gate["completed_at"], gate["created_at"]),
    )
    for other in newer:
        other_target = _target_task_for_gate(conn, other["id"], _block_metadata(conn, other["id"]))
        if other_target == target_id:
            return True
    return False


def _implementation_assignee(target_row) -> str:
    assignee = str(target_row["assignee"] or "").strip()
    if assignee in ACTIVE_CAST and assignee not in {AUTHOR, GATEWARDEN}:
        return assignee
    return "runtimesteward"


def _cure_body(gate, target, comments: str) -> str:
    excerpt = comments.strip()
    if len(excerpt) > 3500:
        excerpt = excerpt[:3500].rstrip() + "\n... [truncated by reactive sweep; open gate card for full evidence]"
    return f"""Mission Commander reactive cure card for Gate Warden BLOCK `{gate['id']}` against `{target['id']}`.

Reactive sweep criteria matched:
- Gate task `{gate['id']}` is `done`, assigned to `gatewarden`, and carries a BLOCK verdict in run metadata or comments.
- No non-archived semantic cure card or idempotency key for this gate was found.

Target implementation lane:
- Original/right lane: `{target['assignee'] or 'unknown'}`.
- Target card status at sweep time: `{target['status']}`.

Gate BLOCK evidence excerpt:
{excerpt}

Acceptance:
- Spec Kit: `{SPEC_KIT_PACKET}`.
- Source packet: `{SOURCE_PACKET}`.
- Cure the exact BLOCK findings from `{gate['id']}`.
- Include raw evidence required by `{EVIDENCE_CONTRACT}`: diff/files or source refs, copy-pasteable repro commands, test/check output, and side-effect declaration.
- No merge/promote/deploy/external side effects unless Jared explicitly authorizes.

Completion protocol:
- If changes need review, post structured review-required handoff as a comment, then block with `review-required: ...`.
- A Gate Warden re-gate is/will be parentless/ready-on-creation; if evidence is missing, Gate Warden BLOCKs rather than waiting on a graph dependency.
"""


def _regate_body(gate, target, cure_id: str) -> str:
    return f"""Mission Commander reactive re-gate for cure card `{cure_id}` after Gate Warden BLOCK `{gate['id']}` on target `{target['id']}`.

Dependency doctrine:
- This re-gate is parentless / ready-on-creation so it cannot deadlock behind a
  cure card that may self-block with `review-required:`.
- It does NOT depend on the cure card `{cure_id}` or the original blocked/review-required target `{target['id']}`.
- If remediation evidence is not present yet, Gate Warden must BLOCK with exact
  missing evidence rather than waiting on a graph dependency.

Required verification:
- Spec Kit: `{SPEC_KIT_PACKET}`.
- Source packet: `{SOURCE_PACKET}`.
- Review the cure card `{cure_id}` and original BLOCK `{gate['id']}`.
- Verify the cure against raw evidence requirements in `{EVIDENCE_CONTRACT}`.
- Run at least one reviewer-side repro/check for T1/T2 work; do not approve summaries alone.
- Confirm no merge/promote/deploy/external side effects occurred unless Jared explicitly authorized.

Output:
- APPROVE with raw evidence refs if cured.
- BLOCK with exact missing/broken evidence or remediation if not cured.
"""


def _review_gate_body(blocked) -> str:
    reason = _latest_block_reason_for_body(blocked)
    return f"""Mission Commander reactive parentless ready-gate for review-required blocked card `{blocked['id']}`.

Reactive sweep criteria matched:
- Target card `{blocked['id']}` is `blocked`.
- Latest block/run summary begins with `review-required:`.
- No non-archived parentless Gate Warden review card for `{blocked['id']}` was found.

Doctrine:
- This review gate is parentless / ready-on-creation and intentionally does NOT depend on the blocked build card.
- Gate Warden must inspect raw evidence on the target card/workspace/comments, not just this summary.

Target:
- Title: {blocked['title']}
- Assignee: {blocked['assignee']}
- Workspace: {blocked['workspace_path'] or '(board default/scratch)'}
- Review-required reason: {reason}

Required verification:
- Spec Kit: `{SPEC_KIT_PACKET}`.
- Source packet: `{SOURCE_PACKET}`.
- Apply `{EVIDENCE_CONTRACT}`.
- Run at least one reviewer-side repro/check for T1/T2 work.
- APPROVE only with raw evidence refs; BLOCK with exact remediation if evidence/scope/checks fail.
"""


def _latest_block_reason_for_body(blocked) -> str:
    # This helper is filled by caller through the row-like dict when available.
    return str(dict(blocked).get("reactive_reason") or "").strip() or "(see target card comments/runs)"


def plan_actions(conn) -> list[Action]:
    actions: list[Action] = []
    planned_keys: set[str] = set()
    regate_cure_ids: set[str] = set()

    gate_rows = _rows(
        conn,
        "SELECT * FROM tasks WHERE assignee = ? AND status = 'done' ORDER BY completed_at DESC, created_at DESC",
        (GATEWARDEN,),
    )
    for gate in gate_rows:
        if not _is_block_verdict(conn, gate):
            continue
        metadata = _block_metadata(conn, gate["id"])
        target_id = _target_task_for_gate(conn, gate["id"], metadata)
        if not target_id:
            continue
        target = _task(conn, target_id)
        if not target:
            continue
        if str(target["status"] or "") in _TERMINAL_TARGET_STATUSES:
            # Chain closed or verdict superseded: a stale BLOCK must not
            # replay cure/re-gate work against a finished target.
            continue
        if _superseded_by_newer_gate(conn, gate, target_id):
            continue
        root_id = _lineage_root(conn, target_id)
        root = _task(conn, root_id) or target
        if str(root["status"] or "") in _TERMINAL_TARGET_STATUSES:
            # The lineage's original target is finished: generation churn on
            # intermediate cure/re-gate cards must not replay work either.
            continue
        cure_key = f"mission-reactive:cure:{root_id}"
        regate_key = f"mission-reactive:regate:{root_id}"
        # Cards minted before root-lineage keys carry per-gate keys; honor them.
        legacy_cure_key = f"mission-reactive:cure:{gate['id']}"
        legacy_regate_key = f"mission-reactive:regate:{gate['id']}"
        existing_cure = (
            _nonarchived_with_key(conn, cure_key)
            or _nonarchived_with_key(conn, legacy_cure_key)
            or _existing_semantic(conn, kind="cure", source_id=gate["id"], target_id=root_id)
        )
        existing_regate = (
            _nonarchived_with_key(conn, regate_key)
            or _nonarchived_with_key(conn, legacy_regate_key)
            or _existing_semantic(conn, kind="regate", source_id=gate["id"], target_id=root_id)
        )
        if not existing_cure and cure_key not in planned_keys:
            planned_keys.add(cure_key)
            title = f"Cure: {_base_title(root['title'])[:78]}"
            actions.append(Action(
                kind="cure",
                dedupe_key=cure_key,
                title=title,
                body=_cure_body(gate, root, _gate_evidence_text(conn, gate["id"])),
                assignee=_implementation_assignee(root),
            ))
        if (
            not existing_regate
            and regate_key not in planned_keys
            # The re-gate is deferred until the cure exists AND carries
            # reviewable evidence; a same-minute re-gate is guaranteed to
            # BLOCK ("no on-card evidence yet") and spawn generation N+1.
            and existing_cure
            and _cure_has_on_card_evidence(conn, existing_cure)
            # A reachable Gate Warden review that already references the cure
            # (e.g. a review-required ready gate) satisfies the re-gate need.
            and not _existing_regate_child_for_blocked(conn, existing_cure)
        ):
            planned_keys.add(regate_key)
            regate_cure_ids.add(existing_cure)
            title = f"Re-gate: {_base_title(root['title'])[:75]}"
            actions.append(Action(
                kind="regate",
                dedupe_key=regate_key,
                title=title,
                body=_regate_body(gate, root, existing_cure),
                assignee=GATEWARDEN,
                parents=(),
            ))

    blocked_rows = _rows(conn, "SELECT * FROM tasks WHERE status = 'blocked' ORDER BY created_at")
    for row in blocked_rows:
        run = _latest_run(conn, row["id"])
        reason = _latest_block_reason(conn, row["id"])
        summary = str(run["summary"] or "") if run else ""
        if not (reason.startswith("review-required:") or summary.startswith("review-required:")):
            continue
        key = f"mission-reactive:review-gate:{row['id']}"
        if str(row["title"] or "").lower().startswith("cure:") and (
            row["id"] in regate_cure_ids or _existing_regate_child_for_blocked(conn, row["id"])
        ):
            continue
        if _nonarchived_with_key(conn, key) or _existing_semantic(conn, kind="review_gate", source_id=row["id"], target_id=row["id"]):
            continue
        d = dict(row)
        d["reactive_reason"] = reason or summary
        actions.append(Action(
            kind="review_gate",
            dedupe_key=key,
            title=f"Ready gate: review-required handoff for {_base_title(row['title'])[:58]}",
            body=_review_gate_body(d),
            assignee=GATEWARDEN,
            parents=(),
        ))
    return actions


def plan_approve_propagations(conn) -> list[ApprovePropagation]:
    """Plan APPROVE verdict applications for blocked review-required targets."""
    propagations: list[ApprovePropagation] = []
    gate_rows = _rows(
        conn,
        "SELECT * FROM tasks WHERE assignee = ? AND status = 'done' ORDER BY completed_at DESC, created_at DESC",
        (GATEWARDEN,),
    )
    for gate in gate_rows:
        if not _is_approve_verdict(conn, gate):
            continue
        metadata = _block_metadata(conn, gate["id"])
        immediate_target = _target_task_for_gate(conn, gate["id"], metadata)
        if not immediate_target:
            continue
        # Resolve through the cure/re-gate lineage to the ORIGINAL blocked build,
        # mirroring plan_actions. A re-gate's metadata target_task is the cure card
        # (which completes and goes `done`), so without this the APPROVE never lands
        # on the real build and it is stranded blocked forever (audit finding #4:
        # t_1d942c15 approved-but-blocked behind done cure t_2f219b8f).
        target_id = _lineage_root(conn, immediate_target)
        target = _task(conn, target_id)
        if not target or not _is_review_required_blocked(conn, target):
            continue
        marker = f"mission-reactive:approve:{gate['id']}:{target_id}"
        if marker in _comments_text(conn, gate["id"]) or marker in _comments_text(conn, target_id):
            continue
        propagations.append(ApprovePropagation(
            gate_id=gate["id"],
            target_id=target_id,
            summary=f"Gate Warden APPROVE applied by reactive sweep from `{gate['id']}`.",
        ))
    return propagations


def apply_approve_propagations(conn, propagations: list[ApprovePropagation]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for propagation in propagations:
        gate = _task(conn, propagation.gate_id)
        target = _task(conn, propagation.target_id)
        if not gate or not target or not _is_review_required_blocked(conn, target):
            results.append({"kind": "approve_propagation", "gate_id": propagation.gate_id, "target_id": propagation.target_id, "applied": False, "reason": "not-applicable"})
            continue
        marker = f"mission-reactive:approve:{propagation.gate_id}:{propagation.target_id}"
        comment = (
            f"APPROVE propagated by Mission Engine reactive sweep.\n"
            f"Marker: {marker}\n"
            f"Gate: `{propagation.gate_id}`\n"
            f"Target: `{propagation.target_id}`\n"
            f"Summary: {propagation.summary}"
        )
        if marker not in _comments_text(conn, propagation.gate_id):
            kb.add_comment(conn, propagation.gate_id, AUTHOR, comment)
        if marker not in _comments_text(conn, propagation.target_id):
            kb.add_comment(conn, propagation.target_id, AUTHOR, comment)
        completed = kb.complete_task(
            conn,
            propagation.target_id,
            result=propagation.summary,
            summary=propagation.summary,
            metadata={"verdict": "APPROVE", "approved_by_gate": propagation.gate_id, "applied_by": "mission_engine_reactive_sweep"},
        )
        results.append({"kind": "approve_propagation", "gate_id": propagation.gate_id, "target_id": propagation.target_id, "applied": bool(completed), "marker": marker})
    return results


def apply_actions(conn, actions: list[Action]) -> list[dict[str, Any]]:
    created_by_key: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    for action in actions:
        parents = tuple(created_by_key.get(p.removeprefix("<created-by:").removesuffix(">"), p) if p.startswith("<created-by:") else p for p in action.parents)
        body = action.body
        for key, task_id in created_by_key.items():
            body = body.replace(f"<created-by:{key}>", task_id)
        existing = _nonarchived_with_key(conn, action.dedupe_key)
        if existing:
            results.append({"kind": action.kind, "task_id": existing, "created": False, "dedupe_key": action.dedupe_key})
            created_by_key[action.dedupe_key] = existing
            continue
        task_id = kb.create_task(
            conn,
            title=action.title,
            body=body,
            assignee=action.assignee,
            created_by=AUTHOR,
            parents=parents,
            priority=action.priority,
            idempotency_key=action.dedupe_key,
            board=BOARD,
        )
        created_by_key[action.dedupe_key] = task_id
        results.append({"kind": action.kind, "task_id": task_id, "created": True, "dedupe_key": action.dedupe_key, "parents": list(parents)})
    promoted = kb.recompute_ready(conn)
    if promoted:
        results.append({"kind": "recompute_ready", "promoted": promoted})
    return results


def _open_gate_exists(conn, root_id: str) -> bool:
    """True if a non-terminal gatewarden gate references this lineage root by id."""
    rows = _rows(
        conn,
        "SELECT id, title, body FROM tasks WHERE assignee = ? AND status IN ('todo', 'ready', 'running')",
        (GATEWARDEN,),
    )
    return any(root_id in f"{row['title'] or ''} {row['body'] or ''}" for row in rows)


def detect_stalls(conn) -> list[dict[str, Any]]:
    """Blocked cards the sweep cannot advance and that await a human.

    Only meaningful when the sweep plans zero actions: it distinguishes a
    genuinely converged board from a frozen one. A card is stalled when it is
    review-required-blocked and no open (todo/ready/running) gatewarden gate
    exists for its lineage to unblock it. Detection/reporting only — no card is
    minted here; the escalation-delivery half is the observability cluster's job
    (audit #5/#17: a 0-action sweep must not be indistinguishable from converged).
    """
    stalls: list[dict[str, Any]] = []
    for row in _rows(conn, "SELECT * FROM tasks WHERE status = 'blocked' ORDER BY created_at"):
        if not _is_review_required_blocked(conn, row):
            continue
        root = _lineage_root(conn, row["id"])
        if _open_gate_exists(conn, root) or _open_gate_exists(conn, row["id"]):
            continue
        stalls.append({
            "task_id": row["id"],
            "lineage_root": root,
            "title": str(row["title"] or "")[:80],
            "reason": "review-required-blocked; no open gate and no sweep action — awaiting human",
        })
    return stalls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reactive Mission Commander sweep for Spec 002 Kanban board")
    parser.add_argument("--board", default=BOARD, help=f"Board slug (default: {BOARD})")
    parser.add_argument("--apply", action="store_true", help="Create missing cards. Omit for dry-run.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--quiet-if-empty", action="store_true", help="Print nothing when no actions are needed")
    args = parser.parse_args(argv)

    if args.board != BOARD:
        raise SystemExit(f"This sweep is intentionally scoped to {BOARD!r}; got {args.board!r}")
    conn = kb.connect(board=BOARD)
    actions = plan_actions(conn)
    approve_propagations = plan_approve_propagations(conn)
    total_planned = len(actions) + len(approve_propagations)
    # Only compute stalls when the sweep plans nothing — that is precisely when a
    # bare "0 actions" would otherwise be indistinguishable from a converged board.
    stalls = [] if total_planned else detect_stalls(conn)
    health = "active" if total_planned else ("stalled" if stalls else "converged")
    if args.apply:
        applied_actions = apply_actions(conn, actions) + apply_approve_propagations(conn, approve_propagations)
        result = {"mode": "apply", "board": BOARD, "actions_planned": total_planned, "actions": applied_actions, "health": health, "stalls": stalls}
    else:
        dry_run_actions = [a.__dict__ for a in actions] + [p.__dict__ | {"kind": "approve_propagation"} for p in approve_propagations]
        result = {"mode": "dry-run", "board": BOARD, "actions_planned": len(dry_run_actions), "actions": dry_run_actions, "health": health, "stalls": stalls}
    if args.json:
        if args.quiet_if_empty and not actions and not approve_propagations:
            return 0
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if not actions and not approve_propagations:
            if args.quiet_if_empty:
                return 0
            print(f"mission reactive sweep: no missing cards on board {BOARD}")
        else:
            print(f"mission reactive sweep: {len(actions) + len(approve_propagations)} action(s) in {result['mode']} on board {BOARD}")
            for item in result["actions"]:
                print(json.dumps(item, sort_keys=True) if isinstance(item, dict) else item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
