"""Spec Governance sidecar parsing and validation for Kanban missions.

The source of truth is a Hermes-specific ``governance.yaml`` sidecar in a
Spec Kit folder. Kanban DB metadata may cache this later; v0 exposes status
and supports the narrow dispatch preflight gate, but deliberately does not
enforce completion gates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GOVERNANCE_FILENAME = "governance.yaml"
REQUIRED_FIELDS = (
    "version",
    "spec_governed",
    "spec_reference_required",
    "spec_mode",
    "canonical_spec",
    "allowed_reference_forms",
    "override_authority",
    "high_risk_requires_verifier",
    "extensions",
)
ALLOWED_REFERENCE_FORMS = {
    "full_spec_kit_artifact_path",
    "lite_spec_kit_mission_contract_path",
    "legacy_migration_exception",
    "obsidian_note_path",
}
ALLOWED_LANE_REGISTRY_FIELDS = {
    "enabled",
    "mode",
    "schema",
    "canonical_registry",
    "require_pre_dispatch_record",
    "require_review_for_verification_actor",
    "default_fallback_policy",
    "external_writes_require_jared",
}
ALLOWED_TOP_LEVEL_FIELDS = set(REQUIRED_FIELDS) | {"lane_registry"}
LANE_REGISTRY_MODES = {"off", "warn", "block"}
LANE_REGISTRY_SCHEMA = "lane-invocation-registry/v0.1"
LANE_REGISTRY_REQUIRED_CONTRACT_FIELDS = (
    "harness",
    "workspace",
    "authority_scope",
    "side_effect_authority",
    "evidence_expectations",
    "fallback_policy",
    "verification_actor",
    "input_refs",
)
LANE_REGISTRY_REQUIRED_ONE_OF = ("executor_lane", "lane_id")
ALLOWED_EXECUTOR_LANES = {
    "hermes-profile",
    "claude-code-kitty",
    "codex-cli",
    "antigravity-kitty",
    "local-gpu",
    "human-jared",
    "external-system",
}
ALLOWED_HARNESSES = {"hermes-profile", "kitty-claude", "codex-cli", "agy", "ollama", "manual"}
ALLOWED_AUTHORITY_SCOPES = {"docs-only", "local-files", "PR-only", "external-api", "production"}
ALLOWED_SIDE_EFFECT_AUTHORITIES = {
    "docs/comments only",
    "local files/tests",
    "PR only",
    "external API",
    "production action",
}
ALLOWED_FALLBACK_POLICIES = {"block", "reassign-to-hermes", "ask-jared", "docs-only"}
ALLOWED_VERIFICATION_ACTORS = {"posreview", "Codex", "Claude-review", "Jared", "none"}


@dataclass(frozen=True)
class GovernanceResult:
    """Validation result for a Spec Governance sidecar."""

    path: Path
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SpecReferencePreflight:
    """Dispatch preflight decision for one Kanban card under governance."""

    ok: bool
    reason: str | None = None
    diagnostic: str = ""
    spec_reference: Any = None
    reference_form: str | None = None
    governance_path: Path | None = None


@dataclass(frozen=True)
class LaneRegistryValidation:
    """Validator-only result for lane registry governance checks.

    Slice 1 deliberately returns diagnostics only; callers must not use this
    result to block Kanban claims until the dispatch preflight slice wires it in.
    """

    ok: bool
    reason: str | None = None
    contract: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mode: str = "off"
    schema: str | None = None
    governance_path: Path | None = None


def governance_file_for(path: Path | str) -> Path:
    """Return the governance sidecar path for either a file or spec directory."""

    candidate = Path(path)
    if candidate.name == GOVERNANCE_FILENAME or candidate.suffix in {".yaml", ".yml"}:
        return candidate
    return candidate / GOVERNANCE_FILENAME


def _load_yaml(path: Path) -> tuple[dict[str, Any], list[str]]:
    if not path.exists():
        return {}, [f"missing governance sidecar: {path}"]
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return {}, [f"invalid YAML: {exc}"]
    except OSError as exc:
        return {}, [f"could not read governance sidecar: {exc}"]
    if raw is None:
        return {}, ["governance sidecar is empty"]
    if not isinstance(raw, dict):
        return {}, ["governance sidecar must be a YAML mapping"]
    return dict(raw), []


def _normalize_lane_registry_mode(value: Any) -> str | Any:
    # YAML 1.1 parses unquoted ``off`` as False. Treat it as the documented
    # string mode to avoid surprising sidecar failures.
    if value is False:
        return "off"
    return value


def validate_lane_registry_config(value: Any) -> tuple[list[str], list[str]]:
    """Validate the optional ``lane_registry`` governance sidecar section."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(value, dict):
        return ["lane_registry must be a mapping"], warnings

    for field_name in sorted(set(value) - ALLOWED_LANE_REGISTRY_FIELDS):
        warnings.append(f"unknown lane_registry field: {field_name}")

    if "enabled" in value and not isinstance(value["enabled"], bool):
        errors.append("lane_registry.enabled must be a boolean")
    if "mode" in value:
        mode = _normalize_lane_registry_mode(value["mode"])
        if not isinstance(mode, str) or mode not in LANE_REGISTRY_MODES:
            errors.append("lane_registry.mode must be one of: block, off, warn")
    if "schema" in value:
        schema = value["schema"]
        if not isinstance(schema, str):
            errors.append("lane_registry.schema must be a string")
        elif schema != LANE_REGISTRY_SCHEMA:
            errors.append(f"lane_registry.schema must be {LANE_REGISTRY_SCHEMA}")
    if "canonical_registry" in value and not isinstance(value["canonical_registry"], str):
        errors.append("lane_registry.canonical_registry must be a string path")
    for bool_field in (
        "require_pre_dispatch_record",
        "require_review_for_verification_actor",
        "external_writes_require_jared",
    ):
        if bool_field in value and not isinstance(value[bool_field], bool):
            errors.append(f"lane_registry.{bool_field} must be a boolean")
    if "default_fallback_policy" in value:
        fallback = value["default_fallback_policy"]
        if not isinstance(fallback, str) or fallback not in ALLOWED_FALLBACK_POLICIES:
            errors.append(
                "lane_registry.default_fallback_policy must be one of: "
                f"{', '.join(sorted(ALLOWED_FALLBACK_POLICIES))}"
            )
    return errors, warnings


