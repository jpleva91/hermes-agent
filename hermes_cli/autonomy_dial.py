"""Spec 002 Autonomy Dial — validated loader + asymmetric dial operations.

Part of the Mission Engine rework GOVERNANCE-AS-DATA phase (P6, Jared-approved
program 2026-07-05).  The dial file
``~/.hermes/specs/002-mission-engine/autonomy.yaml`` has existed since D2-018
with **zero production readers**; this module is the reader.

Contract
--------
* :func:`load_dial` — validated view of the dial: current level, per-level
  dark-run permissions, down-trigger rule names, ratchet counters.
* **Fail-closed, never fail-broken:** a missing / corrupt / invalid dial file
  degrades to the hardcoded MOST-RESTRICTIVE defaults (L0 OBSERVED, nothing
  runs dark, every down-trigger armed) with a loud ``AUTONOMY DIAL
  FAIL-CLOSED`` log line for the liveness sentinel to surface.  The loader
  **never raises** on bad dial data, so it cannot break live flow — which is
  why (per the rework hard rules) no env kill-switch is needed for this
  fail-closed path: degrading to L0 only *adds* Jared packets, it never blocks
  execution.
* **Asymmetric ratchet (auto-decision-policy.md "Autonomy Dial" section):**

  - :func:`decrement` — automatic, no packet needed.  Appends a
    ``dial_events`` audit row on the engine-health board DB and rewrites
    ``autonomy.yaml`` one level down.
  - :func:`request_increment` — the up-ratchet is **human-only, default NO**.
    This function NEVER changes the level; it only returns a binary
    Jared-packet stub (packet-templates.md ratchet-up grammar) for the
    commander to deliver.  Raising the level is done by Jared answering YES
    and a human-authorized writer applying it — never by this module.

* :func:`evaluate_down_triggers` — pure evaluation of externally-verified
  counters (circuit breaker, verdict fail-closed hits, watchdog criticals,
  cross-model degradations) into triggered down-rule names.

Env overrides (tests / unusual deployments):

* ``HERMES_AUTONOMY_DIAL_PATH`` — dial file path override.
* ``HERMES_ENGINE_HEALTH_DB`` — dial_events DB path override.
* ``HERMES_KANBAN_HOME`` — shared root override (same semantics as
  :func:`hermes_cli.kanban_db.kanban_home`, resolved independently here to
  avoid importing the orchestrator-owned module).

Loader pattern follows :mod:`hermes_cli.mission_guardrail_policy`
(mtime+size cache, ``ValueError`` subclass for programmer/IO errors).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths / env knobs
# ---------------------------------------------------------------------------

DIAL_PATH_ENV = "HERMES_AUTONOMY_DIAL_PATH"
ENGINE_HEALTH_DB_ENV = "HERMES_ENGINE_HEALTH_DB"
KANBAN_HOME_ENV = "HERMES_KANBAN_HOME"

DIAL_RELPATH = ("specs", "002-mission-engine", "autonomy.yaml")
ENGINE_HEALTH_DB_RELPATH = ("kanban", "boards", "engine-health", "kanban.db")

# ---------------------------------------------------------------------------
# Levels (autonomy-dial-draft.md / auto-decision-policy.md "Autonomy Dial")
# ---------------------------------------------------------------------------

MIN_LEVEL = 0
MAX_LEVEL = 3

LEVEL_NAMES: dict[int, str] = {
    0: "OBSERVED",
    1: "SUPERVISED",
    2: "TRUSTED",
    3: "DARK_FACTORY",
}

#: What may run dark (without a Jared packet) at each level, *below the
#: constitutional floor* (see :mod:`hermes_cli.constitutional_floor` — the
#: floor never moves, at any level).
LEVEL_PERMISSIONS: dict[int, frozenset] = {
    0: frozenset(),
    1: frozenset({"build", "evidence", "gate_review", "pr_creation"}),
    2: frozenset(
        {
            "build",
            "evidence",
            "gate_review",
            "pr_creation",
            "mission_closeout",
            "ship_list_batching",
        }
    ),
    3: frozenset(
        {
            "build",
            "evidence",
            "gate_review",
            "pr_creation",
            "mission_closeout",
            "ship_list_batching",
            "below_floor_all",
        }
    ),
}

# ---------------------------------------------------------------------------
# Down triggers — externally-verified counters -> down-rule names
# ---------------------------------------------------------------------------

#: rule name -> (signal key, threshold at/above which the rule fires).
#: Signals must be *externally verified* counters (never lane self-report):
#: the circuit breaker page, verdict-guard fail-closed hits, watchdog
#: criticals, and cross-model degradation events.
DOWN_TRIGGER_RULES: dict[str, tuple[str, int]] = {
    "circuit_breaker_fired": ("circuit_breaker_fired", 1),
    "verdict_failclosed": ("verdict_failclosed_hits", 1),
    "watchdog_critical": ("watchdog_criticals", 1),
    "cross_model_degradation": ("cross_model_degradations", 1),
}

#: Spec-declared down triggers (autonomy.yaml ``ratchet.down_triggers``);
#: armed unconditionally in the fail-closed defaults.
DEFAULT_DOWN_TRIGGERS: tuple = (
    "reversed_auto_decision",
    "false_evidence_catch",
    "watchdog_critical",
    "failed_acceptance_rerun",
)


class MissionAutonomyDialError(ValueError):
    """Programmer/IO error in a dial *operation* (not the loader).

    ``ValueError`` subclass so CLI shims surface it as an rc=2 actionable
    error, matching :class:`hermes_cli.mission_guardrail_policy
    .MissionGuardrailPolicyError`.  Note the loader itself never raises this
    for bad dial data — it fail-closes to L0 defaults instead.
    """


@dataclass(frozen=True)
class DialState:
    """Immutable, validated view of the autonomy dial."""

    level: int
    level_name: str
    permissions: frozenset
    down_triggers: tuple
    clean_missions: int
    clean_missions_required: int
    up_default: str
    l3_locked: bool
    fail_closed: bool
    source_path: Optional[Path]
    raw: Optional[Mapping[str, Any]] = None

    def allows(self, capability: str) -> bool:
        """True when ``capability`` may run dark at the current level."""
        return capability in self.permissions or "below_floor_all" in self.permissions


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _hermes_root(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    override = (env.get(KANBAN_HOME_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    try:
        from hermes_constants import get_default_hermes_root

        return get_default_hermes_root()
    except Exception:  # pragma: no cover - constants module always present in repo
        return Path.home() / ".hermes"


def dial_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve the dial file path (env override, then shared-root default)."""
    env = os.environ if env is None else env
    override = (env.get(DIAL_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return _hermes_root(env).joinpath(*DIAL_RELPATH)


def engine_health_db_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve the engine-health board DB holding ``dial_events``."""
    env = os.environ if env is None else env
    override = (env.get(ENGINE_HEALTH_DB_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return _hermes_root(env).joinpath(*ENGINE_HEALTH_DB_RELPATH)


# ---------------------------------------------------------------------------
# Loader (fail-closed to most-restrictive defaults)
# ---------------------------------------------------------------------------

_DIAL_CACHE: dict = {}


def clear_cache() -> None:
    """Drop the parsed-dial cache (primarily for tests)."""
    _DIAL_CACHE.clear()


def _fail_closed(reason: str, path: Optional[Path]) -> DialState:
    _log.error(
        "AUTONOMY DIAL FAIL-CLOSED: %s (path=%s) — defaulting to L0 OBSERVED, "
        "nothing runs dark, all down-triggers armed; liveness sentinel should "
        "surface this line",
        reason,
        path,
    )
    return DialState(
        level=MIN_LEVEL,
        level_name=LEVEL_NAMES[MIN_LEVEL],
        permissions=LEVEL_PERMISSIONS[MIN_LEVEL],
        down_triggers=DEFAULT_DOWN_TRIGGERS,
        clean_missions=0,
        clean_missions_required=5,
        up_default="NO",
        l3_locked=True,
        fail_closed=True,
        source_path=path,
        raw=None,
    )


def _coerce_level(value: Any) -> Optional[int]:
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    if MIN_LEVEL <= level <= MAX_LEVEL:
        return level
    return None


def _parse(path: Path, data: Any) -> DialState:
    if not isinstance(data, dict):
        return _fail_closed(
            f"top-level document must be a mapping, got {type(data).__name__}", path
        )
    level = _coerce_level(data.get("level"))
    if level is None:
        return _fail_closed(f"level is {data.get('level')!r}, expected int 0..3", path)

    ratchet = data.get("ratchet")
    ratchet = ratchet if isinstance(ratchet, dict) else {}

    triggers_raw = ratchet.get("down_triggers")
    if isinstance(triggers_raw, (list, tuple)) and triggers_raw:
        down_triggers = tuple(str(t).strip() for t in triggers_raw if str(t).strip())
    else:
        down_triggers = DEFAULT_DOWN_TRIGGERS
    if not down_triggers:
        down_triggers = DEFAULT_DOWN_TRIGGERS

    try:
        clean = max(0, int(ratchet.get("clean_missions_at_current_level", 0)))
    except (TypeError, ValueError):
        clean = 0
    try:
        required = max(1, int(ratchet.get("clean_missions_required_to_offer_up", 5)))
    except (TypeError, ValueError):
        required = 5

    up_raw = ratchet.get("up_default", "NO")
    if isinstance(up_raw, bool):
        # YAML 1.1: an unquoted `up_default: NO` parses as boolean False.
        up_default = "YES" if up_raw else "NO"
    else:
        up_default = str(up_raw).strip().upper() or "NO"
    if up_default != "NO":
        # The up-ratchet default is NO by policy (D2-018); a dial file claiming
        # otherwise is treated as most-restrictive, not trusted.
        _log.warning(
            "autonomy dial %s declares up_default=%r; policy pins the "
            "ratchet-up default to NO — ignoring the file's value",
            path,
            up_default,
        )
        up_default = "NO"

    l3 = data.get("l3")
    l3_locked = bool(l3.get("locked", True)) if isinstance(l3, dict) else True

    return DialState(
        level=level,
        level_name=LEVEL_NAMES[level],
        permissions=LEVEL_PERMISSIONS[level],
        down_triggers=down_triggers,
        clean_missions=clean,
        clean_missions_required=required,
        up_default=up_default,
        l3_locked=l3_locked,
        fail_closed=False,
        source_path=path,
        raw=data,
    )


def load_dial(
    path: Optional[Path | str] = None, env: Optional[Mapping[str, str]] = None
) -> DialState:
    """Load the dial state; degrade to L0 defaults on any problem.

    Never raises for bad dial data — see the module docstring for why this
    fail-closed path deliberately has no env kill-switch.
    """
    resolved = Path(path).expanduser() if path is not None else dial_path(env)
    try:
        st = resolved.stat()
    except OSError as exc:
        return _fail_closed(f"dial file not readable: {exc}", resolved)
    key = (str(resolved), st.st_mtime_ns, st.st_size)
    cached = _DIAL_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return _fail_closed(f"dial file unreadable/corrupt YAML: {exc}", resolved)
    state = _parse(resolved, data)
    if not state.fail_closed:
        _DIAL_CACHE[key] = state
    return state


# ---------------------------------------------------------------------------
# Down-trigger evaluation
# ---------------------------------------------------------------------------

def evaluate_down_triggers(signals: Mapping[str, Any]) -> list:
    """Return the down-rule names triggered by externally-verified counters.

    ``signals`` carries counters gathered *outside* the executing lane (the
    circuit breaker, the verdict guard, the watchdog, the model router):
    ``circuit_breaker_fired``, ``verdict_failclosed_hits``,
    ``watchdog_criticals``, ``cross_model_degradations``.  Unknown keys are
    ignored; non-numeric values are treated as 0 with a warning (a malformed
    signal must not silently *fire* a demotion, and the sources are trusted
    external counters, not agent self-report).
    """
    triggered: list = []
    if not isinstance(signals, Mapping):
        _log.warning("evaluate_down_triggers: signals is %r, expected mapping", signals)
        return triggered
    for rule, (signal_key, threshold) in DOWN_TRIGGER_RULES.items():
        raw = signals.get(signal_key, 0)
        try:
            count = int(raw)
        except (TypeError, ValueError):
            _log.warning(
                "evaluate_down_triggers: signal %s=%r is not a counter; treating as 0",
                signal_key,
                raw,
            )
            count = 0
        if count >= threshold:
            triggered.append(rule)
    return triggered


# ---------------------------------------------------------------------------
# dial_events audit table (engine-health board DB)
# ---------------------------------------------------------------------------

_DIAL_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS dial_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('down','up')),
  from_level INTEGER NOT NULL,
  to_level INTEGER NOT NULL,
  reason TEXT,
  evidence_ref TEXT,
  authority TEXT
)
"""


def _append_dial_event(
    db_path: Path,
    *,
    direction: str,
    from_level: int,
    to_level: int,
    reason: str,
    evidence_ref: str,
    authority: str,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_DIAL_EVENTS_DDL)
        conn.execute(
            "INSERT INTO dial_events"
            " (ts, direction, from_level, to_level, reason, evidence_ref, authority)"
            " VALUES (?,?,?,?,?,?,?)",
            (int(time.time()), direction, from_level, to_level, reason, evidence_ref, authority),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Asymmetric dial ops
# ---------------------------------------------------------------------------

_GENERATED_HEADER = (
    "# Spec 002 Autonomy Dial state\n"
    "# Constitutional floor entries are immutable without a Jared-approved\n"
    "# Spec-002 policy amendment. Down-moves are automatic (hermes_cli/\n"
    "# autonomy_dial.decrement); up-moves are human-only via Jared packet.\n"
)


def _atomic_write_yaml(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = yaml.safe_dump(dict(doc), sort_keys=False, allow_unicode=True)
    tmp.write_text(_GENERATED_HEADER + body, encoding="utf-8")
    os.replace(tmp, path)


def decrement(
    reason: str,
    evidence_ref: str,
    *,
    authority: str = "engine:auto-down-trigger",
    path: Optional[Path | str] = None,
    db_path: Optional[Path | str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> DialState:
    """Drop the dial one level (automatic, per the D2-018 down-ratchet).

    Order of operations is audit-first: the ``dial_events`` row is written to
    the engine-health board DB *before* ``autonomy.yaml`` is rewritten, so a
    crash between the two leaves an audit trail pointing at a dial file that
    is one level too high — the safe direction to reconcile from.

    Returns the new :class:`DialState`.  Raises
    :class:`MissionAutonomyDialError` on empty ``reason``/``evidence_ref``
    (a demotion without a recorded cause is itself a governance violation)
    or when the rewrite fails.
    """
    reason = (reason or "").strip()
    evidence_ref = (evidence_ref or "").strip()
    if not reason or not evidence_ref:
        raise MissionAutonomyDialError(
            "autonomy dial decrement requires a non-empty reason and evidence_ref"
        )

    resolved = Path(path).expanduser() if path is not None else dial_path(env)
    db_resolved = Path(db_path).expanduser() if db_path is not None else engine_health_db_path(env)

    state = load_dial(resolved)
    from_level = state.level
    to_level = max(MIN_LEVEL, from_level - 1)

    _append_dial_event(
        db_resolved,
        direction="down",
        from_level=from_level,
        to_level=to_level,
        reason=reason,
        evidence_ref=evidence_ref,
        authority=authority,
    )

    if from_level == to_level:
        _log.warning(
            "autonomy dial already at L%d (%s); decrement recorded but no "
            "level change (reason=%s)",
            from_level,
            LEVEL_NAMES[from_level],
            reason,
        )
        clear_cache()
        return load_dial(resolved)

    doc: dict[str, Any]
    if isinstance(state.raw, Mapping):
        doc = dict(state.raw)
    else:
        # Corrupt/missing file: self-heal with a minimal, most-restrictive doc.
        doc = {"status": "active"}
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    doc["level"] = to_level
    doc["level_name"] = LEVEL_NAMES[to_level]
    doc["last_updated_at"] = now_iso
    ratchet = dict(doc.get("ratchet") or {})
    ratchet["clean_missions_at_current_level"] = 0
    ratchet.setdefault("clean_missions_required_to_offer_up", state.clean_missions_required)
    ratchet["next_offer_level"] = min(MAX_LEVEL, to_level + 1)
    ratchet.setdefault("up_default", "NO")
    ratchet.setdefault("down_triggers", list(state.down_triggers))
    doc["ratchet"] = ratchet
    doc["last_down"] = {
        "at": now_iso,
        "from_level": from_level,
        "to_level": to_level,
        "reason": reason,
        "evidence_ref": evidence_ref,
        "authority": authority,
    }
    try:
        _atomic_write_yaml(resolved, doc)
    except OSError as exc:
        raise MissionAutonomyDialError(
            f"autonomy dial decrement recorded in dial_events but rewrite of "
            f"{resolved} failed: {exc}"
        ) from exc
    _log.warning(
        "AUTONOMY DIAL DOWN: L%d %s -> L%d %s — reason=%s evidence=%s authority=%s",
        from_level,
        LEVEL_NAMES[from_level],
        to_level,
        LEVEL_NAMES[to_level],
        reason,
        evidence_ref,
        authority,
    )
    clear_cache()
    return load_dial(resolved)


def request_increment(
    reason: str = "",
    evidence_ref: str = "",
    *,
    path: Optional[Path | str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Return a binary Jared-packet stub offering +1 level. NEVER moves the dial.

    The up-ratchet is human-only (constitutional floor F005): this function
    performs **no writes** — no YAML rewrite, no dial_events row.  The caller
    delivers the returned packet (packet-templates.md ratchet-up grammar);
    only Jared answering YES authorizes a level increase, applied by a
    human-authorized writer with the packet id as authority.
    """
    resolved = Path(path).expanduser() if path is not None else dial_path(env)
    state = load_dial(resolved)
    from_level = state.level
    to_level = min(MAX_LEVEL, from_level + 1)

    at_max = from_level >= MAX_LEVEL
    l3_blocked = to_level == MAX_LEVEL and state.l3_locked
    earned = state.clean_missions >= state.clean_missions_required
    eligible = earned and not at_max and not l3_blocked and not state.fail_closed

    blocked_by: list = []
    if state.fail_closed:
        blocked_by.append("dial_fail_closed")
    if at_max:
        blocked_by.append("already_at_max_level")
    if l3_blocked:
        blocked_by.append("l3_overseer_go_live_block")
    if not earned:
        blocked_by.append(
            f"ratchet_not_earned:{state.clean_missions}/{state.clean_missions_required}"
        )

    question = (
        f"Ratchet autonomy from L{from_level} to L{to_level}? — "
        f"YES = move to L{to_level} for below-floor work / "
        f"NO = stay at L{from_level} / silence = NO at next digest"
    )
    return {
        "type": "jared_packet_stub",
        "kind": "autonomy_ratchet_up",
        "question": question,
        "from_level": from_level,
        "to_level": to_level,
        "default": "NO",
        "authority_required": "jared_packet",
        "eligible": eligible,
        "blocked_by": blocked_by,
        "clean_missions": state.clean_missions,
        "clean_missions_required": state.clean_missions_required,
        "reason": (reason or "").strip(),
        "evidence_ref": (evidence_ref or "").strip(),
        "level_changed": False,
    }
