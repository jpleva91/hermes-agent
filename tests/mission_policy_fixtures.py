"""Shared test helpers for the externalized Spec 002 mission guardrail policy.

The mission board slug, active cast, Mission Commander role, and mission-shape
regex used to be hardcoded in ``hermes_cli.kanban_db``. They now live in a
versioned YAML policy loaded by ``hermes_cli.mission_guardrail_policy``. Tests
that exercise the guardrail write that policy into their isolated ``HERMES_HOME``
via these helpers, so a single source of truth keeps the DB-layer and dashboard
tests from drifting.
"""

from __future__ import annotations

from pathlib import Path

import yaml

MISSION_BOARD_SLUG = "fable-emulation-workflow"

CANONICAL_ACTIVE_CAST = [
    "missioncommander",
    "specsteward",
    "sourcecartographer",
    "claudecodeconductor",
    "codexoperator",
    "gatewarden",
    "runtimesteward",
]

CANONICAL_MISSION_PATTERN = (
    r"\b(t[12]|mission|multi[- ]?step|implement|implementation|architecture|"
    r"product behavior|user[- ]visible|source packet|rehearsal|review|gate|"
    r"evidence|delegate_task|background agent|deploy|release|closeout)\b"
)

# Diagnostics deliberately preserve the ``active-cast preflight`` and
# ``goal-mode handoff preflight`` substrings the CLI/DB/dashboard tests match on.
CANONICAL_ACTIVE_CAST_DIAGNOSTIC = (
    "Spec 002 active-cast preflight rejected assignee: active mission cards may "
    "target only the configured active cast; retired/non-canonical profiles "
    "must not receive new Mission Engine cards."
)
CANONICAL_GOAL_MODE_DIAGNOSTIC = (
    "Spec 002 goal-mode handoff preflight rejected mission-shaped work outside "
    "Mission Commander: T1/T2 goal-mode/chat intake must create/request a "
    "Mission Commander handoff card and stop before implementation."
)
CANONICAL_ARTIFACT_CONTRACT_DIAGNOSTIC = (
    "Spec 002 artifact-driven handoff preflight rejected mission-shaped worker card: "
    "executor lanes require durable Spec Kit/source artifacts before implementation."
)

DEFAULT_POLICY_RELPATH = ("specs", "002-mission-engine", "mission-guardrail-policy.yaml")


def mission_policy_doc(
    *,
    slug=MISSION_BOARD_SLUG,
    roles=None,
    commander="missioncommander",
    pattern=None,
    flags=("IGNORECASE",),
    active_diag=CANONICAL_ACTIVE_CAST_DIAGNOSTIC,
    goal_diag=CANONICAL_GOAL_MODE_DIAGNOSTIC,
    artifact_contract=False,
):
    roles = list(CANONICAL_ACTIVE_CAST) if roles is None else list(roles)
    pattern = CANONICAL_MISSION_PATTERN if pattern is None else pattern
    doc = {
        "version": 1,
        "kind": "mission_engine_guardrail_policy",
        "policy_id": "spec-002-mission-engine-guardrails",
        "status": "proposed",
        "board": {"slug": slug, "mission_board": True},
        "fail_safe": {
            "ordinary_boards": "fail_open",
            "declared_mission_board_policy_missing": "fail_closed",
            "declared_mission_board_policy_corrupt": "fail_closed",
            "no_silent_downgrade": True,
        },
        "active_cast": {"source": "spec_002_role_cast_v2", "roles": roles},
        "commander_role": commander,
        "mission_shape": {"mode": "regex_any", "pattern": pattern, "flags": list(flags)},
        "preflight_rules": {
            "active_cast_only": {"diagnostic": active_diag},
            "goal_mode_handoff": {"diagnostic": goal_diag},
        },
    }
    if artifact_contract:
        doc["preflight_rules"]["artifact_driven_handoff"] = {
            "enabled": True,
            "diagnostic": CANONICAL_ARTIFACT_CONTRACT_DIAGNOSTIC,
            "required_patterns": [
                {"name": "spec_kit", "pattern": r"(Spec Kit|specs/|spec\.md|mission-contract\.ya?ml)", "flags": ["IGNORECASE"]},
                {"name": "source_packet", "pattern": r"(source packet|NotebookLM|Notebook LM|source-packet\.md|evidence\.md)", "flags": ["IGNORECASE"]},
            ],
        }
    return doc


def write_mission_policy(home, *, relpath=DEFAULT_POLICY_RELPATH, **kwargs):
    """Write a mission guardrail policy YAML under ``home`` and return its path."""
    path = Path(home).joinpath(*relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(mission_policy_doc(**kwargs), sort_keys=False), encoding="utf-8")
    return path