def validate_governance_path(path: Path | str) -> GovernanceResult:
    """Validate a Spec Governance sidecar without applying any gates."""

    governance_path = governance_file_for(path)
    data, load_errors = _load_yaml(governance_path)
    errors = list(load_errors)
    warnings: list[str] = []

    if data:
        for field_name in REQUIRED_FIELDS:
            if field_name not in data:
                errors.append(f"missing required field: {field_name}")
        for field_name in sorted(set(data) - ALLOWED_TOP_LEVEL_FIELDS):
            warnings.append(f"unknown top-level field: {field_name}")

        if "spec_governed" in data and not isinstance(data["spec_governed"], bool):
            errors.append("spec_governed must be a boolean")
        if "spec_reference_required" in data and not isinstance(data["spec_reference_required"], bool):
            errors.append("spec_reference_required must be a boolean")
        if "high_risk_requires_verifier" in data and not isinstance(data["high_risk_requires_verifier"], bool):
            errors.append("high_risk_requires_verifier must be a boolean")
        if "canonical_spec" in data and not isinstance(data["canonical_spec"], str):
            errors.append("canonical_spec must be a string path")
        if "spec_mode" in data and not isinstance(data["spec_mode"], str):
            errors.append("spec_mode must be a string")
        if "override_authority" in data and not isinstance(data["override_authority"], str):
            errors.append("override_authority must be a string")
        if "allowed_reference_forms" in data:
            forms = data["allowed_reference_forms"]
            if not isinstance(forms, list) or not forms or not all(isinstance(item, str) for item in forms):
                errors.append("allowed_reference_forms must be a non-empty list of strings")
            else:
                for form in forms:
                    if form not in ALLOWED_REFERENCE_FORMS:
                        warnings.append(f"unknown allowed_reference_form: {form}")
        if "extensions" in data and not isinstance(data["extensions"], dict):
            errors.append("extensions must be a mapping")
        if "lane_registry" in data:
            lane_errors, lane_warnings = validate_lane_registry_config(data["lane_registry"])
            errors.extend(lane_errors)
            warnings.extend(lane_warnings)

    return GovernanceResult(
        path=governance_path,
        ok=not errors,
        data=data,
        errors=errors,
        warnings=warnings,
    )


