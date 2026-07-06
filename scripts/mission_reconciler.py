#!/usr/bin/env python3
"""Mission Engine RECONCILER (rework program P4, 2026-07-05).

Replaces the edge-triggered ``scripts/mission_engine_reactive_sweep.py``
(mint-cards-to-fix-cards; produced the 130-cards-in-4h livelock on Jul 4)
with an idempotent, level-triggered state reconciliation loop:

* One pass per invocation (cron/tick friendly). For every non-terminal
  lineage on the mission board it computes desired-vs-actual and emits a
  PLAN of transitions instead of minting repair cards by default.
* Sources of truth, in order: structured ``task_verdicts`` rows first,
  THEN legacy comment-prose verdicts recovered with the old sweep's
  regexes (transition period; flagged ``legacy_source: true``).
* Repair recycles the existing card (needs-work feedback + unblock) —
  a NEW card is proposed only when repair requires genuinely new labor
  (e.g. a parentless Gate Warden review for a review-required handoff
  that has no reviewer). Every proposed mint carries a
  ``defect_fingerprint = sha256(lineage_root + ':' + defect_class + ':'
  + normalized_target)`` and must INSERT into ``defect_fingerprints``
  BEFORE the card is created; a UNIQUE violation means the repair was
  already attempted → the action becomes ``suppressed-duplicate`` and is
  escalated as a stall-report instead of re-minted. The reconciler never
  fights the same defect twice — that is the anti-livelock core.
* Circuit breaker: before executing ANY mints, cards created in the last
  hour are counted per ``created_by``; any source above the threshold
  (default 20/h — rework research number) aborts all minting this pass
  and emits ``CIRCUIT-BREAKER`` in the output.
* Modes: ``--plan`` (default; print plan JSON, mutate NOTHING),
  ``--apply`` (execute plan actions via kanban_db PUBLIC mutators only),
  ``--shadow-compare`` (run the plan AND the old sweep's detection
  in-process, print divergences for the soak).
* There is deliberately NO ``--quiet-if-empty`` flag: an empty plan
  prints ``{"actions": [], "health": {...}}`` — silence must never be
  ambiguous (audit #5/#17).

Hard-rule compliance: this module only READS the board schema directly;
all task mutations go through ``hermes_cli.kanban_db`` public functions
(``complete_task``, ``unblock_task``, ``promote_task``, ``add_comment``,
``create_task``, ``recompute_ready``). Actions that would need a public
function that does not exist are planned as ``requires_orchestrator``.
``defect_fingerprints`` is a reconciler-owned coordination table (schema
contract copy; ``CREATE TABLE IF NOT EXISTS`` keeps both sides
compatible, same pattern as ``hermes_cli.engine_health``).

Env:

* ``HERMES_RECONCILER_DISABLE=1`` — kill-switch: prints an empty plan
  with ``health.disabled = true`` and exits 0.
* ``HERMES_RECONCILER_BREAKER_THRESHOLD`` — mint circuit-breaker
  cards/hour threshold (default 20).
* ``HERMES_LIVENESS_BEATS=0`` — (honored inside engine_health) disables
  the heartbeat write.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_db as kb  # noqa: E402

_log = logging.getLogger("mission_reconciler")

DEFAULT_BOARD = "fable-emulation-workflow"
#: Minting identity. The Spec 002 guardrail policy's goal_mode_handoff
#: preflight rejects parentless mission-shaped cards whose created_by is not
#: the commander role, so reconciler mints ride the commander identity (same
#: as the old sweep). Reconciler provenance is carried by the
#: ``reconciler:`` idempotency-key prefix and an explicit body marker.
AUTHOR = "missioncommander"
ACTOR = "mission_reconciler"
GATEWARDEN = "gatewarden"
COMPONENT = "reconciler"
BEAT_INTERVAL_SECONDS = 1800
EVIDENCE_CONTRACT = "/home/red/.hermes/specs/002-mission-engine/evidence-contract.md"

DISABLE_ENV = "HERMES_RECONCILER_DISABLE"
BREAKER_THRESHOLD_ENV = "HERMES_RECONCILER_BREAKER_THRESHOLD"
BREAKER_THRESHOLD_DEFAULT = 20
BREAKER_WINDOW_SECONDS = 3600

OVERHEAD_ALARM_THRESHOLD = 0.15  # rework research: overhead SLO alarm >15%

#: Grace before a todo card with fully-terminal parents counts as
#: wake-overdue (avoid racing the dispatcher's own recompute_ready tick).
WAKE_GRACE_SECONDS = 900

#: Stall SLO defaults (rework research: dependency 1h, review-required 2h,
#: needs_input/human 24h). Used when tasks.wake_deadline is absent/NULL.
STALL_SLO_REVIEW_SECONDS = 2 * 3600
STALL_SLO_HUMAN_SECONDS = 24 * 3600
STALL_SLO_DEPENDENCY_SECONDS = 3600

TERMINAL_STATUSES = {"done", "archived"}
_TRUTHY = {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Legacy regexes — ported verbatim from scripts/mission_engine_reactive_sweep.py
# (transition-period verdict recovery; facts carry legacy_source=True).
# ---------------------------------------------------------------------------
_CURE_LINEAGE_RE = re.compile(
    r"cure card for Gate Warden BLOCK `t_[0-9a-f]+` against `(t_[0-9a-f]+)`"
)
_REGATE_LINEAGE_RE = re.compile(
    r"Gate Warden BLOCK `t_[0-9a-f]+` on target `(t_[0-9a-f]+)`"
)
_BLOCK_PROSE_RE = re.compile(r"(^|\n)\s*BLOCK\s*:", re.IGNORECASE)
_APPROVE_PROSE_RE = re.compile(r"(^|\n)\s*APPROVE\b", re.IGNORECASE)
_EXPLICIT_TARGET_PATTERNS = (
    r"target[_ -]task[`:\s]+(t_[0-9a-f]+)",
    r"runtime card [`']?(t_[0-9a-f]+)",
    r"review (?:blocked build|target) [`']?(t_[0-9a-f]+)`?",
    r"original (?:build|runtime).*?`(t_[0-9a-f]+)`",
    r"on original build [`']?(t_[0-9a-f]+)",
)
_MINTED_TITLE_PREFIX_RE = re.compile(r"^(?:(?:cure|re-?gate)\s*:\s*)+", re.IGNORECASE)

#: Governance-overhead title classifier for the board health block
#: (gate/cure/routing/hygiene fraction; alarm above 0.15).
_OVERHEAD_TITLE_RE = re.compile(
    r"\b(?:re-?gate[sd]?|gates?|cure[sd]?|routing|hygiene)\b", re.IGNORECASE
)

STRUCTURED_SOURCE = "task_verdicts"
LEGACY_SOURCE = "legacy-prose"


# ---------------------------------------------------------------------------
# Small read helpers (READ-ONLY board access; mutation is kanban_db-only)
# ---------------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()):
    return conn.execute(sql, tuple(params)).fetchall()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return False
    return column in cols


def _json_loads(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _task(conn: sqlite3.Connection, task_id: str):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def _comments_text(conn: sqlite3.Connection, task_id: str) -> str:
    rows = _rows(
        conn, "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
    )
    return "\n".join(str(r["body"] or "") for r in rows)


def _latest_run(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()


def _run_metadata(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    run = _latest_run(conn, task_id)
    metadata = _json_loads(run["metadata"] if run else None)
    return metadata if isinstance(metadata, dict) else {}


def _gate_card_and_run_text(conn: sqlite3.Connection, gate_id: str) -> str:
    """Gate title/body/result plus the closing run handoff (old-sweep parity)."""
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


def _gate_evidence_text(conn: sqlite3.Connection, gate_id: str) -> str:
    parts = [_gate_card_and_run_text(conn, gate_id), _comments_text(conn, gate_id)]
    return "\n".join(p for p in parts if p)


def _latest_blocked_event(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _blocked_payload(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = _latest_blocked_event(conn, task_id)
    payload = _json_loads(row["payload"]) if row else None
    return payload if isinstance(payload, dict) else {}


def _last_blocked_at(conn: sqlite3.Connection, task_id: str) -> int:
    """When the card last entered ``blocked`` (verdict freshness anchor)."""
    row = _latest_blocked_event(conn, task_id)
    if row and row["created_at"]:
        return int(row["created_at"])
    run = _latest_run(conn, task_id)
    if run and run["ended_at"]:
        return int(run["ended_at"])
    task = _task(conn, task_id)
    return int(task["created_at"]) if task else 0


def _block_reason(conn: sqlite3.Connection, task_id: str) -> str:
    reason = str(_blocked_payload(conn, task_id).get("reason") or "")
    if reason:
        return reason
    run = _latest_run(conn, task_id)
    return str(run["summary"] or "") if run else ""


def _is_review_required_blocked(conn: sqlite3.Connection, row) -> bool:
    if str(row["status"] or "") != "blocked":
        return False
    reason = str(_blocked_payload(conn, row["id"]).get("reason") or "")
    run = _latest_run(conn, row["id"])
    summary = str(run["summary"] or "") if run else ""
    return reason.startswith("review-required:") or summary.startswith("review-required:")


def lineage_root(conn: sqlite3.Connection, task_id: str) -> str:
    """Follow sweep-minted cure/re-gate body pointers back to the original
    target (old sweep's ``_lineage_root``; keeps fingerprints and verdict
    application stable across BLOCK -> cure -> re-gate generations)."""
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


def _base_title(raw: Any) -> str:
    title = str(raw or "").strip()
    stripped = _MINTED_TITLE_PREFIX_RE.sub("", title).strip()
    return stripped or title


def normalize_target(target: str) -> str:
    return str(target or "").strip().lower()


def defect_fingerprint(root: str, defect_class: str, target: str) -> str:
    """``sha256(lineage_root + ':' + defect_class + ':' + normalized_target)``."""
    raw = f"{root}:{defect_class}:{normalize_target(target)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Verdict fact gathering
# ---------------------------------------------------------------------------

def _legacy_verdict(conn: sqlite3.Connection, gate_row) -> Optional[str]:
    """Single-verdict merge of the old sweep's ``_is_block_verdict`` /
    ``_is_approve_verdict``: structured run metadata wins, then the latest
    card/run handoff text, then comments. Ambiguous (both markers at the
    same precedence tier) → ``None``."""
    metadata = _run_metadata(conn, gate_row["id"])
    verdict = str(metadata.get("verdict") or "").upper()
    if verdict in ("APPROVE", "BLOCK"):
        return verdict

    latest = _gate_card_and_run_text(conn, gate_row["id"])
    latest_approve = bool(_APPROVE_PROSE_RE.search(latest))
    latest_block = bool(_BLOCK_PROSE_RE.search(latest))
    if latest_approve != latest_block:
        return "APPROVE" if latest_approve else "BLOCK"
    if latest_approve and latest_block:
        return None

    comments = _comments_text(conn, gate_row["id"])
    c_approve = bool(_APPROVE_PROSE_RE.search(comments))
    c_block = bool(_BLOCK_PROSE_RE.search(comments))
    if c_approve != c_block:
        return "APPROVE" if c_approve else "BLOCK"
    return None


def _legacy_target(conn: sqlite3.Connection, gate_id: str, metadata: dict[str, Any]) -> Optional[str]:
    """Old sweep's ``_target_task_for_gate`` (metadata keys, then explicit
    prose patterns, then first resolvable task id)."""
    for key in ("target_task", "target", "blocked_task", "blocked_build", "runtime_card"):
        value = metadata.get(key)
        if isinstance(value, str) and re.fullmatch(r"t_[0-9a-f]+", value):
            return value
    primary_text = _gate_card_and_run_text(conn, gate_id)
    comments_text = _comments_text(conn, gate_id)
    for text in (primary_text, comments_text):
        for pattern in _EXPLICIT_TARGET_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1)
    for text in (primary_text, comments_text):
        for tid in re.findall(r"t_[0-9a-f]+", text):
            if tid != gate_id and _task(conn, tid):
                return tid
    return None


def gather_verdict_facts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All verdict facts on the board — structured rows first, legacy prose
    second. Per-lineage precedence is applied later in :func:`build_plan`."""
    facts: list[dict[str, Any]] = []

    if _table_exists(conn, "task_verdicts"):
        for r in _rows(conn, "SELECT * FROM task_verdicts ORDER BY created_at, id"):
            facts.append({
                "verdict": str(r["verdict"] or "").upper(),
                "target_id": str(r["target_task_id"] or ""),
                "origin_id": str(r["task_id"] or ""),
                "created_at": int(r["created_at"] or 0),
                "source": STRUCTURED_SOURCE,
                "legacy_source": False,
                "waive_authority": r["waive_authority"],
                "feedback": (
                    f"task_verdicts row id={r['id']} verdict={r['verdict']}"
                    f" tier={r['tier']} reviewer_model={r['reviewer_model']}"
                    f" cross_model={r['cross_model']}"
                ),
            })

    for gate in _rows(
        conn,
        "SELECT * FROM tasks WHERE assignee = ? AND status = 'done' "
        "ORDER BY COALESCE(completed_at, created_at), created_at",
        (GATEWARDEN,),
    ):
        verdict = _legacy_verdict(conn, gate)
        if verdict is None:
            continue
        target_id = _legacy_target(conn, gate["id"], _run_metadata(conn, gate["id"]))
        if not target_id:
            continue
        excerpt = _gate_evidence_text(conn, gate["id"]).strip()
        if len(excerpt) > 1200:
            excerpt = excerpt[:1200].rstrip() + "\n... [truncated by reconciler]"
        facts.append({
            "verdict": verdict,
            "target_id": target_id,
            "origin_id": gate["id"],
            "created_at": int(gate["completed_at"] or gate["created_at"] or 0),
            "source": LEGACY_SOURCE,
            "legacy_source": True,
            "waive_authority": None,
            "feedback": excerpt,
        })
    return facts


