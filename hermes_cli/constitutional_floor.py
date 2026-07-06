"""Spec 002 Mission Engine — the immutable constitutional floor.

Part of the Mission Engine rework GOVERNANCE-AS-DATA phase (P6, Jared-approved
program 2026-07-05).  The floor is the 8-item set of rules that hold at EVERY
autonomy level — the autonomy dial (:mod:`hermes_cli.autonomy_dial`) only
changes what runs dark *below* this floor; it never moves the floor itself.

Integrity model — THE FLOOR IS THE FLOOR
----------------------------------------
* The data file lives at ``~/.hermes/specs/002-mission-engine/floor.yaml``
  (synced/versioned by hermes-runtime).
* Its exact bytes are sha256-pinned here as :data:`FLOOR_SHA256`.  A hash
  mismatch, a missing file, or a structurally invalid document raises
  :class:`MissionFloorError` (a ``ValueError`` subclass) — **fail closed**.
* **There is deliberately NO env kill-switch and no warn/shadow mode for the
  hash pin.**  The rework hard rules require a kill-switch for fail-closed
  behavior *where it could break live flow*; the floor is the one governance
  artifact that is allowed to break flow, because a tampered or missing
  constitution must halt floor-gated actions, not degrade them.  The only
  env knob is :data:`FLOOR_PATH_ENV` (test/deployment path override) — the
  content at any path must still hash to the pin, so the override cannot
  weaken the floor.
* Amendment path: Jared-approved Spec-002 policy amendment editing
  ``floor.yaml`` AND re-pinning :data:`FLOOR_SHA256` in the same reviewed
  commit.  :data:`CANONICAL_FLOOR_TEXT` (the authored bytes) is embedded so
  tests stay hermetic and bootstrap/sync can rewrite a lost runtime copy.

API
---
``check(action, context)`` returns ``None`` when the floor permits the action
(or does not govern it) and raises :class:`MissionFloorError` carrying the
violated floor item id otherwise.  Governed actions: ``side_effect`` /
``complete_task``, ``waive`` / ``verdict``, ``git_push``, ``dial_up`` /
``autonomy_increment``, ``delete_history`` / ``delete``, ``review_fallback``,
``external_send``.  Unknown actions return ``None`` — the floor governs its
enumerated enforcement points; callers must use the documented action names.

Loader pattern follows :mod:`hermes_cli.mission_guardrail_policy` (ValueError
subclass, parsed-document cache), except the cache is keyed by content hash:
the pin is re-verified on every load so a same-size/same-mtime rewrite can
never dodge it.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pin + discovery
# ---------------------------------------------------------------------------

#: sha256 of the exact bytes of the authored floor.yaml.  Re-pin ONLY inside a
#: Jared-approved Spec-002 policy amendment commit.
FLOOR_SHA256 = "ea32d6420aebb1a330ad2db703236bc34c281318738e8b0f6b89d01113e4bef3"

#: Path override for tests / unusual deployments.  Content must still hash to
#: :data:`FLOOR_SHA256` — this knob cannot weaken the floor.  There is NO
#: kill-switch env var for the hash check itself (see module docstring).
FLOOR_PATH_ENV = "HERMES_CONSTITUTIONAL_FLOOR_PATH"

KANBAN_HOME_ENV = "HERMES_KANBAN_HOME"
FLOOR_RELPATH = ("specs", "002-mission-engine", "floor.yaml")

FLOOR_KIND = "mission_constitutional_floor"
FLOOR_IDS = ("F001", "F002", "F003", "F004", "F005", "F006", "F007", "F008")

#: The authored floor.yaml bytes (kept byte-identical to the runtime file; the
#: test suite asserts sha256(CANONICAL_FLOOR_TEXT) == FLOOR_SHA256).
CANONICAL_FLOOR_TEXT = """\
# Spec 002 Mission Engine — Constitutional Floor (GOVERNANCE-AS-DATA, P6)
#
# THE FLOOR IS THE FLOOR. Every byte of this file is sha256-pinned in
# hermes_cli/constitutional_floor.py (FLOOR_SHA256). Any edit — including
# whitespace — makes the loader raise MissionFloorError and every declared
# enforcement point fails CLOSED. There is deliberately NO env kill-switch.
# Amendment path: Jared-approved Spec-002 policy amendment, then re-pin
# FLOOR_SHA256 in the same reviewed commit.
#
# Distilled 2026-07-05 from: auto-decision-policy.md (Jared-gated list),
# autonomy.yaml constitutional_floor, evidence-contract.md, packet-templates.md,
# mission-guardrail-policy.yaml side_effect_policy, decision-ledger.md D2-018.
kind: mission_constitutional_floor
version: 1
canonical_spec: /home/red/.hermes/specs/002-mission-engine/spec.md
items:
  - id: F001
    rule: >-
      side_effect_class 'send' or 'spend' requires Jared-personal authority:
      a Jared packet id recorded on the card before the side effect executes.
      Never auto-approved at any autonomy level.
    enforcement_point: complete_task side_effect gate -> constitutional_floor.check('side_effect')
    immutable: true
  - id: F002
    rule: >-
      side_effect_class 'deploy' or 'merge' requires a cross-model APPROVE
      verdict on raw evidence PLUS a default-on-silence Jared packet
      (packet id recorded). Merge/promote/deploy and anything touching
      production or main/develop stays Jared-gated at every autonomy level.
    enforcement_point: complete_task side_effect gate -> constitutional_floor.check('side_effect')
    immutable: true
  - id: F003
    rule: >-
      A WAIVED verdict requires waive_authority = a Jared packet id.
      No agent, at any autonomy level, may waive a gate on its own authority.
    enforcement_point: task_verdicts WAIVE path -> constitutional_floor.check('waive')
    immutable: true
  - id: F004
    rule: >-
      No git push to the NousResearch origin remote, ever. Pushes go to the
      fork only (governance.yaml fork_pushes_only).
    enforcement_point: pre-push gate -> constitutional_floor.check('git_push')
    immutable: true
  - id: F005
    rule: >-
      The autonomy dial only ratchets UP via an explicit Jared packet answered
      YES. request_increment never changes the level; silence defaults NO.
      Down-moves are automatic and packet-free.
    enforcement_point: autonomy_dial writer path -> constitutional_floor.check('dial_up')
    immutable: true
  - id: F006
    rule: >-
      Durable history (boards, vault notes, memory DBs, mission artifacts)
      is never deleted without an evidence archive reference recorded first.
      Archive is auto-decidable; deletion is not.
    enforcement_point: destructive-op gate -> constitutional_floor.check('delete_history')
    immutable: true
  - id: F007
    rule: >-
      No silent same-model review fallback on T2+ missions. The gate must be
      cross-model; degrading to a same-model review requires an explicit,
      logged fallback declaration and counts as a cross_model_degradation
      down-trigger signal.
    enforcement_point: review dispatch -> constitutional_floor.check('review_fallback')
    immutable: true
  - id: F008
    rule: >-
      External sends (Discord/Telegram/email/any human-facing channel) count
      as delivered only with a recorded delivery receipt (message id or
      provider receipt). No receipt, no 'sent'.
    enforcement_point: external send completion -> constitutional_floor.check('external_send')
    immutable: true