_SPEC_REFERENCE_LINE_RE = re.compile(r"(?im)^\s*spec_reference\s*:\s*(.+?)\s*$")


def _frontmatter_payload(body: str) -> str | None:
    if not body.startswith("---"):
        return None
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:idx])
    return None


def extract_spec_reference(body: str | None) -> Any:
    """Extract ``spec_reference`` from YAML frontmatter or simple body metadata."""

    if not body:
        return None
    candidates: list[str] = []
    frontmatter = _frontmatter_payload(body)
    if frontmatter:
        candidates.append(frontmatter)
    candidates.append(body)

    for candidate in candidates:
        try:
            parsed = yaml.safe_load(candidate)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("spec_reference") is not None:
            return parsed.get("spec_reference")

    match = _SPEC_REFERENCE_LINE_RE.search(body)
    if match:
        return match.group(1).strip().strip('"\'') or None
    return None


def _is_spec_path(value: str) -> bool:
    value = value.strip()
    return bool(value) and (value.startswith("specs/") or "/specs/" in value)


def _classify_reference(spec_reference: Any) -> tuple[str | None, str | None]:
    if isinstance(spec_reference, dict):
        form = spec_reference.get("form") or spec_reference.get("type")
        path = spec_reference.get("path") or spec_reference.get("artifact") or spec_reference.get("value")
        if isinstance(form, str) and form.strip():
            return form.strip(), str(path).strip() if path is not None else None
        if isinstance(path, str):
            return _classify_reference(path)
        return None, None
    if not isinstance(spec_reference, str):
        return None, None
    ref = spec_reference.strip()
    lowered = ref.casefold()
    if lowered == "legacy_migration_exception" or lowered.startswith("legacy_migration_exception:"):
        return "legacy_migration_exception", None
    if "mission-contract" in lowered or "mission_contract" in lowered:
        return "lite_spec_kit_mission_contract_path", ref
    if _is_spec_path(ref):
        return "full_spec_kit_artifact_path", ref
    return None, ref


def validate_spec_reference_preflight(
    body: str | None,
    governance: GovernanceResult,
) -> SpecReferencePreflight:
    """Validate dispatch-time ``spec_reference`` for a governed Kanban card.

    This helper is intentionally limited to preflight. It accepts references
    encoded in task body metadata/frontmatter and does not implement completion
    gates.
    """

    if not governance.ok:
        diagnostic = (
            "Spec Governance dispatch preflight failed: governance sidecar "
            f"is invalid at {governance.path}; errors: {', '.join(governance.errors)}"
        )
        return SpecReferencePreflight(
            ok=False,
            reason="governance_invalid",
            diagnostic=diagnostic,
            governance_path=governance.path,
        )

    data = governance.data or {}
    if not data.get("spec_governed") or not data.get("spec_reference_required"):
        return SpecReferencePreflight(ok=True, governance_path=governance.path)

    allowed_forms = set(data.get("allowed_reference_forms") or [])
    spec_reference = extract_spec_reference(body)
    if spec_reference is None:
        diagnostic = (
            "Spec Governance dispatch preflight failed: missing spec_reference "
            f"for governed board; governance={governance.path}; "
            f"canonical_spec={data.get('canonical_spec')}"
        )
        return SpecReferencePreflight(
            ok=False,
            reason="spec_reference_missing",
            diagnostic=diagnostic,
            governance_path=governance.path,
        )

    form, path = _classify_reference(spec_reference)
    if form is None or form not in allowed_forms:
        diagnostic = (
            "Spec Governance dispatch preflight failed: invalid spec_reference "
            f"{spec_reference!r}; allowed_reference_forms={sorted(allowed_forms)}; "
            f"governance={governance.path}"
        )
        return SpecReferencePreflight(
            ok=False,
            reason="spec_reference_invalid",
            diagnostic=diagnostic,
            spec_reference=spec_reference,
            reference_form=form,
            governance_path=governance.path,
        )

    if form != "legacy_migration_exception" and (not path or not _is_spec_path(path)):
        diagnostic = (
            "Spec Governance dispatch preflight failed: invalid spec_reference "
            f"path {path!r}; governance={governance.path}"
        )
        return SpecReferencePreflight(
            ok=False,
            reason="spec_reference_invalid",
            diagnostic=diagnostic,
            spec_reference=spec_reference,
            reference_form=form,
            governance_path=governance.path,
        )

    return SpecReferencePreflight(
        ok=True,
        spec_reference=spec_reference,
        reference_form=form,
        governance_path=governance.path,
    )


