"""Unit tests for the externalized Spec 002 mission guardrail policy loader.

These exercise :mod:`hermes_cli.mission_guardrail_policy` directly (parsing,
validation, and the fail-open / fail-closed resolution contract) without going
through the Kanban DB layer, so a regression is attributable to the loader.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_cli import mission_guardrail_policy as mgp
from hermes_cli.mission_guardrail_policy import MissionGuardrailPolicyError


CANONICAL_PATTERN = (
    r"\b(t[12]|mission|multi[- ]?step|implement|implementation|architecture|"
    r"product behavior|user[- ]visible|source packet|rehearsal|review|gate|"
    r"evidence|delegate_task|background agent|deploy|release|closeout)\b"
)


def _policy_doc(
    *,
    slug="mission-board",
    roles=("missioncommander", "specsteward", "runtimesteward"),
    commander="missioncommander",
    pattern=CANONICAL_PATTERN,
    flags=("IGNORECASE",),
    kind="mission_engine_guardrail_policy",
    artifact_contract=False,
):
    doc = {
        "version": 1,
        "kind": kind,
        "board": {"slug": slug, "mission_board": True},
        "fail_safe": {
            "ordinary_boards": "fail_open",
            "declared_mission_board_policy_missing": "fail_closed",
            "declared_mission_board_policy_corrupt": "fail_closed",
        },
        "active_cast": {"roles": list(roles)},
        "commander_role": commander,
        "mission_shape": {"mode": "regex_any", "pattern": pattern, "flags": list(flags)},
        "preflight_rules": {
            "active_cast_only": {
                "diagnostic": "Spec 002 active-cast preflight rejected assignee"
            },
            "goal_mode_handoff": {
                "diagnostic": "Spec 002 goal-mode handoff preflight rejected mission-shaped work"
            },
        },
    }
    if artifact_contract:
        doc["preflight_rules"]["artifact_driven_handoff"] = {
            "enabled": True,
            "diagnostic": "Spec 002 artifact-driven handoff preflight rejected mission-shaped worker card",
            "required_patterns": [
                {"name": "spec_kit", "pattern": r"(spec kit|specs/|spec\.md|mission-contract\.ya?ml)"},
                {"name": "source_packet", "pattern": r"(source packet|notebook\s*lm|notebooklm|evidence\.md)"},
            ],
        }
    return doc


def _write(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(doc, str):
        path.write_text(doc, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_cache():
    mgp.clear_cache()
    yield
    mgp.clear_cache()


# ---------------------------------------------------------------------------
# load_policy: parsing + validation
# ---------------------------------------------------------------------------

def test_load_valid_policy_parses_and_compiles_regex(tmp_path):
    path = _write(tmp_path / "policy.yaml", _policy_doc())
    policy = mgp.load_policy(path)
    assert policy.board_slug == "mission-board"
    assert policy.commander_role == "missioncommander"
    assert "runtimesteward" in policy.active_cast
    assert policy.mission_shape is not None
    # IGNORECASE flag applied.
    assert policy.mission_shape.search("build the T2 MISSION")


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(MissionGuardrailPolicyError):
        mgp.load_policy(tmp_path / "nope.yaml")


def test_load_corrupt_yaml_raises(tmp_path):
    path = _write(tmp_path / "policy.yaml", "kind: mission_engine_guardrail_policy\nboard: {slug: x\n")
    with pytest.raises(MissionGuardrailPolicyError, match="corrupt"):
        mgp.load_policy(path)


def test_load_non_mapping_raises(tmp_path):
    path = _write(tmp_path / "policy.yaml", "- just\n- a\n- list\n")
    with pytest.raises(MissionGuardrailPolicyError, match="mapping"):
        mgp.load_policy(path)


def test_load_wrong_kind_raises(tmp_path):
    path = _write(tmp_path / "policy.yaml", _policy_doc(kind="something_else"))
    with pytest.raises(MissionGuardrailPolicyError, match="kind"):
        mgp.load_policy(path)


def test_load_missing_active_cast_raises(tmp_path):
    doc = _policy_doc()
    del doc["active_cast"]
    path = _write(tmp_path / "policy.yaml", doc)
    with pytest.raises(MissionGuardrailPolicyError, match="active_cast.roles"):
        mgp.load_policy(path)


def test_load_missing_commander_raises(tmp_path):
    doc = _policy_doc()
    del doc["commander_role"]
    path = _write(tmp_path / "policy.yaml", doc)
    with pytest.raises(MissionGuardrailPolicyError, match="commander_role"):
        mgp.load_policy(path)


def test_load_commander_not_in_cast_raises(tmp_path):
    doc = _policy_doc(roles=("specsteward", "runtimesteward"), commander="missioncommander")
    path = _write(tmp_path / "policy.yaml", doc)
    with pytest.raises(MissionGuardrailPolicyError, match="commander_role"):
        mgp.load_policy(path)


def test_load_uncompilable_regex_raises(tmp_path):
    doc = _policy_doc(pattern=r"(unterminated")
    path = _write(tmp_path / "policy.yaml", doc)
    with pytest.raises(MissionGuardrailPolicyError, match="compile"):
        mgp.load_policy(path)


def test_load_unsupported_flag_raises(tmp_path):
    doc = _policy_doc(flags=("IGNORECASE", "VERBOSE"))
    path = _write(tmp_path / "policy.yaml", doc)
    with pytest.raises(MissionGuardrailPolicyError, match="flag"):
        mgp.load_policy(path)


def test_load_unknown_fail_safe_value_raises(tmp_path):
    doc = _policy_doc()
    doc["fail_safe"]["ordinary_boards"] = "explode"
    path = _write(tmp_path / "policy.yaml", doc)
    with pytest.raises(MissionGuardrailPolicyError, match="fail_safe"):
        mgp.load_policy(path)


def test_load_is_cached_until_mtime_changes(tmp_path):
    path = _write(tmp_path / "policy.yaml", _policy_doc(roles=("missioncommander", "alpha")))
    first = mgp.load_policy(path)
    assert "alpha" in first.active_cast
    # Rewrite with a different cast; a fresh stat (size/mtime) busts the cache.
    _write(path, _policy_doc(roles=("missioncommander", "beta", "gamma")))
    second = mgp.load_policy(path)
    assert "beta" in second.active_cast
    assert "alpha" not in second.active_cast


# ---------------------------------------------------------------------------
# resolve_policy_for_board: fail-open / fail-closed contract
# ---------------------------------------------------------------------------

def test_undeclared_board_no_policy_fails_open(tmp_path):
    assert (
        mgp.resolve_policy_for_board(
            "ordinary", {}, default_path=tmp_path / "missing.yaml", home=tmp_path, env={}
        )
        is None
    )


def test_undeclared_board_slug_mismatch_fails_open(tmp_path):
    path = _write(tmp_path / "policy.yaml", _policy_doc(slug="mission-board"))
    # Default policy names 'mission-board'; ordinary board != that -> ignored.
    assert (
        mgp.resolve_policy_for_board(
            "ordinary", {}, default_path=path, home=tmp_path, env={}
        )
        is None
    )


def test_undeclared_board_slug_match_enforces(tmp_path):
    path = _write(tmp_path / "policy.yaml", _policy_doc(slug="mission-board"))
    policy = mgp.resolve_policy_for_board(
        "mission-board", {}, default_path=path, home=tmp_path, env={}
    )
    assert policy is not None
    assert policy.board_slug == "mission-board"


def test_declared_board_missing_policy_fails_closed(tmp_path):
    meta = {"mission_board": True}
    with pytest.raises(MissionGuardrailPolicyError, match="fail-closed"):
        mgp.resolve_policy_for_board(
            "mission-board", meta, default_path=tmp_path / "missing.yaml", home=tmp_path, env={}
        )


def test_declared_board_corrupt_policy_fails_closed(tmp_path):
    path = _write(tmp_path / "policy.yaml", "kind: mission_engine_guardrail_policy\nboard: {slug: x\n")
    meta = {"mission_board": True}
    with pytest.raises(MissionGuardrailPolicyError):
        mgp.resolve_policy_for_board(
            "mission-board", meta, default_path=path, home=tmp_path, env={}
        )


def test_declared_board_slug_mismatch_fails_closed(tmp_path):
    path = _write(tmp_path / "policy.yaml", _policy_doc(slug="other-board"))
    meta = {"mission_board": True}
    with pytest.raises(MissionGuardrailPolicyError, match="different board"):
        mgp.resolve_policy_for_board(
            "mission-board", meta, default_path=path, home=tmp_path, env={}
        )


def test_explicit_metadata_policy_path_is_authoritative(tmp_path):
    explicit = _write(tmp_path / "explicit.yaml", _policy_doc(slug="mission-board"))
    # A different default policy must be ignored in favour of the explicit path.
    other = _write(tmp_path / "default.yaml", _policy_doc(slug="mission-board", roles=("missioncommander", "zzz")))
    meta = {"mission_guardrail_policy": str(explicit)}
    policy = mgp.resolve_policy_for_board(
        "mission-board", meta, default_path=other, home=tmp_path, env={}
    )
    assert policy is not None
    assert "zzz" not in policy.active_cast


def test_explicit_missing_path_fails_closed_without_falling_back(tmp_path):
    # Even though a valid default exists, an explicitly-declared missing path
    # must fail closed rather than silently load a different policy.
    default_ok = _write(tmp_path / "default.yaml", _policy_doc(slug="mission-board"))
    meta = {"mission_guardrail_policy": str(tmp_path / "gone.yaml")}
    with pytest.raises(MissionGuardrailPolicyError, match="fail-closed"):
        mgp.resolve_policy_for_board(
            "mission-board", meta, default_path=default_ok, home=tmp_path, env={}
        )


def test_env_override_respected(tmp_path):
    override = _write(tmp_path / "override.yaml", _policy_doc(slug="mission-board"))
    env = {mgp.POLICY_ENV_OVERRIDE: str(override)}
    policy = mgp.resolve_policy_for_board(
        "mission-board", {}, default_path=tmp_path / "missing.yaml", home=tmp_path, env=env
    )
    assert policy is not None
    assert policy.board_slug == "mission-board"


def test_env_override_does_not_promote_arbitrary_board(tmp_path):
    override = _write(tmp_path / "override.yaml", _policy_doc(slug="mission-board"))
    env = {mgp.POLICY_ENV_OVERRIDE: str(override)}
    # Undeclared board whose slug != policy slug: env override must NOT govern it.
    assert (
        mgp.resolve_policy_for_board(
            "some-other-board", {}, default_path=None, home=tmp_path, env=env
        )
        is None
    )


# ---------------------------------------------------------------------------
# The named fixture-control proof from the source packet.
# ---------------------------------------------------------------------------

def test_policy_fixture_controls_board_slug_cast_and_regex(tmp_path):
    """Prove board slug, active cast, commander, and regex are all external."""
    path = _write(
        tmp_path / "policy.yaml",
        _policy_doc(
            slug="mission-test-board",
            roles=("alpha", "beta"),
            commander="alpha",
            pattern=r"\bdragonfruit-mission\b",
        ),
    )
    meta = {"mission_board": True, "mission_guardrail_policy": str(path)}
    policy = mgp.resolve_policy_for_board(
        "mission-test-board", meta, default_path=None, home=tmp_path, env={}
    )
    assert policy is not None
    assert policy.board_slug == "mission-test-board"
    assert policy.active_cast == frozenset({"alpha", "beta"})
    assert policy.commander_role == "alpha"

    # alpha (commander, in cast) is always allowed.
    policy.check_card(title="anything", body=None, assignee="alpha")
    # A canonical Mission Engine role is NOT in this cast -> rejected.
    with pytest.raises(ValueError, match="active-cast preflight"):
        policy.check_card(title="anything", body=None, assignee="runtimesteward")
    # Canonical mission words do not match this policy's unique regex token.
    policy.check_card(
        title="Implement T2 mission architecture",
        body="rehearsal gate evidence",
        assignee="beta",
        created_by="beta",
        goal_mode=True,
    )
    # The policy's unique token trips the commander-handoff guard.
    with pytest.raises(ValueError, match="goal-mode handoff preflight"):
        policy.check_card(
            title="dragonfruit-mission kickoff",
            body=None,
            assignee="beta",
            created_by="beta",
            goal_mode=True,
        )


def test_check_card_commander_handoff_with_parent_allowed(tmp_path):
    path = _write(tmp_path / "policy.yaml", _policy_doc(slug="mission-board"))
    policy = mgp.load_policy(path)
    # Mission-shaped, non-commander creator, but has a parent -> allowed (this is
    # the Mission-Commander-minted child card path).
    policy.check_card(
        title="Implement mission architecture",
        body="source packet rehearsal gate evidence",
        assignee="runtimesteward",
        created_by="missioncommander",
        parents=["t_parent"],
    )
    # Same shape but goal-mode intake from a non-commander with no parent -> reject.
    with pytest.raises(ValueError, match="goal-mode handoff preflight"):
        policy.check_card(
            title="Implement mission architecture",
            body="source packet rehearsal gate evidence",
            assignee="runtimesteward",
            created_by="specsteward",
            goal_mode=True,
        )


def test_artifact_contract_rejects_commander_child_without_spec_and_source_packets(tmp_path):
    path = _write(tmp_path / "policy.yaml", _policy_doc(slug="mission-board", artifact_contract=True))
    policy = mgp.load_policy(path)

    with pytest.raises(ValueError, match="artifact-driven handoff preflight.*spec_kit, source_packet"):
        policy.check_card(
            title="Implement mission architecture",
            body="Please build the feature and write a handoff.",
            assignee="runtimesteward",
            created_by="missioncommander",
            parents=["t_parent"],
        )


def test_artifact_contract_allows_commander_child_with_spec_and_source_packets(tmp_path):
    path = _write(tmp_path / "policy.yaml", _policy_doc(slug="mission-board", artifact_contract=True))
    policy = mgp.load_policy(path)

    policy.check_card(
        title="Implement mission architecture",
        body=(
            "Spec Kit: /home/red/.hermes/mission-control/specs/003-artifact-driven-preflight/spec.md\n"
            "Source packet: /home/red/.hermes/mission-control/specs/003-artifact-driven-preflight/source-packet.md\n"
            "Evidence: /home/red/.hermes/mission-control/specs/003-artifact-driven-preflight/evidence.md"
        ),
        assignee="runtimesteward",
        created_by="missioncommander",
        parents=["t_parent"],
    )