"""


class MissionFloorError(ValueError):
    """The constitutional floor is missing, tampered with, or violated.

    ``ValueError`` subclass so existing CLI/DB call sites surface it as an
    rc=2 actionable error.  ``floor_id`` carries the violated item id (or
    ``None`` for integrity failures, which indict the whole floor).
    """

    def __init__(self, message: str, floor_id: Optional[str] = None):
        super().__init__(message)
        self.floor_id = floor_id


@dataclass(frozen=True)
class FloorItem:
    id: str
    rule: str
    enforcement_point: str
    immutable: bool


@dataclass(frozen=True)
class FloorState:
    items: tuple
    sha256: str
    source_path: Path

    def item(self, floor_id: str) -> FloorItem:
        for it in self.items:
            if it.id == floor_id:
                return it
        raise KeyError(floor_id)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def floor_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    override = (env.get(FLOOR_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    root_override = (env.get(KANBAN_HOME_ENV) or "").strip()
    if root_override:
        return Path(root_override).expanduser().joinpath(*FLOOR_RELPATH)
    try:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root()
    except Exception:  # pragma: no cover - constants module always present in repo
        root = Path.home() / ".hermes"
    return Path(root).joinpath(*FLOOR_RELPATH)


# Parsed-document cache keyed by CONTENT HASH (not mtime): the sha256 pin is
# re-verified against the raw bytes on every load, so a rewrite that preserves
# size+mtime still cannot dodge the check.
_FLOOR_CACHE: dict = {}


def clear_cache() -> None:
    """Drop the parsed-floor cache (primarily for tests)."""
    _FLOOR_CACHE.clear()


def _parse_items(path: Path, data: Any) -> tuple:
    if not isinstance(data, dict):
        raise MissionFloorError(
            f"constitutional floor at {path}: top-level document must be a mapping"
        )
    kind = str(data.get("kind", "")).strip()
    if kind != FLOOR_KIND:
        raise MissionFloorError(
            f"constitutional floor at {path}: kind is {kind!r}, expected {FLOOR_KIND!r}"
        )
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise MissionFloorError(f"constitutional floor at {path}: items must be a non-empty list")
    items = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise MissionFloorError(f"constitutional floor at {path}: item is not a mapping")
        fid = str(entry.get("id", "")).strip()
        rule = str(entry.get("rule", "")).strip()
        point = str(entry.get("enforcement_point", "")).strip()
        immutable = entry.get("immutable")
        if not fid or not rule or not point:
            raise MissionFloorError(
                f"constitutional floor at {path}: item {fid or '<missing id>'} needs "
                f"id, rule and enforcement_point"
            )
        if immutable is not True:
            raise MissionFloorError(
                f"constitutional floor at {path}: item {fid} must declare immutable: true"
            )
        items.append(FloorItem(id=fid, rule=rule, enforcement_point=point, immutable=True))
    ids = tuple(it.id for it in items)
    if ids != FLOOR_IDS:
        raise MissionFloorError(
            f"constitutional floor at {path}: item ids {ids} != expected {FLOOR_IDS}"
        )
    return tuple(items)


def load_floor(
    path: Optional[Path | str] = None, env: Optional[Mapping[str, str]] = None
) -> FloorState:
    """Load + integrity-verify the floor. Fail closed on ANY problem.

    Raises :class:`MissionFloorError` when the file is missing, its bytes do
    not hash to :data:`FLOOR_SHA256`, or the document is structurally invalid.
    No kill-switch, no shadow mode — see module docstring.
    """
    resolved = Path(path).expanduser() if path is not None else floor_path(env)
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise MissionFloorError(
            f"constitutional floor missing/unreadable at {resolved}: {exc} — "
            f"floor-gated actions fail closed"
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != FLOOR_SHA256:
        raise MissionFloorError(
            f"CONSTITUTIONAL FLOOR HASH MISMATCH at {resolved}: sha256={digest} "
            f"!= pinned {FLOOR_SHA256} — file was modified outside a "
            f"Jared-approved amendment; floor-gated actions fail closed"
        )
    cached = _FLOOR_CACHE.get(digest)
    if cached is not None and cached.source_path == resolved:
        return cached
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:  # pragma: no cover - pinned bytes parse
        raise MissionFloorError(
            f"constitutional floor at {resolved} matched the pin but failed to parse: {exc}"
        ) from exc
    state = FloorState(items=_parse_items(resolved, data), sha256=digest, source_path=resolved)
    _FLOOR_CACHE[digest] = state
    return state


# ---------------------------------------------------------------------------
# check(action, context)
# ---------------------------------------------------------------------------

def _packet_id(context: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        val = context.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _tier_rank(raw: Any) -> int:
    """Normalize 'T2'/'t2'/2/'2' -> 2; unknown/missing -> -1."""
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw or "").strip().lower()
    if text.startswith("t"):
        text = text[1:]
    try:
        return int(text)
    except ValueError:
        return -1


def _violation(state: FloorState, floor_id: str, detail: str) -> MissionFloorError:
    item = state.item(floor_id)
    _log.error("CONSTITUTIONAL FLOOR VIOLATION %s: %s — rule: %s", floor_id, detail, item.rule)
    return MissionFloorError(f"constitutional floor {floor_id}: {detail} — {item.rule}", floor_id)


def check(
    action: str,
    context: Mapping[str, Any],
    *,
    path: Optional[Path | str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """Return ``None`` if the floor permits ``action``; raise otherwise.

    Raises :class:`MissionFloorError` (with ``floor_id`` set) on a violation,
    and with ``floor_id=None`` when the floor itself fails integrity checks
    (missing / hash mismatch) — both fail closed at the call site.
    """
    state = load_floor(path=path, env=env)
    act = (action or "").strip().lower()
    context = context if isinstance(context, Mapping) else {}

    if act in ("side_effect", "complete_task"):
        cls = str(context.get("side_effect_class") or "").strip().lower()
        if not cls:
            return None
        if cls in ("send", "spend"):
            if not _packet_id(context, "jared_packet_id"):
                raise _violation(
                    state, "F001", f"side_effect_class={cls!r} without a Jared packet id"
                )
            return None
        if cls in ("deploy", "merge"):
            if not context.get("cross_model_approve"):
                raise _violation(
                    state, "F002", f"side_effect_class={cls!r} without a cross-model APPROVE"
                )
            if not _packet_id(context, "jared_packet_id"):
                raise _violation(
                    state,
                    "F002",
                    f"side_effect_class={cls!r} without a default-on-silence Jared packet id",
                )
            return None
        # Unknown side-effect class: the floor cannot classify it, so it gets
        # the strictest treatment (fail closed under F002).
        raise _violation(state, "F002", f"unrecognized side_effect_class={cls!r} (fail-closed)")

    if act in ("waive", "verdict"):
        verdict = str(context.get("verdict") or "WAIVED").strip().upper()
        if act == "verdict" and verdict != "WAIVED":
            return None
        if not _packet_id(context, "waive_authority", "jared_packet_id"):
            raise _violation(state, "F003", "WAIVED verdict without a Jared packet id")
        return None

    if act == "git_push":
        target = " ".join(
            str(context.get(k) or "") for k in ("remote_url", "remote", "push_target")
        ).lower()
        if "nousresearch" in target:
            raise _violation(state, "F004", f"push targets NousResearch origin ({target.strip()})")
        return None

    if act in ("dial_up", "autonomy_increment"):
        if not _packet_id(context, "jared_packet_id"):
            raise _violation(state, "F005", "autonomy up-ratchet without a Jared packet id")
        return None

    if act in ("delete_history", "delete"):
        archived = _packet_id(context, "archive_ref") or context.get("evidence_archived")
        if not archived:
            raise _violation(state, "F006", "durable-history deletion without an archive reference")
        return None

    if act == "review_fallback":
        tier = _tier_rank(context.get("tier"))
        if tier >= 2 and not context.get("cross_model") and not context.get("fallback_declared"):
            raise _violation(
                state, "F007", f"silent same-model review fallback on tier T{tier}"
            )
        return None

    if act == "external_send":
        if not _packet_id(context, "delivery_receipt"):
            raise _violation(state, "F008", "external send without a delivery receipt")
        return None

    _log.debug("constitutional_floor.check: action %r not floor-governed", action)
    return None