def _leading_yaml_payload(body: str) -> str | None:
    lines = body.splitlines()
    collected: list[str] = []
    for line in lines:
        if not line.strip():
            break
        if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*:", line) or line.startswith(("  - ", "    - ", "  ", "    ")):
            collected.append(line)
            continue
        break
    return "\n".join(collected) if collected else None


def _parse_mapping_payload(payload: str | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        parsed = yaml.safe_load(payload)
    except yaml.YAMLError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def extract_lane_registry_contract(body: str | None) -> dict[str, Any]:
    """Extract lane registry contract metadata from task body/frontmatter.

    Supports YAML frontmatter, a leading YAML metadata block, or whole-body YAML.
    This is intentionally read-only and does not mutate Kanban tasks.
    """

    if not body:
        return {}
    contract: dict[str, Any] = {}
    for payload in (_frontmatter_payload(body), _leading_yaml_payload(body), body):
        contract.update(_parse_mapping_payload(payload))
    return {key: value for key, value in contract.items() if key in _LANE_REGISTRY_CONTRACT_KEYS}


_LANE_REGISTRY_CONTRACT_KEYS = set(LANE_REGISTRY_REQUIRED_CONTRACT_FIELDS) | set(
    LANE_REGISTRY_REQUIRED_ONE_OF
) | {"live_target", "auto_spawn_allowed"}


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _list_of_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _workspace_value_is_valid(value: Any) -> bool:
    if not _is_non_empty_string(value):
        return False
    text = value.strip()
    return bool(
        text.startswith(("dir:/", "worktree:/", "obsidian:/", "external:"))
        or text.startswith("/")
    )


def validate_lane_registry_contract(
    body: str | None,
    governance: GovernanceResult,
) -> LaneRegistryValidation:
    """Validate a task body against lane-invocation-registry/v0.1 metadata.

    Slice 1 is validator-only: this helper reports diagnostics but is not wired
    into Kanban claim/dispatch blocking.
    """

    if not governance.ok:
        return LaneRegistryValidation(
            ok=False,
            reason="governance_invalid",
            errors=list(governance.errors),
            governance_path=governance.path,
        )

    lane_registry = governance.data.get("lane_registry") or {}
    if not isinstance(lane_registry, dict) or not lane_registry.get("enabled"):
        return LaneRegistryValidation(ok=True, governance_path=governance.path)

    mode = _normalize_lane_registry_mode(
        lane_registry["mode"] if "mode" in lane_registry else "warn"
    )
    schema = lane_registry.get("schema")
    contract = extract_lane_registry_contract(body)
    errors: list[str] = []
    warnings: list[str] = []

    if not any(_is_non_empty_string(contract.get(field)) for field in LANE_REGISTRY_REQUIRED_ONE_OF):
        errors.append("missing required lane registry field: executor_lane or lane_id")
    for field_name in LANE_REGISTRY_REQUIRED_CONTRACT_FIELDS:
        if field_name not in contract:
            errors.append(f"missing required lane registry field: {field_name}")

    lane = contract.get("executor_lane") or contract.get("lane_id")
    if lane is not None and lane not in ALLOWED_EXECUTOR_LANES:
        errors.append(
            "invalid lane registry field executor_lane/lane_id: "
            f"{lane!r}; allowed={sorted(ALLOWED_EXECUTOR_LANES)}"
        )
    harness = contract.get("harness")
    if harness is not None and harness not in ALLOWED_HARNESSES:
        errors.append(f"invalid lane registry field harness: {harness!r}; allowed={sorted(ALLOWED_HARNESSES)}")
    workspace = contract.get("workspace")
    if workspace is not None and not _workspace_value_is_valid(workspace):
        errors.append("invalid lane registry field workspace: expected dir:/, worktree:/, obsidian:/, external:, or absolute path")
    authority = contract.get("authority_scope")
    if authority is not None and authority not in ALLOWED_AUTHORITY_SCOPES:
        errors.append(f"invalid lane registry field authority_scope: {authority!r}; allowed={sorted(ALLOWED_AUTHORITY_SCOPES)}")
    side_effect = contract.get("side_effect_authority")
    if side_effect is not None and side_effect not in ALLOWED_SIDE_EFFECT_AUTHORITIES:
        errors.append(
            "invalid lane registry field side_effect_authority: "
            f"{side_effect!r}; allowed={sorted(ALLOWED_SIDE_EFFECT_AUTHORITIES)}"
        )
    expectations = contract.get("evidence_expectations")
    if expectations is not None and not _list_of_strings(expectations):
        errors.append("evidence_expectations must be a non-empty list of strings")
    input_refs = contract.get("input_refs")
    if input_refs is not None and not _list_of_strings(input_refs):
        errors.append("input_refs must be a list of strings")
    fallback = contract.get("fallback_policy")
    if fallback is not None and fallback not in ALLOWED_FALLBACK_POLICIES:
        errors.append(f"invalid lane registry field fallback_policy: {fallback!r}; allowed={sorted(ALLOWED_FALLBACK_POLICIES)}")
    verifier = contract.get("verification_actor")
    if verifier is not None and verifier not in ALLOWED_VERIFICATION_ACTORS:
        errors.append(
            "invalid lane registry field verification_actor: "
            f"{verifier!r}; allowed={sorted(ALLOWED_VERIFICATION_ACTORS)}"
        )

    if schema != LANE_REGISTRY_SCHEMA:
        errors.append(f"lane_registry schema mismatch: expected {LANE_REGISTRY_SCHEMA}, got {schema!r}")
    if mode not in LANE_REGISTRY_MODES:
        errors.append(f"lane_registry mode invalid: {mode!r}")

    return LaneRegistryValidation(
        ok=not errors,
        reason=None if not errors else "lane_registry_contract_invalid",
        contract=contract,
        errors=errors,
        warnings=warnings,
        mode=mode,
        schema=schema,
        governance_path=governance.path,
    )


def status_payload(result: GovernanceResult) -> dict[str, Any]:
    """JSON-serializable status shape for CLI/status page integrations."""

    return {
        "path": str(result.path),
        "ok": result.ok,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "spec_governed": result.data.get("spec_governed"),
        "spec_reference_required": result.data.get("spec_reference_required"),
        "spec_mode": result.data.get("spec_mode"),
        "canonical_spec": result.data.get("canonical_spec"),
        "allowed_reference_forms": result.data.get("allowed_reference_forms") or [],
        "override_authority": result.data.get("override_authority"),
        "high_risk_requires_verifier": result.data.get("high_risk_requires_verifier"),
        "extensions": result.data.get("extensions") or {},
        "lane_registry": result.data.get("lane_registry") or {},
    }
