import json
from pathlib import Path

from plugins.kanban.governance import (
    extract_lane_registry_contract,
    status_payload,
    validate_governance_path,
    validate_lane_registry_contract,
    validate_spec_reference_preflight,
)


VALID_GOVERNANCE = """
version: 1
spec_governed: true
spec_reference_required: true
spec_mode: full_spec_kit
canonical_spec: specs/001-clawta-hermes-agent-workflow/spec.md
allowed_reference_forms:
  - full_spec_kit_artifact_path
  - lite_spec_kit_mission_contract_path
  - legacy_migration_exception
override_authority: Jared
high_risk_requires_verifier: true
extensions:
  board_slug: clawta-hermes-agent-workflow
  note: Hermes-specific sidecar, not upstream Spec Kit replacement
""".lstrip()


def test_valid_governance_yaml_passes(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(VALID_GOVERNANCE, encoding="utf-8")

    result = validate_governance_path(path)

    assert result.ok is True
    assert result.errors == []
    assert result.warnings == []
    assert result.data["spec_reference_required"] is True
    assert result.data["extensions"]["board_slug"] == "clawta-hermes-agent-workflow"


def test_missing_required_fields_fail(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text("version: 1\nspec_governed: true\n", encoding="utf-8")

    result = validate_governance_path(path)

    assert result.ok is False
    assert "missing required field: canonical_spec" in result.errors
    assert "missing required field: allowed_reference_forms" in result.errors


def test_unknown_top_level_fields_warn_but_do_not_fail(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(VALID_GOVERNANCE + "future_field: allowed later\n", encoding="utf-8")

    result = validate_governance_path(path)

    assert result.ok is True
    assert result.errors == []
    assert result.warnings == ["unknown top-level field: future_field"]


def test_extensions_allows_nested_unknown_fields(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(
        VALID_GOVERNANCE + "  nested_future:\n    arbitrary: true\n",
        encoding="utf-8",
    )

    result = validate_governance_path(path)

    assert result.ok is True
    assert result.errors == []
    assert result.warnings == []
    json.dumps(status_payload(result))


def test_spec_reference_preflight_rejects_invalid_governance_sidecar(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text("version: 1\nspec_governed: true\n", encoding="utf-8")
    governance = validate_governance_path(path)

    decision = validate_spec_reference_preflight(
        "spec_reference: specs/001-clawta-hermes-agent-workflow/spec.md\n",
        governance,
    )

    assert decision.ok is False
    assert decision.reason == "governance_invalid"
    assert "governance sidecar is invalid" in decision.diagnostic
    assert str(path) in decision.diagnostic


VALID_LANE_GOVERNANCE = VALID_GOVERNANCE + """
lane_registry:
  enabled: true
  mode: block
  schema: lane-invocation-registry/v0.1
  canonical_registry: AI Chief of Staff/Agentic SDLC v4 - Lane Invocation Registry.md
  require_pre_dispatch_record: true
  require_review_for_verification_actor: true
  default_fallback_policy: block
  external_writes_require_jared: true
"""

VALID_LANE_TASK_BODY = """
executor_lane: hermes-profile
harness: hermes-profile
workspace: dir:/home/red/Documents/Obsidian Vault
authority_scope: docs-only
side_effect_authority: docs/comments only
evidence_expectations:
  - Run focused governance tests.
fallback_policy: block
verification_actor: posreview
input_refs:
  - AI Chief of Staff/Agentic SDLC v4 - Dispatcher Plugin Enforcement Spec.md

Task:
Implement validator-only lane registry checks.
""".lstrip()


def test_lane_registry_governance_config_passes(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(VALID_LANE_GOVERNANCE, encoding="utf-8")

    result = validate_governance_path(path)

    assert result.ok is True
    assert result.errors == []
    assert result.data["lane_registry"]["mode"] == "block"
    payload = status_payload(result)
    assert payload["lane_registry"]["schema"] == "lane-invocation-registry/v0.1"


def test_lane_registry_governance_config_rejects_invalid_mode_and_schema(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(
        VALID_GOVERNANCE
        + """
lane_registry:
  enabled: yes
  mode: enforce
  schema: lane-invocation-registry/v99
  default_fallback_policy: silently-fallback
  unknown_future_field: ok later
""",
        encoding="utf-8",
    )

    result = validate_governance_path(path)

    assert result.ok is False
    assert "lane_registry.mode must be one of: block, off, warn" in result.errors
    assert "lane_registry.schema must be lane-invocation-registry/v0.1" in result.errors
    assert any("default_fallback_policy" in error for error in result.errors)
    assert result.warnings == ["unknown lane_registry field: unknown_future_field"]


def test_extract_lane_registry_contract_from_leading_yaml_block():
    contract = extract_lane_registry_contract(VALID_LANE_TASK_BODY)

    assert contract["executor_lane"] == "hermes-profile"
    assert contract["workspace"] == "dir:/home/red/Documents/Obsidian Vault"
    assert contract["evidence_expectations"] == ["Run focused governance tests."]
    assert "Task" not in contract


def test_lane_registry_contract_accepts_valid_task_metadata(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(VALID_LANE_GOVERNANCE, encoding="utf-8")
    governance = validate_governance_path(path)

    result = validate_lane_registry_contract(VALID_LANE_TASK_BODY, governance)

    assert result.ok is True
    assert result.errors == []
    assert result.reason is None
    assert result.mode == "block"
    assert result.schema == "lane-invocation-registry/v0.1"
    assert result.contract["verification_actor"] == "posreview"


def test_lane_registry_contract_reports_missing_required_fields(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(VALID_LANE_GOVERNANCE, encoding="utf-8")
    governance = validate_governance_path(path)

    result = validate_lane_registry_contract("executor_lane: hermes-profile\n", governance)

    assert result.ok is False
    assert result.reason == "lane_registry_contract_invalid"
    assert "missing required lane registry field: evidence_expectations" in result.errors
    assert "missing required lane registry field: fallback_policy" in result.errors
    assert "missing required lane registry field: input_refs" in result.errors


def test_lane_registry_contract_reports_invalid_values(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(VALID_LANE_GOVERNANCE, encoding="utf-8")
    governance = validate_governance_path(path)
    body = """
executor_lane: magic-lane
harness: shell
workspace: relative/path
authority_scope: root
side_effect_authority: anything
evidence_expectations: run tests
fallback_policy: silently-do-it
verification_actor: worker-self
input_refs: spec.md
""".lstrip()

    result = validate_lane_registry_contract(body, governance)

    assert result.ok is False
    assert any("executor_lane/lane_id" in error for error in result.errors)
    assert any("harness" in error for error in result.errors)
    assert any("workspace" in error for error in result.errors)
    assert any("authority_scope" in error for error in result.errors)
    assert any("side_effect_authority" in error for error in result.errors)
    assert "evidence_expectations must be a non-empty list of strings" in result.errors
    assert "input_refs must be a list of strings" in result.errors
    assert any("fallback_policy" in error for error in result.errors)
    assert any("verification_actor" in error for error in result.errors)


def test_lane_registry_disabled_is_noop_validator_only(tmp_path):
    path = tmp_path / "governance.yaml"
    path.write_text(
        VALID_GOVERNANCE
        + """
lane_registry:
  enabled: false
  mode: off
  schema: lane-invocation-registry/v0.1
""",
        encoding="utf-8",
    )
    governance = validate_governance_path(path)

    result = validate_lane_registry_contract("", governance)

    assert result.ok is True
    assert result.mode == "off"
    assert result.contract == {}
