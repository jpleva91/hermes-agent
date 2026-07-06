"""Load & evaluate the Spec 002 Mission Engine guardrail policy.

The concrete guardrail data — *which* board is the mission board, the active
role cast, the Mission Commander role, and the mission-shape regex — is NOT
hardcoded in :mod:`hermes_cli.kanban_db`.  It lives in a versioned YAML policy
under ``~/.hermes/specs/002-mission-engine/mission-guardrail-policy.yaml`` (and
is mirrored in the ``hermes-runtime`` repo).  ``kanban_db`` carries only a
thin, board-agnostic hook: if the authoritative board is *declared* a mission
board, or is *named* by an existing policy, enforce that policy; otherwise fail
open so ordinary Kanban boards keep using arbitrary project-specific assignees.

Fail-safe contract (see the policy ``fail_safe`` block):

* Ordinary/undeclared board with no matching policy -> **fail open** (return
  ``None``; no guardrail).
* Board *declared* a mission board (via board metadata ``mission_board: true``
  or an explicit policy-path field) whose policy is missing / corrupt /
  invalid / for a different board -> **fail closed** (raise
  :class:`MissionGuardrailPolicyError`, a ``ValueError`` subclass, before any
  DB mutation).

This module has no dependency on :mod:`hermes_cli.kanban_db`; the caller passes
in the resolved board slug, the board metadata, and the default policy path so
there is no import cycle and no hidden reliance on process-global board state.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

# ---------------------------------------------------------------------------
# Policy identity / discovery knobs
# ---------------------------------------------------------------------------

#: ``kind`` a valid mission guardrail policy document must declare.
POLICY_KIND = "mission_engine_guardrail_policy"

#: Optional absolute-path override for tests / operators.  It does NOT turn an
#: arbitrary board into a mission board on its own: the resolved policy's
#: ``board.slug`` must still match the authoritative board (unless the board is
#: independently declared a mission board via its metadata).
POLICY_ENV_OVERRIDE = "HERMES_MISSION_GUARDRAIL_POLICY"

#: Runtime default policy location, relative to the shared Hermes root
#: (``kanban_home()``).  The file's *contents* identify the governed board, so
#: the concrete board slug never needs to live in ``kanban_db``.
DEFAULT_POLICY_RELPATH = ("specs", "002-mission-engine", "mission-guardrail-policy.yaml")

# board.json declaration fields — presence of any of these makes a board a
# *declared* mission board (fail-closed on a missing/corrupt policy).
_META_MISSION_BOARD_FLAG = "mission_board"
_META_POLICY_PATH_FIELD = "mission_guardrail_policy"
_META_GUARDRAILS_FIELD = "guardrails"
_META_GUARDRAILS_PATH_FIELD = "mission_policy_path"

_ALLOWED_REGEX_FLAGS = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
}
_KNOWN_FAIL_SAFE_VALUES = {"fail_open", "fail_closed"}
_SUPPORTED_MISSION_SHAPE_MODES = {"regex_any"}

# Fallback diagnostics used only when a policy omits its own ``preflight_rules``
# diagnostics.  They deliberately preserve the ``active-cast preflight`` and
# ``goal-mode handoff preflight`` substrings that the CLI/DB tests match on.
_DEFAULT_ACTIVE_CAST_DIAGNOSTIC = (
    "Spec 002 active-cast preflight rejected assignee: active mission cards may "
    "target only the configured active cast; retired/non-canonical profiles "
    "must not receive new Mission Engine cards"
)
_DEFAULT_GOAL_MODE_DIAGNOSTIC = (
    "Spec 002 goal-mode handoff preflight rejected mission-shaped work outside "
    "Mission Commander: T1/T2 goal-mode/chat intake must create/request a "
    "Mission Commander handoff card and stop before implementation"
)


class MissionGuardrailPolicyError(ValueError):
    """A declared mission board's policy is missing / corrupt / invalid.

    Subclasses :class:`ValueError` so that ``kanban_db`` mutator call sites and
    the CLI shims — which already surface ``ValueError`` as an ``rc=2``
    actionable error — keep working unchanged.
    """


@dataclass(frozen=True)
class MissionGuardrailPolicy:
    """Immutable, validated view of a mission guardrail policy."""

    board_slug: str
    commander_role: str
    active_cast: frozenset
    mission_shape: Optional[re.Pattern]
    active_cast_diagnostic: str
    goal_mode_diagnostic: str
    source_path: Path

    def _matches_mission_shape(self, title: Optional[str], body: Optional[str]) -> bool:
        if self.mission_shape is None:
            return False
        haystack = f"{title or ''}\n{body or ''}"
        return bool(self.mission_shape.search(haystack))

    def check_card(
        self,
        *,
        title: Optional[str],
        body: Optional[str],
        assignee: Optional[str],
        created_by: Optional[str] = None,
        goal_mode: bool = False,
        parents: Iterable[str] = (),
    ) -> None:
        """Enforce the Spec 002 mission-card invariants for this board.

        Assumes the caller has already decided that this policy governs the
        board (i.e. this is the mission board).  Mirrors the historical
        hardcoded preflight exactly:

        1. Active-cast only: any set assignee outside the active cast is
           rejected.
        2. Mission Commander is always allowed (short-circuit).
        3. Otherwise, mission-shaped goal-mode / non-commander chat intake
           without a parent must be handed to Mission Commander.
        """
        if assignee and assignee not in self.active_cast:
            raise ValueError(
                f"{self.active_cast_diagnostic} "
                f"(rejected assignee={assignee!r}; active cast: "
                f"{', '.join(sorted(self.active_cast))})"
            )
        if assignee == self.commander_role:
            return
        if not self._matches_mission_shape(title, body):
            return
        creator = (created_by or "").strip()
        parent_list = tuple(p for p in parents if p)
        if goal_mode or (creator and creator != self.commander_role and not parent_list):
            raise ValueError(self.goal_mode_diagnostic)


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------

# Cache parsed policies by (path, mtime_ns, size) so repeated mutations don't
# re-parse an unchanged file, while a test/operator that rewrites the fixture
# still sees the new content (mtime and/or size change busts the key).
_POLICY_CACHE: dict = {}


def clear_cache() -> None:
    """Drop the parsed-policy cache (primarily for tests)."""
    _POLICY_CACHE.clear()


def _require(condition: bool, path: Path, message: str) -> None:
    if not condition:
        raise MissionGuardrailPolicyError(f"invalid mission guardrail policy at {path}: {message}")


def _compile_flags(flags_raw: Any, path: Path) -> int:
    if flags_raw is None:
        return 0
    _require(
        isinstance(flags_raw, (list, tuple)),
        path,
        "mission_shape.flags must be a list when present",
    )
    flags = 0
    for entry in flags_raw:
        name = str(entry).strip().upper()
        _require(
            name in _ALLOWED_REGEX_FLAGS,
            path,
            f"unsupported mission_shape flag {entry!r}; allowed: "
            f"{sorted(_ALLOWED_REGEX_FLAGS)}",
        )
        flags |= _ALLOWED_REGEX_FLAGS[name]
    return flags


def _validate_fail_safe(fail_safe: Any, path: Path) -> None:
    if fail_safe is None:
        return
    _require(isinstance(fail_safe, dict), path, "fail_safe must be a mapping when present")
    for key in (
        "ordinary_boards",
        "declared_mission_board_policy_missing",
        "declared_mission_board_policy_corrupt",
    ):
        if key in fail_safe:
            val = str(fail_safe[key]).strip()
            _require(
                val in _KNOWN_FAIL_SAFE_VALUES,
                path,
                f"fail_safe.{key} has unknown value {val!r}; allowed: "
                f"{sorted(_KNOWN_FAIL_SAFE_VALUES)}",
            )


def _diagnostics(data: Mapping[str, Any]) -> tuple[str, str]:
    active_diag = _DEFAULT_ACTIVE_CAST_DIAGNOSTIC
    goal_diag = _DEFAULT_GOAL_MODE_DIAGNOSTIC
    rules = data.get("preflight_rules")
    if isinstance(rules, dict):
        ac = rules.get("active_cast_only")
        if isinstance(ac, dict) and str(ac.get("diagnostic", "")).strip():
            active_diag = str(ac["diagnostic"]).strip()
        gm = rules.get("goal_mode_handoff")
        if isinstance(gm, dict) and str(gm.get("diagnostic", "")).strip():
            goal_diag = str(gm["diagnostic"]).strip()
    return active_diag, goal_diag


def _parse_and_validate(path: Path) -> MissionGuardrailPolicy:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissionGuardrailPolicyError(
            f"mission guardrail policy not readable at {path}: {exc}"
        ) from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise MissionGuardrailPolicyError(
            f"corrupt mission guardrail policy YAML at {path}: {exc}"
        ) from exc

    _require(isinstance(data, dict), path, f"top-level document must be a mapping, got {type(data).__name__}")

    kind = str(data.get("kind", "")).strip()
    _require(kind == POLICY_KIND, path, f"kind is {kind!r}, expected {POLICY_KIND!r}")

    board = data.get("board")
    _require(isinstance(board, dict), path, "missing 'board' mapping")
    board_slug = str(board.get("slug", "")).strip().lower()
    _require(bool(board_slug), path, "board.slug is required and must be non-empty")

    active = data.get("active_cast")
    roles_raw = active.get("roles") if isinstance(active, dict) else None
    _require(
        isinstance(roles_raw, list) and len(roles_raw) > 0,
        path,
        "active_cast.roles must be a non-empty list",
    )
    roles = []
    for role in roles_raw:
        role_name = str(role).strip()
        _require(bool(role_name), path, "active_cast.roles must not contain empty entries")
        roles.append(role_name)
    active_cast = frozenset(roles)

    commander = str(data.get("commander_role", "")).strip()
    _require(bool(commander), path, "commander_role is required and must be non-empty")
    _require(
        commander in active_cast,
        path,
        f"commander_role {commander!r} must be listed in active_cast.roles",
    )

    shape = data.get("mission_shape")
    _require(isinstance(shape, dict), path, "missing 'mission_shape' mapping")
    mode = str(shape.get("mode", "regex_any")).strip() or "regex_any"
    _require(
        mode in _SUPPORTED_MISSION_SHAPE_MODES,
        path,
        f"mission_shape.mode {mode!r} unsupported; expected one of "
        f"{sorted(_SUPPORTED_MISSION_SHAPE_MODES)}",
    )
    pattern = shape.get("pattern")
    _require(
        isinstance(pattern, str) and bool(pattern.strip()),
        path,
        "mission_shape.pattern is required and must be a non-empty string",
    )
    flags = _compile_flags(shape.get("flags"), path)
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise MissionGuardrailPolicyError(
            f"invalid mission guardrail policy at {path}: mission_shape.pattern "
            f"does not compile: {exc}"
        ) from exc

    _validate_fail_safe(data.get("fail_safe"), path)

    active_diag, goal_diag = _diagnostics(data)
    return MissionGuardrailPolicy(
        board_slug=board_slug,
        commander_role=commander,
        active_cast=active_cast,
        mission_shape=regex,
        active_cast_diagnostic=active_diag,
        goal_mode_diagnostic=goal_diag,
        source_path=path,
    )


def load_policy(path: Path | str) -> MissionGuardrailPolicy:
    """Parse + validate the policy at ``path`` (cached by path/mtime/size).

    Raises :class:`MissionGuardrailPolicyError` if the file is unreadable,
    corrupt YAML, or fails minimal enforcement-field validation.
    """
    path = Path(path)
    try:
        st = path.stat()
    except OSError as exc:
        raise MissionGuardrailPolicyError(
            f"mission guardrail policy not readable at {path}: {exc}"
        ) from exc
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _POLICY_CACHE.get(key)
    if cached is not None:
        return cached
    policy = _parse_and_validate(path)
    _POLICY_CACHE[key] = policy
    return policy


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _explicit_policy_path(board_meta: Any, home: Optional[Path]) -> Optional[Path]:
    if not isinstance(board_meta, dict):
        return None
    raw = board_meta.get(_META_POLICY_PATH_FIELD)
    if not raw:
        guardrails = board_meta.get(_META_GUARDRAILS_FIELD)
        if isinstance(guardrails, dict):
            raw = guardrails.get(_META_GUARDRAILS_PATH_FIELD)
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute() and home is not None:
        path = Path(home) / path
    return path


def _is_declared_mission_board(board_meta: Any) -> bool:
    return isinstance(board_meta, dict) and bool(board_meta.get(_META_MISSION_BOARD_FLAG))


def resolve_policy_for_board(
    board_slug: Optional[str],
    board_meta: Any,
    *,
    default_path: Optional[Path],
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[MissionGuardrailPolicy]:
    """Return the policy governing ``board_slug``, or ``None`` to fail open.

    * An **undeclared** board fails open unless a discoverable policy
      (env override, then ``default_path``) exists *and* names this exact
      board via its ``board.slug``.
    * A **declared** mission board (board metadata ``mission_board: true`` or an
      explicit policy-path field) fails closed — raising
      :class:`MissionGuardrailPolicyError` — when its policy is missing,
      corrupt, invalid, or names a different board.

    The environment override never silently promotes an arbitrary board to a
    mission board: for undeclared boards, a slug mismatch simply fails open.
    """
    slug = (board_slug or "").strip().lower()

    explicit = _explicit_policy_path(board_meta, home)
    declared = _is_declared_mission_board(board_meta) or explicit is not None

    if env is None:
        env = os.environ
    env_raw = (env.get(POLICY_ENV_OVERRIDE) or "").strip()
    env_path = Path(env_raw).expanduser() if env_raw else None

    # An explicitly declared policy path is authoritative and must exist as
    # written — no silent fallback to env/default that could load a *different*
    # policy behind the operator's back.
    if explicit is not None:
        candidates = [explicit]
    else:
        candidates = [p for p in (env_path, default_path) if p is not None]

    chosen: Optional[Path] = None
    for cand in candidates:
        try:
            if cand and Path(cand).exists():
                chosen = Path(cand)
                break
        except OSError:
            continue

    if chosen is None:
        if declared:
            searched = ", ".join(str(c) for c in candidates) or "<none>"
            raise MissionGuardrailPolicyError(
                f"declared mission board {slug!r} has no loadable mission "
                f"guardrail policy (searched: {searched}); refusing to mutate "
                f"mission cards without an enforceable policy (fail-closed)"
            )
        return None

    try:
        policy = load_policy(chosen)
    except MissionGuardrailPolicyError:
        if declared:
            raise
        # Undeclared board: a corrupt/invalid discoverable policy is not ours
        # to enforce — fail open rather than brick an ordinary board.
        return None

    if policy.board_slug != slug:
        if declared:
            raise MissionGuardrailPolicyError(
                f"declared mission board {slug!r} resolved a guardrail policy for "
                f"a different board ({policy.board_slug!r}) at {chosen}; refusing "
                f"to mutate (fail-closed)"
            )
        return None

    return policy