# ---------------------------------------------------------------------------
# Plan construction (mutates NOTHING)
# ---------------------------------------------------------------------------

def _breaker_threshold() -> int:
    raw = os.environ.get(BREAKER_THRESHOLD_ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            _log.warning("ignoring non-integer %s=%r", BREAKER_THRESHOLD_ENV, raw)
    return BREAKER_THRESHOLD_DEFAULT


def compute_breaker(conn: sqlite3.Connection, now: int) -> dict[str, Any]:
    """Cards created in the last hour per ``created_by`` (mint circuit
    breaker; rework research: page when any single source mints >20/h)."""
    threshold = _breaker_threshold()
    counts: dict[str, int] = {}
    for r in _rows(
        conn,
        "SELECT COALESCE(created_by, '(unknown)') AS src, COUNT(*) AS n "
        "FROM tasks WHERE created_at > ? GROUP BY src",
        (now - BREAKER_WINDOW_SECONDS,),
    ):
        counts[str(r["src"])] = int(r["n"])
    tripped = sorted(src for src, n in counts.items() if n > threshold)
    return {
        "state": "tripped" if tripped else "armed",
        "threshold_per_hour": threshold,
        "window_seconds": BREAKER_WINDOW_SECONDS,
        "sources": counts,
        "tripped_sources": tripped,
    }


def _overhead_ratio(conn: sqlite3.Connection) -> tuple[float, int, int]:
    rows = _rows(conn, "SELECT title FROM tasks")
    total = len(rows)
    if not total:
        return 0.0, 0, 0
    overhead = sum(1 for r in rows if _OVERHEAD_TITLE_RE.search(str(r["title"] or "")))
    return overhead / total, overhead, total


def _open_gate_exists(conn: sqlite3.Connection, ref_id: str) -> bool:
    """Non-terminal gatewarden card referencing ``ref_id`` (old-sweep parity)."""
    rows = _rows(
        conn,
        "SELECT title, body FROM tasks WHERE assignee = ? "
        "AND status IN ('todo', 'ready', 'running')",
        (GATEWARDEN,),
    )
    return any(ref_id in f"{r['title'] or ''} {r['body'] or ''}" for r in rows)


def _fingerprint_seen(conn: sqlite3.Connection, root: str, fingerprint: str) -> bool:
    if not _table_exists(conn, "defect_fingerprints"):
        return False
    row = conn.execute(
        "SELECT 1 FROM defect_fingerprints WHERE lineage_root = ? AND fingerprint = ?",
        (root, fingerprint),
    ).fetchone()
    return row is not None


def _stall_deadline(conn: sqlite3.Connection, row, has_wake_deadline: bool) -> tuple[int, str]:
    """(deadline_epoch, basis). ``wake_deadline`` column is authoritative when
    present/set; otherwise SLO defaults keyed off the block reason/kind."""
    if has_wake_deadline:
        try:
            wd = row["wake_deadline"]
        except (IndexError, KeyError):
            wd = None
        if wd:
            return int(wd), "wake_deadline"
    blocked_at = _last_blocked_at(conn, row["id"])
    payload = _blocked_payload(conn, row["id"])
    reason = str(payload.get("reason") or "")
    kind = str(payload.get("kind") or "")
    if reason.startswith("review-required:"):
        return blocked_at + STALL_SLO_REVIEW_SECONDS, "slo:review-required"
    if kind in ("needs_input", "human", "capability") or reason.startswith("approval-required:"):
        return blocked_at + STALL_SLO_HUMAN_SECONDS, "slo:human"
    return blocked_at + STALL_SLO_DEPENDENCY_SECONDS, "slo:dependency"


def _verdict_action(
    conn: sqlite3.Connection, root: str, fact: dict[str, Any], now: int
) -> Optional[dict[str, Any]]:
    """Desired-vs-actual for one lineage's freshest verdict fact."""
    target_id = fact["target_id"]
    target = _task(conn, target_id)
    effective = None
    if target is not None and str(target["status"] or "") == "blocked":
        effective = target
    elif root != target_id:
        root_row = _task(conn, root)
        if root_row is not None and str(root_row["status"] or "") == "blocked":
            effective = root_row
    if effective is None:
        return None  # desired already true, target in flight, or nothing actionable

    root_row = _task(conn, root)
    if root_row is not None and str(root_row["status"] or "") in TERMINAL_STATUSES and root != effective["id"]:
        return None  # lineage closed; stale generation churn must not replay

    # Freshness guard: a verdict older than the card's latest block belongs to
    # a previous generation — acting on it re-creates the unblock/re-block
    # livelock the old sweep suffered from. Stale verdicts fall through to the
    # mint/stall evaluation instead.
    blocked_at = _last_blocked_at(conn, effective["id"])
    if int(fact["created_at"]) < blocked_at:
        return None

    base = {
        "target": effective["id"],
        "target_title": _base_title(effective["title"])[:80],
        "lineage_root": root,
        "verdict": fact["verdict"],
        "verdict_origin": fact["origin_id"],
        "verdict_source": fact["source"],
        "legacy_source": bool(fact["legacy_source"]),
        "verdict_created_at": int(fact["created_at"]),
    }

    verdict = fact["verdict"]
    if verdict == "WAIVED" and not (fact.get("waive_authority") or "").strip():
        return {
            **base,
            "type": "requires_orchestrator",
            "requested_operation": "resolve-waived-verdict-missing-authority",
            "reason": (
                "task_verdicts row is WAIVED but waive_authority (Jared packet id) "
                "is empty; schema contract requires it — reconciler will not apply."
            ),
        }

    if verdict in ("APPROVE", "WAIVED"):
        if fact["legacy_source"] and not _is_review_required_blocked(conn, effective):
            return None  # prose APPROVE only closes review-required blocks
        return {
            **base,
            "type": "apply-APPROVE",
            "result": (
                f"Gate verdict {verdict} applied by {ACTOR} from "
                f"{fact['source']} origin `{fact['origin_id']}`."
            ),
        }

    if verdict in ("NEEDS_WORK", "REJECT", "BLOCK"):
        if fact["legacy_source"] and not _is_review_required_blocked(conn, effective):
            return None  # prose BLOCK must not recycle capability/needs_input blocks
        feedback = str(fact.get("feedback") or "").strip()
        return {
            **base,
            "type": "apply-NEEDS_WORK",
            "feedback": feedback[:1500],
            "wiring_gap": (
                "needs_work_count not incremented: no public kanban_db function "
                "(see WIRING — kanban_db.record_needs_work)"
            ),
        }

    return None


def build_plan(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Level-triggered reconcile plan for one board. READ-ONLY."""
    now = int(now if now is not None else time.time())
    actions: list[dict[str, Any]] = []
    lineages: set[str] = set()
    handled: set[str] = set()

    # --- verdict facts, bucketed per lineage root -------------------------
    facts = gather_verdict_facts(conn)
    structured_count = sum(1 for f in facts if not f["legacy_source"])
    by_root: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        root = lineage_root(conn, fact["target_id"])
        by_root.setdefault(root, []).append(fact)
        lineages.add(root)

    for root in sorted(by_root):
        pool = [f for f in by_root[root] if not f["legacy_source"]] or by_root[root]
        fact = max(pool, key=lambda f: (int(f["created_at"]), f["origin_id"]))
        action = _verdict_action(conn, root, fact, now)
        if action:
            actions.append(action)
            handled.add(action["target"])

    # --- wake-overdue: todo cards whose parents are all terminal ----------
    for row in _rows(
        conn,
        "SELECT t.* FROM tasks t WHERE t.status = 'todo' AND t.created_at < ? "
        "AND NOT EXISTS (SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = t.id AND p.status NOT IN ('done', 'archived')) "
        "ORDER BY t.created_at",
        (now - WAKE_GRACE_SECONDS,),
    ):
        root = lineage_root(conn, row["id"])
        lineages.add(root)
        actions.append({
            "type": "wake-overdue",
            "target": row["id"],
            "target_title": _base_title(row["title"])[:80],
            "lineage_root": root,
            "reason": "todo with all parent dependencies terminal; promote via kanban_db.promote_task",
        })
        handled.add(row["id"])

    # --- mints (genuinely new labor only) + stall reports ------------------
    # Every blocked card lands in health.blocked_inventory with a disposition,
    # even when no action fires — silence must never be ambiguous (audit #5/#17).
    has_wake_deadline = _column_exists(conn, "tasks", "wake_deadline")
    blocked_inventory: list[dict[str, Any]] = []
    blocked_rows = _rows(conn, "SELECT * FROM tasks WHERE status = 'blocked' ORDER BY created_at")
    for row in blocked_rows:
        inv = {
            "target": row["id"],
            "target_title": _base_title(row["title"])[:80],
            "block_kind": str(_blocked_payload(conn, row["id"]).get("kind") or ""),
            "blocked_at": _last_blocked_at(conn, row["id"]),
        }
        blocked_inventory.append(inv)
        if row["id"] in handled:
            inv["disposition"] = "verdict-action-planned"
            continue
        root = lineage_root(conn, row["id"])
        lineages.add(root)
        stall_extra: Optional[str] = None

        if _is_review_required_blocked(conn, row) and not (
            _open_gate_exists(conn, row["id"]) or (root != row["id"] and _open_gate_exists(conn, root))
        ):
            # Review-required handoff with no reviewer anywhere: repair needs
            # genuinely new labor (a parentless Gate Warden review card).
            fp = defect_fingerprint(root, "review-gate", row["id"])
            mint = {
                "type": "mint",
                "defect_class": "review-gate",
                "target": row["id"],
                "target_title": _base_title(row["title"])[:80],
                "lineage_root": root,
                "defect_fingerprint": fp,
                "assignee": GATEWARDEN,
                "title": f"Ready gate: review-required handoff for {_base_title(row['title'])[:58]}",
                "idempotency_key": f"reconciler:review-gate:{root}",
                "reason": "review-required blocked with no open Gate Warden card for target or lineage root",
            }
            if _fingerprint_seen(conn, root, fp):
                mint["type"] = "suppressed-duplicate"
                mint["reason"] = (
                    "defect fingerprint already recorded — repair already attempted; "
                    "never re-mint on suppression"
                )
                actions.append(mint)
                inv["disposition"] = "suppressed-duplicate"
                stall_extra = "repair already attempted (fingerprint suppressed) and card is still blocked"
            else:
                actions.append(mint)
                inv["disposition"] = "mint-proposed"
                handled.add(row["id"])
                continue

        deadline, basis = _stall_deadline(conn, row, has_wake_deadline)
        inv["deadline"] = deadline
        inv["deadline_basis"] = basis
        inv.setdefault("disposition", "stalled" if deadline < now else "within-slo")
        if deadline < now or stall_extra:
            payload = _blocked_payload(conn, row["id"])
            actions.append({
                "type": "stall-report",
                "target": row["id"],
                "target_title": _base_title(row["title"])[:80],
                "lineage_root": root,
                "blocked_at": _last_blocked_at(conn, row["id"]),
                "deadline": deadline,
                "deadline_basis": basis,
                "overdue_seconds": max(0, now - deadline),
                "block_kind": str(payload.get("kind") or ""),
                "reason": (stall_extra or str(payload.get("reason") or _block_reason(conn, row["id"])))[:300],
            })
        handled.add(row["id"])

    breaker = compute_breaker(conn, now)
    ratio, overhead_cards, total_cards = _overhead_ratio(conn)
    health = {
        "board": board or "(db-path)",
        "boards_scanned": 1,
        "generated_at": now,
        "lineages": len(lineages),
        "blocked_cards": len(blocked_inventory),
        "blocked_inventory": blocked_inventory,
        "actions_by_type": dict(Counter(a["type"] for a in actions)),
        "breaker": breaker,
        "overhead_ratio": round(ratio, 4),
        "overhead_cards": overhead_cards,
        "total_cards": total_cards,
        "overhead_alarm": ratio > OVERHEAD_ALARM_THRESHOLD,
        "overhead_alarm_threshold": OVERHEAD_ALARM_THRESHOLD,
        "verdict_sources": {
            "structured": structured_count,
            "legacy_prose": len(facts) - structured_count,
        },
        "schema": {
            "task_verdicts": _table_exists(conn, "task_verdicts"),
            "defect_fingerprints": _table_exists(conn, "defect_fingerprints"),
            "wake_deadline_column": has_wake_deadline,
        },
    }
    return {
        "schema_version": 1,
        "board": board or "(db-path)",
        "generated_at": now,
        "actions": actions,
        "health": health,
    }


# ---------------------------------------------------------------------------
# Apply (kanban_db public mutators only; direct SQL only on the
# reconciler-owned defect_fingerprints coordination table)
# ---------------------------------------------------------------------------

_DEFECT_FINGERPRINTS_DDL = """
CREATE TABLE IF NOT EXISTS defect_fingerprints (
  lineage_root TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  card_id TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(lineage_root, fingerprint)
)
"""


def _ensure_defect_fingerprints(conn: sqlite3.Connection) -> None:
    """Schema-contract copy (same CREATE TABLE IF NOT EXISTS pattern as
    ``engine_health``); the orchestrator creates the same table in kanban_db."""
    with kb.write_txn(conn):
        conn.execute(_DEFECT_FINGERPRINTS_DDL)


def _mint_body(action: dict[str, Any], target_row) -> str:
    reason = _base_title(action.get("reason") or "")
    return f"""Mission Engine reconciler (rework P4) parentless ready-gate for review-required blocked card `{action['target']}`.

Reconciler provenance:
- minted-by: {ACTOR}
- lineage_root: `{action['lineage_root']}`
- defect_fingerprint: {action['defect_fingerprint']}
- plan reason: {reason}

Doctrine:
- This review gate is parentless / ready-on-creation and intentionally does NOT depend on the blocked target.
- Gate Warden must inspect raw evidence on the target card/workspace/comments, not just this summary.

Target:
- Title: {target_row['title']}
- Assignee: {target_row['assignee']}
- Status at reconcile time: {target_row['status']}

Required verification:
- Apply `{EVIDENCE_CONTRACT}`.
- Run at least one reviewer-side repro/check for T1/T2 work.
- Record the verdict as a structured task_verdicts row targeting `{action['target']}` (APPROVE or NEEDS_WORK with exact findings); comment-prose verdicts are transition-period only.
"""


def apply_plan(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    *,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Execute plan actions. Mints go through the defect-fingerprint UNIQUE
    gate and the created-cards/hour circuit breaker; everything else uses
    kanban_db public mutators. Report-only actions pass through unchanged."""
    now = int(now if now is not None else time.time())
    executed: list[dict[str, Any]] = []
    alerts: list[str] = []

    breaker = plan.get("health", {}).get("breaker") or compute_breaker(conn, now)
    breaker_tripped = breaker.get("state") == "tripped"
    mint_planned = any(a["type"] == "mint" for a in plan.get("actions", ()))
    if breaker_tripped and mint_planned:
        alerts.append("CIRCUIT-BREAKER")
        _log.warning(
            "CIRCUIT-BREAKER: mint sources over threshold %s — aborting all mints this pass: %s",
            breaker.get("threshold_per_hour"), breaker.get("tripped_sources"),
        )

    for action in plan.get("actions", ()):  # single pass, in plan order
        kind = action["type"]

        if kind == "apply-APPROVE":
            ok = kb.complete_task(
                conn,
                action["target"],
                result=action["result"],
                summary=action["result"],
                metadata={
                    "verdict": action["verdict"],
                    "applied_by": ACTOR,
                    "verdict_origin": action["verdict_origin"],
                    "verdict_source": action["verdict_source"],
                    "legacy_source": action["legacy_source"],
                },
            )
            executed.append({**action, "executed": bool(ok)})

        elif kind == "apply-NEEDS_WORK":
            marker = f"mission-reconciler:needs-work:{action['verdict_origin']}:{action['target']}"
            if marker not in _comments_text(conn, action["target"]):
                kb.add_comment(
                    conn,
                    action["target"],
                    AUTHOR,
                    (
                        f"NEEDS_WORK feedback applied by {ACTOR}.\n"
                        f"Marker: {marker}\n"
                        f"Verdict origin: `{action['verdict_origin']}` ({action['verdict_source']}"
                        f"{', legacy prose' if action['legacy_source'] else ''})\n"
                        f"Card returned to the queue — cure the findings on THIS card; "
                        f"do not mint a new one.\n\n"
                        f"Findings excerpt:\n{action.get('feedback') or '(see origin card)'}"
                    ),
                )
            ok = kb.unblock_task(conn, action["target"])
            executed.append({**action, "executed": bool(ok)})

        elif kind == "wake-overdue":
            ok, refusal = kb.promote_task(
                conn, action["target"], actor=ACTOR,
                reason="reconciler wake-overdue: all parent dependencies terminal",
            )
            executed.append({**action, "executed": bool(ok), **({"refusal": refusal} if refusal else {})})

        elif kind == "mint":
            if breaker_tripped:
                # No fingerprint INSERT on a breaker abort: a later healthy
                # pass must still be allowed the one legitimate attempt.
                executed.append({**action, "executed": False, "reason": "CIRCUIT-BREAKER"})
                continue
            _ensure_defect_fingerprints(conn)
            try:
                with kb.write_txn(conn):
                    conn.execute(
                        "INSERT INTO defect_fingerprints(lineage_root, fingerprint, card_id, created_at) "
                        "VALUES (?, ?, NULL, ?)",
                        (action["lineage_root"], action["defect_fingerprint"], now),
                    )
            except sqlite3.IntegrityError:
                executed.append({
                    **action,
                    "type": "suppressed-duplicate",
                    "executed": False,
                    "reason": "UNIQUE(lineage_root, fingerprint) violation — repair already attempted",
                })
                continue
            target_row = _task(conn, action["target"])
            try:
                card_id = kb.create_task(
                    conn,
                    title=action["title"],
                    body=_mint_body(action, target_row),
                    assignee=action["assignee"],
                    created_by=AUTHOR,
                    priority=90,
                    idempotency_key=action["idempotency_key"],
                )
            except Exception as exc:
                # Roll the fingerprint back so a fixed environment can retry;
                # a fingerprint without a card would suppress repair forever.
                with kb.write_txn(conn):
                    conn.execute(
                        "DELETE FROM defect_fingerprints WHERE lineage_root = ? AND fingerprint = ? "
                        "AND card_id IS NULL",
                        (action["lineage_root"], action["defect_fingerprint"]),
                    )
                _log.warning("mint failed for %s: %s", action["target"], exc)
                executed.append({**action, "executed": False, "error": str(exc)})
                continue
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE defect_fingerprints SET card_id = ? WHERE lineage_root = ? AND fingerprint = ?",
                    (card_id, action["lineage_root"], action["defect_fingerprint"]),
                )
            executed.append({**action, "executed": True, "card_id": card_id})

        else:
            # stall-report / suppressed-duplicate / requires_orchestrator are
            # report-only by design — they surface, they never mutate.
            executed.append({**action, "executed": False, "report_only": True})

    promoted = kb.recompute_ready(conn)
    return {"actions": executed, "alerts": alerts, "recompute_ready_promoted": promoted}


# ---------------------------------------------------------------------------
# Shadow compare (soak instrumentation)
# ---------------------------------------------------------------------------

def load_legacy_sweep():
    """Load the paused reactive sweep by file path (kept untouched for
    rollback). Returns the module or ``None`` when unavailable."""
    path = REPO_ROOT / "scripts" / "mission_engine_reactive_sweep.py"
    try:
        spec = importlib.util.spec_from_file_location("mission_engine_reactive_sweep_shadow", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # dataclass creation resolves the defining module through sys.modules,
        # so register before exec (otherwise: 'NoneType' has no '__dict__').
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # pragma: no cover - environment-dependent
        _log.warning("old sweep unavailable for shadow-compare: %s", exc)
        return None


def shadow_compare(conn: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, Any]:
    """Run the old sweep's detection in-process and diff intents per lineage.

    Labels are provenance, not adjudication: ``new_right`` = intents only the
    reconciler surfaced, ``old_right`` = intents only the old sweep surfaced,
    ``both`` = agreement. Humans adjudicate during the soak. Old ``cure``
    mints are mapped to the reconciler's ``apply-NEEDS_WORK`` recycle (same
    defect, new repair strategy); old ``regate`` mints have no reconciler
    equivalent by design (recycled targets re-enter review via re-block).
    """
    legacy = load_legacy_sweep()
    if legacy is None:
        return {"available": False, "reason": "old sweep not importable"}

    old_intents: set[tuple[str, str]] = set()
    old_detail: list[dict[str, Any]] = []
    for a in legacy.plan_actions(conn):
        raw_ref = a.dedupe_key.rsplit(":", 1)[-1]
        root = lineage_root(conn, raw_ref)
        intent = {"cure": "repair", "regate": "review-gate", "review_gate": "review-gate"}.get(a.kind, a.kind)
        old_intents.add((intent, root))
        old_detail.append({"kind": a.kind, "dedupe_key": a.dedupe_key, "lineage_root": root})
    for p in legacy.plan_approve_propagations(conn):
        root = lineage_root(conn, p.target_id)
        old_intents.add(("approve", root))
        old_detail.append({"kind": "approve_propagation", "target": p.target_id, "lineage_root": root})

    new_intents: set[tuple[str, str]] = set()
    for a in plan["actions"]:
        if a["type"] == "apply-APPROVE":
            new_intents.add(("approve", a["lineage_root"]))
        elif a["type"] == "apply-NEEDS_WORK":
            new_intents.add(("repair", a["lineage_root"]))
        elif a["type"] in ("mint", "suppressed-duplicate") and a.get("defect_class") == "review-gate":
            new_intents.add(("review-gate", a["lineage_root"]))

    def _fmt(intents: set[tuple[str, str]]) -> list[dict[str, str]]:
        return [{"intent": i, "lineage_root": r} for i, r in sorted(intents)]

    return {
        "available": True,
        "both": _fmt(new_intents & old_intents),
        "new_right": _fmt(new_intents - old_intents),
        "old_right": _fmt(old_intents - new_intents),
        "old_sweep_raw": old_detail,
        "note": (
            "labels are provenance (which engine surfaced the intent); "
            "adjudication is human during the soak"
        ),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _emit_beat(status: str, detail: Optional[str]) -> None:
    """component='reconciler', interval 1800 — lazy import, tolerate absence."""
    try:
        from hermes_cli import engine_health
    except Exception:
        return
    try:
        engine_health.beat(COMPONENT, BEAT_INTERVAL_SECONDS, status=status, detail=detail)
    except Exception as exc:  # pragma: no cover - beat() contract is never-raise anyway
        _log.warning("reconciler heartbeat failed: %s", exc)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mission Engine reconciler (rework P4): level-triggered desired-vs-actual plan/apply",
    )
    parser.add_argument("--board", default=DEFAULT_BOARD, help=f"Board slug (default: {DEFAULT_BOARD})")
    parser.add_argument("--db", default=None, help="Explicit kanban.db path (tests/manual runs; overrides --board)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--plan", action="store_true", help="Print the plan JSON, mutate NOTHING (default)")
    mode_group.add_argument("--apply", action="store_true", help="Execute plan actions")
    mode_group.add_argument(
        "--shadow-compare", action="store_true",
        help="Plan + run the old sweep's detection in-process; print divergences (soak)",
    )
    args = parser.parse_args(argv)
    mode = "apply" if args.apply else ("shadow-compare" if args.shadow_compare else "plan")

    if os.environ.get(DISABLE_ENV, "").strip().lower() in _TRUTHY:
        print(json.dumps({
            "actions": [],
            "health": {"disabled": True, "reason": f"{DISABLE_ENV} set", "boards_scanned": 0},
            "mode": mode,
        }, indent=2, sort_keys=True))
        return 0

    if args.db:
        conn = kb.connect(db_path=Path(args.db))
        board_label = args.db
    else:
        conn = kb.connect(board=args.board)
        board_label = args.board

    now = int(time.time())
    plan = build_plan(conn, now=now, board=board_label)
    out: dict[str, Any] = {
        "mode": mode,
        "board": board_label,
        "generated_at": now,
        "actions": plan["actions"],
        "health": plan["health"],
        "alerts": [],
    }
    if mode == "apply":
        applied = apply_plan(conn, plan, now=now)
        out["actions"] = applied["actions"]
        out["alerts"] = applied["alerts"]
        out["recompute_ready_promoted"] = applied["recompute_ready_promoted"]
    elif mode == "shadow-compare":
        out["shadow"] = shadow_compare(conn, plan)

    # NO --quiet-if-empty, ever: an empty plan still prints actions + health.
    print(json.dumps(out, indent=2, sort_keys=True))

    breaker_state = plan["health"]["breaker"]["state"]
    status = "ok" if breaker_state != "tripped" else "ok (breaker tripped)"
    _emit_beat(status, detail=f"mode={mode} actions={len(out['actions'])} board={board_label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
