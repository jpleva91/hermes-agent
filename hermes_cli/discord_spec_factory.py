"""Local steel-thread helpers for the Discord → Spec Kit factory.

This module is intentionally side-effect narrow: it validates configuration,
builds source/context packets from synthetic Discord-like mappings, and persists
only repository-local Spec Kit projection files requested by callers. It never
contacts live Discord, GitHub, Kanban, the gateway, credentials, or a live
Hermes home.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

PLACEHOLDER_CHANNEL_ID = "DISCORD_SPEC_FACTORY_INTAKE_CHANNEL_ID"
REQUIRED_FACTORY_ROLES = (
    "intake-triage",
    "spec-lead",
    "architect",
    "implementer",
    "reviewer",
    "release-captain",
    "operator-notifier",
)
_ALLOWED_SIDEEFFECTS = {"none", "local-files", "local-git", "remote-github"}
_ALLOWED_KANBAN_MODES = {"disabled", "notify-only", "mirror-cards"}

_WORKFLOW_ID_RE = re.compile(r"[^a-z0-9]+")
_REPO_MARKERS = (".git", ".specify", "pyproject.toml")
_LIVE_HERMES_HOME = (Path.home() / ".hermes").resolve(strict=False)
_APPROVAL_REQUIRED_SIDEEFFECTS = {"local-git", "remote-github"}


@dataclass(frozen=True)
class ValidationResult:
    """Small result object for fail-closed contract validators."""

    ok: bool
    reason: str = ""


class SpecKitContractSeedRuntime(Protocol):
    """Narrow adapter for loading the repository-local Spec Kit workflow contract.

    This is deliberately not an official Spec Kit execution runtime. It reads
    the repository-local workflow contract and seeds Hermes-owned state only.
    """

    def start_contract_seed(
        self,
        *,
        workflow_path: Path,
        repository_path: Path,
        workflow_id: str,
        inputs: Mapping[str, Any],
        authority_packet: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


def _as_nonempty_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slug(text: str, *, limit: int = 48) -> str:
    normalized = _WORKFLOW_ID_RE.sub("-", text.lower()).strip("-")
    if not normalized:
        return "workflow"
    return normalized[:limit].strip("-") or "workflow"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _ensure_repository_root(path: Path, *, allow_hermes_home: bool = False) -> Path:
    try:
        root = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("repository.path must exist") from exc
    if not root.is_dir():
        raise ValueError("repository.path must be a directory")
    if not allow_hermes_home and _is_relative_to(root, _LIVE_HERMES_HOME):
        raise ValueError("repository.path must not be inside the live Hermes home")
    if not any((root / marker).exists() for marker in _REPO_MARKERS):
        raise ValueError("repository.path must contain a repository marker")
    return root


def _safe_repo_path(root: Path, relative: str | Path) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"derived path escapes repository: {relative}")
    candidate = root / relative_path
    probe = candidate
    while not probe.exists() and probe != root:
        probe = probe.parent
    resolved_probe = probe.resolve(strict=True)
    if not _is_relative_to(resolved_probe, root):
        raise ValueError(f"derived path escapes repository: {relative}")
    if candidate.exists():
        resolved = candidate.resolve(strict=True)
        if not _is_relative_to(resolved, root):
            raise ValueError(f"derived path escapes repository: {relative}")
    return candidate


def _safe_repo_dir(root: Path, relative: str | Path) -> Path:
    directory = _safe_repo_path(root, relative)
    if directory.exists():
        resolved = directory.resolve(strict=True)
        if not _is_relative_to(resolved, root):
            raise ValueError(f"derived path escapes repository: {relative}")
        if not resolved.is_dir():
            raise ValueError(f"derived path is not a directory: {relative}")
        return directory
    parent = _safe_repo_path(root, Path(relative).parent)
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    if not _is_relative_to(parent.resolve(strict=True), root):
        raise ValueError(f"derived path escapes repository: {relative}")
    directory.mkdir(parents=True, exist_ok=True)
    resolved = directory.resolve(strict=True)
    if not _is_relative_to(resolved, root):
        raise ValueError(f"derived path escapes repository: {relative}")
    return directory


def _safe_write_text(root: Path, relative: str | Path, text: str) -> Path:
    path = _safe_repo_path(root, relative)
    _safe_repo_dir(root, path.parent.relative_to(root))
    if path.exists() and not path.resolve(strict=True).is_file():
        raise ValueError(f"derived path is not a file: {relative}")
    path.write_text(text, encoding="utf-8")
    return path


def _safe_write_json(root: Path, relative: str | Path, data: Mapping[str, Any]) -> Path:
    return _safe_write_text(root, relative, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object expected at {path}")
    return data


def _repo_path_from_packet(packet: Mapping[str, Any]) -> Path:
    repository = packet.get("repository") if isinstance(packet, Mapping) else None
    if not isinstance(repository, Mapping):
        raise ValueError("packet.repository is required")
    repo_path = _as_nonempty_str(repository.get("path"))
    if not repo_path:
        raise ValueError("packet.repository.path is required")
    return _ensure_repository_root(Path(repo_path), allow_hermes_home=repository.get("allow_hermes_home") is True)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _official_workflow_path(repo: Path) -> Path:
    path = repo / ".specify" / "workflows" / "speckit" / "workflow.yml"
    if not path.is_file():
        raise ValueError("repository-local .specify/workflows/speckit/workflow.yml is required")
    resolved = path.resolve(strict=True)
    if not _is_relative_to(resolved, repo):
        raise ValueError("Spec Kit workflow path escapes repository")
    return resolved


def load_thread_registry(repository_path: str | Path) -> dict[str, Any]:
    repo = _ensure_repository_root(Path(repository_path))
    registry_path = _safe_repo_path(repo, Path(".hermes") / "discord-spec-factory" / "thread-registry.json")
    if not registry_path.exists():
        return {"schema_version": 1, "threads": {}, "workflows": {}}
    return _read_json(registry_path)


def _workflow_state_path(repo: Path, workflow_state: Mapping[str, Any]) -> Path:
    artifact_paths = workflow_state.get("artifact_paths") if isinstance(workflow_state.get("artifact_paths"), Mapping) else {}
    state_relative = _as_nonempty_str(artifact_paths.get("workflow_state"))
    if not state_relative:
        feature_directory = _as_nonempty_str(workflow_state.get("feature_directory"))
        if not feature_directory:
            raise ValueError("workflow_state.feature_directory is required")
        state_relative = str(Path(feature_directory) / "workflow-state.json")
    return _safe_repo_path(repo, state_relative)


def persist_workflow_state(repository_path: str | Path, workflow_state: Mapping[str, Any]) -> dict[str, Any]:
    repo = _ensure_repository_root(Path(repository_path))
    state_path = _workflow_state_path(repo, workflow_state)
    relative = _relative_posix(state_path, repo)
    state = copy.deepcopy(dict(workflow_state))
    _safe_write_json(repo, relative, state)
    return state


def load_workflow_state_for_thread(repository_path: str | Path, thread_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    repo = _ensure_repository_root(Path(repository_path))
    registry = load_thread_registry(repo)
    entry = (registry.get("threads") or {}).get(_as_nonempty_str(thread_id))
    if not isinstance(entry, Mapping):
        raise ValueError("thread is not registered to a workflow")
    feature_directory = _as_nonempty_str(entry.get("feature_directory"))
    if not feature_directory:
        raise ValueError("thread registry entry is missing feature_directory")
    state_path = _safe_repo_path(repo, Path(feature_directory) / "workflow-state.json")
    if not state_path.exists():
        raise ValueError("registered workflow state does not exist")
    return registry, _read_json(state_path)


class LocalSpecKitContractSeedRuntime:
    """Safe local adapter for the repository-local Spec Kit workflow contract.

    The adapter loads `.specify/workflows/speckit/workflow.yml` as the workflow
    contract and initializes durable state/gates. It does not execute an
    official Spec Kit engine, shell out, or touch networks.
    """

    def start_contract_seed(
        self,
        *,
        workflow_path: Path,
        repository_path: Path,
        workflow_id: str,
        inputs: Mapping[str, Any],
        authority_packet: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        import yaml

        workflow_doc = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
        workflow_meta = workflow_doc.get("workflow") if isinstance(workflow_doc, Mapping) else None
        if not isinstance(workflow_meta, Mapping) or _as_nonempty_str(workflow_meta.get("id")) != "speckit":
            raise ValueError("official Spec Kit workflow.yml must declare workflow.id=speckit")
        steps = workflow_doc.get("steps") if isinstance(workflow_doc, Mapping) else None
        if not isinstance(steps, list) or not steps:
            raise ValueError("official Spec Kit workflow.yml must declare workflow steps")

        title = _as_nonempty_str(inputs.get("spec")) or workflow_id
        feature_directory = f"specs/{workflow_id}-{_slug(title, limit=40)}"
        gates: list[dict[str, Any]] = []
        phase_status: dict[str, str] = {}
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            step_id = _as_nonempty_str(step.get("id"))
            if not step_id:
                continue
            if step.get("type") == "gate":
                gates.append(
                    {
                        "gate_id": step_id,
                        "workflow_id": workflow_id,
                        "phase": step_id,
                        "status": "pending",
                        "message": _as_nonempty_str(step.get("message")) or None,
                        "options": [str(v) for v in (step.get("options") or [])],
                        "required_role_ids": list(authority_packet.get("allowed_role_ids") or []),
                        "required_actor_ids": list(authority_packet.get("allowed_user_ids") or []),
                    }
                )
            else:
                phase_status[step_id] = "pending"
        if "specify" in phase_status:
            phase_status["specify"] = "active"

        return {
            "schema_version": 1,
            "workflow_id": workflow_id,
            "feature_directory": feature_directory,
            "current_phase": "specify",
            "phase_status": phase_status,
            "source_packet_path": f".hermes/discord-spec-factory/source-packets/{workflow_id}.json",
            "artifact_paths": {
                "spec": f"{feature_directory}/spec.md",
                "workflow_state": f"{feature_directory}/workflow-state.json",
            },
            "gates": gates,
            "events": [
                {
                    "type": "spec_kit_workflow_contract_loaded",
                    "at": _now_iso(),
                    "workflow_path": _relative_posix(workflow_path, repository_path),
                    "runtime": "local-speckit-contract-seed",
                    "official_spec_kit_workflow_executed": False,
                }
            ],
            "discord": {"workflow_thread_id": None},
            "spec_kit": {
                "feature_directory": feature_directory,
                "official_workflow_loaded": True,
                "official_workflow_executed": False,
                "workflow_id": _as_nonempty_str(workflow_meta.get("id")),
                "workflow_version": _as_nonempty_str(workflow_meta.get("version")) or None,
            },
        }


def _profile_exists(profile: str, hermes_home: Path | str | None, existing_profiles: set[str] | None) -> bool:
    if profile == "default":
        return True
    if existing_profiles is not None:
        return profile in existing_profiles
    if hermes_home is None:
        return True
    return (Path(hermes_home) / "profiles" / profile).is_dir()


def validate_intake_binding(binding: Mapping[str, Any]) -> ValidationResult:
    """Validate a Discord factory intake binding using default-deny rules.

    The placeholder channel ID is deliberately invalid for live activation so a
    copied sample config cannot start accepting production requests.
    """

    if not isinstance(binding, Mapping):
        return ValidationResult(False, "binding must be a mapping")
    if binding.get("enabled") is not True:
        return ValidationResult(False, "binding is not explicitly enabled")

    channel_id = _as_nonempty_str(binding.get("channel_id"))
    if not channel_id:
        return ValidationResult(False, "channel_id is required")
    if channel_id in {"*", "all", PLACEHOLDER_CHANNEL_ID}:
        return ValidationResult(False, "channel_id is a placeholder or wildcard")

    allowed_roles = binding.get("allowed_role_ids") or []
    allowed_users = binding.get("allowed_user_ids") or []
    has_roles = isinstance(allowed_roles, list) and any(_as_nonempty_str(v) for v in allowed_roles)
    has_users = isinstance(allowed_users, list) and any(_as_nonempty_str(v) for v in allowed_users)
    if not (has_roles or has_users):
        return ValidationResult(False, "live intake requires at least one allowed role or user")

    return ValidationResult(True)


def _is_authorized_for_binding(message: Mapping[str, Any], binding: Mapping[str, Any]) -> bool:
    allowed_users = {_as_nonempty_str(v) for v in (binding.get("allowed_user_ids") or []) if _as_nonempty_str(v)}
    allowed_roles = {_as_nonempty_str(v) for v in (binding.get("allowed_role_ids") or []) if _as_nonempty_str(v)}
    author_id = _as_nonempty_str(message.get("author_id"))
    author_roles = {_as_nonempty_str(v) for v in (message.get("author_roles") or []) if _as_nonempty_str(v)}
    return (author_id in allowed_users) or bool(author_roles & allowed_roles)


def workflow_id_for_message(channel_id: str, message_id: str) -> str:
    """Return the deterministic workflow id for a Discord source message."""

    seed = f"discord:{channel_id}:{message_id}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"dsf_{digest}"


def workflow_thread_name(workflow_id: str, title: str, *, limit: int = 90) -> str:
    """Return a bounded Discord thread title for a workflow.

    If the Discord title cap cuts through the slug, trim back to the previous
    dash so thread names do not end in partial words.
    """

    short_id = workflow_id.replace("dsf_", "")[:8] or "workflow"
    prefix = f"spec-{short_id}: "
    title_slug = _slug(title, limit=56)
    name = f"{prefix}{title_slug}"
    if len(name) <= limit:
        return name.rstrip("-: ")

    truncated = name[:limit].rstrip("-: ")
    prefix_len = len(prefix)
    last_dash = truncated.rfind("-")
    if last_dash > prefix_len:
        truncated = truncated[:last_dash]
    return truncated.rstrip("-: ")


def _normalize_attachment(raw: Mapping[str, Any]) -> dict[str, Any]:
    content_type = raw.get("content_type", raw.get("mime_type"))
    size = raw.get("size", raw.get("size_bytes"))
    try:
        size_value = int(size) if size is not None else None
    except (TypeError, ValueError):
        size_value = None

    supported = raw.get("supported")
    if supported is None:
        supported = bool(raw.get("cached_path") or str(content_type or "").startswith(("text/", "image/", "audio/", "application/pdf")))

    return {
        "id": _as_nonempty_str(raw.get("id")) or _as_nonempty_str(raw.get("filename")) or "attachment",
        "filename": _as_nonempty_str(raw.get("filename")) or "attachment",
        "content_type": _as_nonempty_str(content_type) or None,
        "size": size_value,
        "cached_path": _as_nonempty_str(raw.get("cached_path")) or None,
        "sha256": _as_nonempty_str(raw.get("sha256")) or None,
        "supported": bool(supported),
        "warning": _as_nonempty_str(raw.get("warning")) or None,
    }


def build_source_context_packet(
    message: Mapping[str, Any],
    *,
    repository: Mapping[str, Any],
    received_at: str | None = None,
) -> dict[str, Any]:
    """Build an immutable source/context packet from a Discord-like message.

    Required message keys: channel_id, message_id, author_id, created_at. Text is
    optional but hashed even when empty. Attachment and voice entries are metadata
    only; this helper never reads files or executes media processors.
    """

    for key in ("channel_id", "message_id", "author_id", "created_at"):
        if not _as_nonempty_str(message.get(key)):
            raise ValueError(f"message.{key} is required")
    if not _as_nonempty_str(repository.get("path")):
        raise ValueError("repository.path is required")
    if not _as_nonempty_str(repository.get("default_branch")):
        raise ValueError("repository.default_branch is required")

    channel_id = _as_nonempty_str(message.get("channel_id"))
    message_id = _as_nonempty_str(message.get("message_id"))
    text = str(message.get("text") or "")
    workflow_id = workflow_id_for_message(channel_id, message_id)
    attachments_raw = message.get("attachments") or []
    voice_raw = message.get("voice") or []

    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "source": {
            "platform": "discord",
            "channel_id": channel_id,
            "parent_channel_id": _as_nonempty_str(message.get("parent_channel_id")) or None,
            "thread_id": _as_nonempty_str(message.get("thread_id")) or None,
            "message_id": message_id,
            "author_id": _as_nonempty_str(message.get("author_id")),
            "author_display": _as_nonempty_str(message.get("author_display")) or None,
            "author_roles": [str(v).strip() for v in (message.get("author_roles") or []) if str(v).strip()],
            "created_at": _as_nonempty_str(message.get("created_at")),
            "received_at": received_at or _now_iso(),
        },
        "content": {
            "text": text,
            "text_sha256": _sha256_text(text),
            "attachments": [
                _normalize_attachment(a) for a in attachments_raw if isinstance(a, Mapping)
            ],
            "voice": [dict(v) for v in voice_raw if isinstance(v, Mapping)],
        },
        "repository": {
            "path": _as_nonempty_str(repository.get("path")),
            "default_branch": _as_nonempty_str(repository.get("default_branch")),
            "remote": _as_nonempty_str(repository.get("remote")) or None,
        },
        "discord": {"workflow_thread_id": None},
        "spec_kit": {"feature_directory": None},
    }


def _packet_identity(packet: Mapping[str, Any]) -> str:
    normalized = copy.deepcopy(dict(packet))
    normalized.pop("discord", None)
    normalized.pop("spec_kit", None)
    source = normalized.get("source")
    if isinstance(source, dict):
        source.pop("received_at", None)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def initialize_local_spec_kit_seed(
    packet: Mapping[str, Any],
    *,
    feature_title: str | None = None,
    initialized_at: str | None = None,
) -> dict[str, Any]:
    """Persist the local, repository-confined Spec Kit seed state.

    This intentionally does not claim to execute the official Spec Kit workflow.
    It creates only a local seed directory, immutable source packet, and helper
    workflow-state file under the target repository.
    """

    workflow_id = _as_nonempty_str(packet.get("workflow_id"))
    if not workflow_id:
        raise ValueError("packet.workflow_id is required")
    repo = _repo_path_from_packet(packet)
    title = feature_title or packet.get("content", {}).get("text") or workflow_id
    feature_directory = f"specs/{workflow_id}-{_slug(str(title), limit=40)}"
    feature_dir = _safe_repo_dir(repo, feature_directory)

    packet_with_links = copy.deepcopy(dict(packet))
    packet_with_links.setdefault("spec_kit", {})["feature_directory"] = feature_directory
    packet_with_links["source_packet_sha256"] = _packet_identity(packet_with_links)
    packet_relative = Path(".hermes") / "discord-spec-factory" / "source-packets" / f"{workflow_id}.json"
    state_relative = Path(feature_directory) / "workflow-state.json"
    spec_relative = Path(feature_directory) / "spec.md"
    packet_path = _safe_repo_path(repo, packet_relative)
    state_path = _safe_repo_path(repo, state_relative)
    spec_path = _safe_repo_path(repo, spec_relative)

    if packet_path.exists():
        existing_packet = _read_json(packet_path)
        existing_feature_directory = _as_nonempty_str((existing_packet.get("spec_kit") or {}).get("feature_directory"))
        if not existing_feature_directory:
            raise ValueError("source packet exists without feature directory link")
        existing_state_relative = Path(existing_feature_directory) / "workflow-state.json"
        existing_state_path = _safe_repo_path(repo, existing_state_relative)
        existing_hash = _as_nonempty_str(existing_packet.get("source_packet_sha256")) or _packet_identity(existing_packet)
        incoming_hash = _as_nonempty_str(packet_with_links.get("source_packet_sha256"))
        if existing_state_path.exists():
            existing_state = _read_json(existing_state_path)
        else:
            raise ValueError("source packet exists without workflow state")
        if existing_hash == incoming_hash:
            return existing_state
        event = {
            "type": "source_packet_conflict_observed",
            "at": initialized_at or _now_iso(),
            "source_packet_path": _relative_posix(packet_path, repo),
            "existing_source_packet_sha256": existing_hash,
            "incoming_source_packet_sha256": incoming_hash,
            "incoming_text_sha256": _as_nonempty_str((packet.get("content") or {}).get("text_sha256")) or None,
        }
        if event not in existing_state.setdefault("events", []):
            existing_state["events"].append(event)
            _safe_write_json(repo, existing_state_relative, existing_state)
        return existing_state

    if not spec_path.exists():
        _safe_write_text(
            repo,
            spec_relative,
            f"# {title}\n\nSource workflow: `{workflow_id}`\n\nStatus: local Spec Kit seed initialized; official workflow execution is not wired yet.\n",
        )

    _safe_write_json(repo, packet_relative, packet_with_links)

    state = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "feature_directory": _relative_posix(feature_dir, repo),
        "current_phase": "specify",
        "phase_status": {"created": "completed", "specify": "active"},
        "source_packet_path": _relative_posix(packet_path, repo),
        "source_packet_sha256": packet_with_links["source_packet_sha256"],
        "artifact_paths": {
            "spec": _relative_posix(spec_path, repo),
            "workflow_state": _relative_posix(state_path, repo),
        },
        "gates": [],
        "events": [
            {
                "type": "local_spec_kit_seed_initialized",
                "at": initialized_at or _now_iso(),
                "source_packet_path": _relative_posix(packet_path, repo),
                "adapter": "local-spec-kit-seed",
                "official_spec_kit_workflow_executed": False,
            }
        ],
        "discord": copy.deepcopy(packet_with_links.get("discord", {})),
        "spec_kit": {"feature_directory": feature_directory, "official_workflow_executed": False},
    }
    _safe_write_json(repo, state_relative, state)
    return state


def start_spec_kit_contract_seed(
    packet: Mapping[str, Any],
    *,
    runtime: SpecKitContractSeedRuntime | None = None,
    authority: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load the repository-local Spec Kit workflow contract and persist seed state.

    This does not execute the official Spec Kit runtime. It only records that
    the workflow contract was found and used to initialize Hermes-local state.
    Exact duplicate source packets resume existing state without re-seeding.
    """

    workflow_id = _as_nonempty_str(packet.get("workflow_id"))
    if not workflow_id:
        raise ValueError("packet.workflow_id is required")
    repo = _repo_path_from_packet(packet)
    workflow_path = _official_workflow_path(repo)
    authority_packet = copy.deepcopy(dict(authority or {}))
    content = packet.get("content") if isinstance(packet.get("content"), Mapping) else {}
    packet_relative = Path(".hermes") / "discord-spec-factory" / "source-packets" / f"{workflow_id}.json"
    packet_path = _safe_repo_path(repo, packet_relative)
    if packet_path.exists():
        existing_packet = _read_json(packet_path)
        existing_feature_directory = _as_nonempty_str((existing_packet.get("spec_kit") or {}).get("feature_directory"))
        existing_hash = _as_nonempty_str(existing_packet.get("source_packet_sha256")) or _packet_identity(existing_packet)
        incoming_probe = copy.deepcopy(dict(packet))
        if existing_feature_directory:
            incoming_probe.setdefault("spec_kit", {})["feature_directory"] = existing_feature_directory
        incoming_hash = _packet_identity(incoming_probe)
        if existing_feature_directory and existing_hash == incoming_hash:
            existing_state_path = _safe_repo_path(repo, Path(existing_feature_directory) / "workflow-state.json")
            if existing_state_path.exists():
                return _read_json(existing_state_path)

    runtime = runtime or LocalSpecKitContractSeedRuntime()
    state = copy.deepcopy(dict(runtime.start_contract_seed(
        workflow_path=workflow_path,
        repository_path=repo,
        workflow_id=workflow_id,
        inputs={
            "spec": str(content.get("text") or ""),
            "integration": "hermes",
            "scope": "contract-seed",
        },
        authority_packet=authority_packet,
    )))
    if _as_nonempty_str(state.get("workflow_id")) != workflow_id:
        raise ValueError("Spec Kit contract seed returned mismatched workflow_id")
    feature_directory = _as_nonempty_str(state.get("feature_directory"))
    if not feature_directory:
        raise ValueError("Spec Kit contract seed returned no feature_directory")

    packet_with_links = copy.deepcopy(dict(packet))
    packet_with_links.setdefault("spec_kit", {})["feature_directory"] = feature_directory
    packet_with_links["source_packet_sha256"] = _packet_identity(packet_with_links)
    if packet_path.exists():
        existing_packet = _read_json(packet_path)
        existing_hash = _as_nonempty_str(existing_packet.get("source_packet_sha256")) or _packet_identity(existing_packet)
        if existing_hash != packet_with_links["source_packet_sha256"]:
            state.setdefault("events", []).append({
                "type": "source_packet_conflict_observed",
                "at": _now_iso(),
                "source_packet_path": _relative_posix(packet_path, repo),
                "existing_source_packet_sha256": existing_hash,
                "incoming_source_packet_sha256": packet_with_links["source_packet_sha256"],
            })
    else:
        _safe_write_json(repo, packet_relative, packet_with_links)

    spec_path = _as_nonempty_str((state.get("artifact_paths") or {}).get("spec"))
    if spec_path and not _safe_repo_path(repo, spec_path).exists():
        _safe_write_text(
            repo,
            spec_path,
            f"# {str(content.get('text') or workflow_id).strip() or workflow_id}\n\n"
            f"Source workflow: `{workflow_id}`\n\n"
            "Status: Spec Kit workflow contract loaded; official Spec Kit execution is not wired yet.\n",
        )
    state["source_packet_path"] = _relative_posix(packet_path, repo)
    state.setdefault("artifact_paths", {})["workflow_state"] = f"{feature_directory}/workflow-state.json"
    state.setdefault("spec_kit", {})["official_workflow_loaded"] = True
    state.setdefault("spec_kit", {})["official_workflow_executed"] = False
    return persist_workflow_state(repo, state)


def build_thread_open_request(packet: Mapping[str, Any], workflow_state: Mapping[str, Any]) -> dict[str, Any]:
    source = packet.get("source", {}) if isinstance(packet.get("source"), Mapping) else {}
    content = packet.get("content", {}) if isinstance(packet.get("content"), Mapping) else {}
    workflow_id = _as_nonempty_str(packet.get("workflow_id"))
    thread_name = workflow_thread_name(workflow_id, str(content.get("text") or workflow_id))
    gates = workflow_state.get("gates") if isinstance(workflow_state.get("gates"), list) else []
    pending_gate = next((g for g in gates if isinstance(g, Mapping) and g.get("status") == "pending"), None)
    options = [str(v) for v in ((pending_gate or {}).get("options") or []) if str(v).strip()]
    authorized_roles = [str(v) for v in ((pending_gate or {}).get("required_role_ids") or []) if str(v).strip()]
    authorized_actors = [str(v) for v in ((pending_gate or {}).get("required_actor_ids") or []) if str(v).strip()]
    next_action = (
        f"Resolve gate `{pending_gate.get('gate_id')}`" if isinstance(pending_gate, Mapping) else "Review the seeded Spec Kit packet and wait for the next explicit gate."
    )
    actor_line = ", ".join([*(f"role:{r}" for r in authorized_roles), *(f"user:{a}" for a in authorized_actors)]) or "configured maintainers only"
    options_line = ", ".join(options) if options else "clarify gates accept free-text answers; approval gates require /approve or /reject"
    timeout = _as_nonempty_str((pending_gate or {}).get("timeout")) or "no automatic timeout configured"
    return {
        "schema_version": 1,
        "adapter_method": "create_handoff_thread",
        "live_effect": False,
        "workflow_id": workflow_id,
        "parent_channel_id": _as_nonempty_str(source.get("parent_channel_id")) or _as_nonempty_str(source.get("channel_id")),
        "source_message_id": _as_nonempty_str(source.get("message_id")),
        "thread_name": thread_name,
        "initial_thread_brief": "\n".join([
            f"Workflow `{workflow_id}` initialized at `{workflow_state.get('feature_directory')}`.",
            f"Source message `{source.get('message_id')}`. Current phase: `{workflow_state.get('current_phase')}`.",
            f"Required next action: {next_action}.",
            f"Authorized actor/role: {actor_line}.",
            f"Options: {options_line}.",
            f"Timeout: {timeout}.",
        ]),
    }


def process_synthetic_discord_intake(
    message: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    received_at: str | None = None,
) -> dict[str, Any]:
    """Run the local intake steel thread from a synthetic Discord event.

    The return shape contains packets/requests only. `thread_open_request` is a
    request for the existing gateway adapter method; this helper never calls it.
    """

    binding_result = validate_intake_binding(binding)
    if not binding_result.ok:
        return {"ok": False, "reason": binding_result.reason, "live_effects": []}
    if _as_nonempty_str(message.get("channel_id")) != _as_nonempty_str(binding.get("channel_id")):
        return {"ok": False, "reason": "message channel does not match intake binding", "live_effects": []}
    if not _is_authorized_for_binding(message, binding):
        return {"ok": False, "reason": "message author is not authorized for intake binding", "live_effects": []}

    repository = binding.get("repository")
    if not isinstance(repository, Mapping):
        return {"ok": False, "reason": "binding.repository is required", "live_effects": []}

    packet = build_source_context_packet(message, repository=repository, received_at=received_at)
    workflow_state = initialize_local_spec_kit_seed(packet)
    thread_request = build_thread_open_request(packet, workflow_state)
    kanban_projection = build_kanban_projection_packet(workflow_state, binding.get("kanban_projection") or {"enabled": False, "mode": "disabled"})
    return {
        "ok": True,
        "source_packet": packet,
        "workflow_state": workflow_state,
        "thread_open_request": thread_request,
        "kanban_projection": kanban_projection,
        "live_effects": [],
    }


def register_workflow_thread(
    repository_path: str | Path,
    *,
    workflow_state: Mapping[str, Any],
    parent_channel_id: str,
    thread_id: str,
    source_message_id: str,
    thread_name: str,
    registered_at: str | None = None,
) -> dict[str, Any]:
    """Persist the derived Discord thread ↔ workflow mapping under the repo."""

    workflow_id = _as_nonempty_str(workflow_state.get("workflow_id"))
    feature_directory = _as_nonempty_str(workflow_state.get("feature_directory"))
    if not workflow_id:
        raise ValueError("workflow_state.workflow_id is required")
    if not feature_directory:
        raise ValueError("workflow_state.feature_directory is required")
    if not _as_nonempty_str(thread_id):
        raise ValueError("thread_id is required")

    repo = _ensure_repository_root(Path(repository_path))
    registry_relative = Path(".hermes") / "discord-spec-factory" / "thread-registry.json"
    registry_path = _safe_repo_path(repo, registry_relative)
    if registry_path.exists():
        registry = _read_json(registry_path)
    else:
        registry = {"schema_version": 1, "threads": {}, "workflows": {}}

    threads = registry.get("threads")
    workflows = registry.get("workflows")
    if not isinstance(threads, dict) or not isinstance(workflows, dict):
        raise ValueError("thread registry mappings are malformed")

    normalized_thread_id = _as_nonempty_str(thread_id)
    existing_thread = threads.get(normalized_thread_id)
    existing_workflow_thread = workflows.get(workflow_id)
    if existing_thread is not None and not isinstance(existing_thread, Mapping):
        raise ValueError("thread registry entry is malformed")
    if existing_workflow_thread is not None and not isinstance(existing_workflow_thread, str):
        raise ValueError("workflow registry entry is malformed")
    if existing_thread is not None:
        existing_thread_workflow = _as_nonempty_str(existing_thread.get("workflow_id"))
        existing_entry_thread_id = _as_nonempty_str(existing_thread.get("thread_id"))
        if not existing_thread_workflow:
            raise ValueError("thread registry entry is missing workflow_id")
        if not existing_entry_thread_id:
            raise ValueError("thread registry entry is missing thread_id")
        if existing_entry_thread_id != normalized_thread_id:
            raise ValueError("thread registry entry thread_id is inconsistent with its key")
        if existing_thread_workflow != workflow_id:
            raise ValueError("thread_id is already registered to a different workflow")
    if existing_workflow_thread is not None and existing_workflow_thread != normalized_thread_id:
        raise ValueError("workflow_id is already registered to a different thread")
    if existing_thread is not None:
        if existing_workflow_thread != normalized_thread_id:
            raise ValueError("thread registry mappings are inconsistent")
        return dict(existing_thread)

    entry = {
        "workflow_id": workflow_id,
        "platform": "discord",
        "parent_channel_id": _as_nonempty_str(parent_channel_id),
        "thread_id": normalized_thread_id,
        "thread_name": _as_nonempty_str(thread_name),
        "source_message_id": _as_nonempty_str(source_message_id),
        "feature_directory": feature_directory,
        "status": "active",
        "last_notified_at": registered_at or _now_iso(),
    }
    threads[entry["thread_id"]] = entry
    workflows[workflow_id] = entry["thread_id"]
    _safe_write_json(repo, registry_relative, registry)
    return entry


def apply_gate_decision(
    gate: Mapping[str, Any],
    reply: Mapping[str, Any],
    *,
    registry: Mapping[str, Any],
    decision: str,
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Apply an async thread reply to a pending approval/clarify gate."""

    updated_gate = copy.deepcopy(dict(gate))
    workflow_id = _as_nonempty_str(updated_gate.get("workflow_id"))
    thread_id = _as_nonempty_str(reply.get("thread_id"))
    thread_entry = (registry.get("threads") or {}).get(thread_id) if isinstance(registry, Mapping) else None
    if not isinstance(thread_entry, Mapping) or _as_nonempty_str(thread_entry.get("workflow_id")) != workflow_id:
        return {"ok": False, "reason": "reply thread is not registered for workflow", "gate": updated_gate}
    if updated_gate.get("status") != "pending":
        return {"ok": False, "reason": "gate is not pending", "gate": updated_gate}

    required_actors = {_as_nonempty_str(v) for v in (updated_gate.get("required_actor_ids") or []) if _as_nonempty_str(v)}
    required_roles = {_as_nonempty_str(v) for v in (updated_gate.get("required_role_ids") or []) if _as_nonempty_str(v)}
    actor_id = _as_nonempty_str(reply.get("author_id"))
    actor_roles = {_as_nonempty_str(v) for v in (reply.get("author_roles") or []) if _as_nonempty_str(v)}
    if not required_actors and not required_roles:
        updated_gate.setdefault("events", []).append({
            "type": "gate_decision_denied",
            "actor_id": actor_id,
            "message_id": _as_nonempty_str(reply.get("message_id")),
            "at": decided_at or _now_iso(),
            "reason": "missing authorization constraint",
        })
        return {"ok": False, "reason": "gate has no authorization constraint", "gate": updated_gate}
    authorized = actor_id in required_actors or bool(actor_roles & required_roles)
    if not authorized:
        updated_gate.setdefault("events", []).append({
            "type": "gate_decision_denied",
            "actor_id": actor_id,
            "message_id": _as_nonempty_str(reply.get("message_id")),
            "at": decided_at or _now_iso(),
        })
        return {"ok": False, "reason": "actor is not authorized for gate", "gate": updated_gate}

    normalized_decision = _as_nonempty_str(decision).lower()
    if normalized_decision not in {"approved", "rejected", "answered"}:
        return {"ok": False, "reason": "decision must be approved, rejected, or answered", "gate": updated_gate}
    gate_kind = (_as_nonempty_str(updated_gate.get("type")) or _as_nonempty_str(updated_gate.get("phase")) or _as_nonempty_str(updated_gate.get("gate_id"))).lower()
    is_clarify_gate = "clarify" in gate_kind or "question" in gate_kind
    if normalized_decision == "answered" and not is_clarify_gate:
        updated_gate.setdefault("events", []).append({
            "type": "gate_decision_denied",
            "actor_id": actor_id,
            "message_id": _as_nonempty_str(reply.get("message_id")),
            "at": decided_at or _now_iso(),
            "reason": "approval gate requires explicit approval option",
        })
        return {"ok": False, "reason": "approval gate requires explicit approval option", "gate": updated_gate}
    if normalized_decision in {"approved", "rejected"} and is_clarify_gate:
        return {"ok": False, "reason": "clarify gate requires an answer", "gate": updated_gate}
    updated_gate["status"] = normalized_decision
    updated_gate["decision"] = {
        "value": normalized_decision,
        "actor_id": actor_id,
        "discord_message_id": _as_nonempty_str(reply.get("message_id")),
        "thread_id": thread_id,
        "timestamp": decided_at or _now_iso(),
    }
    if normalized_decision == "answered":
        updated_gate["decision"]["answer"] = str(reply.get("text") or "")
    return {"ok": True, "gate": updated_gate}


def apply_thread_reply_to_workflow_state(
    repository_path: str | Path,
    reply: Mapping[str, Any],
    *,
    decision: str,
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Persist a registered Discord workflow-thread reply into pending gates."""

    repo = _ensure_repository_root(Path(repository_path))
    thread_id = _as_nonempty_str(reply.get("thread_id"))
    if not thread_id:
        return {"ok": False, "reason": "reply.thread_id is required"}
    registry, state = load_workflow_state_for_thread(repo, thread_id)
    gates = state.get("gates")
    if not isinstance(gates, list):
        return {"ok": False, "reason": "workflow state gates are malformed", "workflow_state": state}
    for idx, gate in enumerate(gates):
        if not isinstance(gate, Mapping) or gate.get("status") != "pending":
            continue
        result = apply_gate_decision(gate, reply, registry=registry, decision=decision, decided_at=decided_at)
        if result.get("ok"):
            state["gates"][idx] = result["gate"]
            state.setdefault("events", []).append({
                "type": "gate_decision_applied",
                "at": decided_at or _now_iso(),
                "gate_id": result["gate"].get("gate_id"),
                "decision": result["gate"].get("decision"),
            })
            return {"ok": True, "workflow_state": persist_workflow_state(repo, state), "gate": result["gate"]}
        if result.get("reason") in {"actor is not authorized for gate", "gate has no authorization constraint"}:
            state["gates"][idx] = result.get("gate", gate)
            persist_workflow_state(repo, state)
            return {"ok": False, "reason": result.get("reason"), "workflow_state": state}
    return {"ok": False, "reason": "no pending gate for registered workflow thread", "workflow_state": state}


def validate_role_routes(
    routes: Mapping[str, Any],
    *,
    hermes_home: str | Path | None = None,
    require_profiles_exist: bool = False,
    existing_profiles: set[str] | None = None,
) -> ValidationResult:
    """Validate required factory role routes before any worker dispatch."""

    if not isinstance(routes, Mapping):
        return ValidationResult(False, "routes must be a mapping")

    for role in REQUIRED_FACTORY_ROLES:
        route = routes.get(role)
        if not isinstance(route, Mapping):
            return ValidationResult(False, f"missing route for {role}")
        profile = _as_nonempty_str(route.get("profile"))
        model = _as_nonempty_str(route.get("model"))
        toolsets = route.get("toolsets")
        if not profile:
            return ValidationResult(False, f"route {role} missing profile")
        if not model:
            return ValidationResult(False, f"route {role} missing model")
        if not isinstance(toolsets, list):
            return ValidationResult(False, f"route {role} toolsets must be a list")
        side_effects = _as_nonempty_str(route.get("side_effects")) or "none"
        if side_effects not in _ALLOWED_SIDEEFFECTS:
            return ValidationResult(False, f"route {role} side_effects is invalid")
        if require_profiles_exist and not _profile_exists(profile, hermes_home, existing_profiles):
            return ValidationResult(False, f"route {role} profile does not exist: {profile}")

    return ValidationResult(True)


def build_execution_packet(
    workflow_state: Mapping[str, Any],
    *,
    routes: Mapping[str, Any],
    role: str,
    hermes_home: str | Path | None = None,
    require_profile_exists: bool = False,
    approval_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a local dispatch packet for a Hermes role/profile worker."""

    if role not in REQUIRED_FACTORY_ROLES:
        raise ValueError(f"unknown factory role: {role}")
    route_result = validate_role_routes(routes, hermes_home=hermes_home, require_profiles_exist=require_profile_exists)
    if not route_result.ok:
        raise ValueError(route_result.reason)
    route = routes[role]
    side_effects = _as_nonempty_str(route.get("side_effects")) or "none"
    approval_required = route.get("approval_required") is True or side_effects in _APPROVAL_REQUIRED_SIDEEFFECTS
    if approval_required and (not approval_gate or approval_gate.get("status") != "approved"):
        raise ValueError(f"route {role} requires an approved gate")

    return {
        "schema_version": 1,
        "workflow_id": _as_nonempty_str(workflow_state.get("workflow_id")),
        "role": role,
        "profile": _as_nonempty_str(route.get("profile")),
        "model": _as_nonempty_str(route.get("model")),
        "toolsets": list(route.get("toolsets") or []),
        "workspace": _as_nonempty_str(route.get("workspace")) or "scratch",
        "side_effects": side_effects,
        "max_turns": route.get("max_turns"),
        "budget_usd": route.get("budget_usd"),
        "approval_gate_id": _as_nonempty_str((approval_gate or {}).get("gate_id")) or None,
        "feature_directory": _as_nonempty_str(workflow_state.get("feature_directory")),
        "source_packet_path": _as_nonempty_str(workflow_state.get("source_packet_path")),
        "artifact_paths": copy.deepcopy(workflow_state.get("artifact_paths") or {}),
        "live_effect": False,
    }


def build_kanban_projection_packet(workflow_state: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Build an optional derived Kanban projection packet without mutating Kanban."""

    enabled = bool(config.get("enabled"))
    mode = _as_nonempty_str(config.get("mode")) or ("notify-only" if enabled else "disabled")
    if mode not in _ALLOWED_KANBAN_MODES:
        raise ValueError(f"invalid Kanban projection mode: {mode}")
    base = {
        "schema_version": 1,
        "workflow_id": _as_nonempty_str(workflow_state.get("workflow_id")),
        "enabled": enabled and mode != "disabled",
        "mode": mode,
        "canonical_source": "spec_kit",
        "board": _as_nonempty_str(config.get("board")) or None,
        "tenant": _as_nonempty_str(config.get("tenant")) or None,
        "last_sync_status": "not_requested" if not enabled or mode == "disabled" else "pending",
        "card_requests": [],
    }
    if not base["enabled"]:
        return base
    phase = _as_nonempty_str(workflow_state.get("current_phase")) or "workflow"
    title = f"[{base['workflow_id']}] {phase}: {_slug(workflow_state.get('feature_directory') or base['workflow_id'], limit=40)}"
    base["card_requests"].append({
        "title": title,
        "body": "\n".join([
            f"canonical feature directory: {workflow_state.get('feature_directory')}",
            f"Discord workflow thread id/link: {(workflow_state.get('discord') or {}).get('workflow_thread_id')}",
            f"source packet path: {workflow_state.get('source_packet_path')}",
            f"current phase: {phase}",
        ]),
        "phase": phase,
        "live_effect": False,
    })
    return base


def validate_pr_target(target: Mapping[str, Any]) -> ValidationResult:
    """Validate the local PR handoff target contract without GitHub credentials."""

    if not isinstance(target, Mapping):
        return ValidationResult(False, "PR target must be a mapping")
    required = ("repository", "base_branch", "feature_branch", "title", "test_evidence", "rollback_plan", "approval_gate_id")
    for key in required:
        if key == "test_evidence":
            if not isinstance(target.get(key), list) or not target.get(key):
                return ValidationResult(False, "test_evidence is required")
        elif not _as_nonempty_str(target.get(key)):
            return ValidationResult(False, f"{key} is required")
    return ValidationResult(True)


def build_pr_handoff_packet(workflow_state: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    """Build a validated GitHub handoff packet with remote effects disabled."""

    result = validate_pr_target(target)
    if not result.ok:
        raise ValueError(result.reason)
    workflow_id = _as_nonempty_str(workflow_state.get("workflow_id"))
    pr_body = "\n\n".join([
        "## Summary\nPrepared by Discord Spec Factory local handoff packet.",
        f"## Source\n- Workflow: `{workflow_id}`\n- Source packet: `{workflow_state.get('source_packet_path')}`\n- Discord thread: `{(workflow_state.get('discord') or {}).get('workflow_thread_id')}`",
        f"## Spec Kit Artifacts\n- Feature directory: `{workflow_state.get('feature_directory')}`",
        f"## Discord Approval Evidence\n- Gate: `{target.get('approval_gate_id')}`",
        "## Test Evidence\n" + "\n".join(f"- `{item}`" for item in target.get("test_evidence", [])),
        f"## Rollback Plan\n{target.get('rollback_plan')}",
    ])
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "repository": _as_nonempty_str(target.get("repository")),
        "base_branch": _as_nonempty_str(target.get("base_branch")),
        "feature_branch": _as_nonempty_str(target.get("feature_branch")),
        "pr_title": _as_nonempty_str(target.get("title")),
        "pr_body": pr_body,
        "test_evidence": list(target.get("test_evidence") or []),
        "approval_gate_id": _as_nonempty_str(target.get("approval_gate_id")),
        "rollback_plan": _as_nonempty_str(target.get("rollback_plan")),
        "remote_effects_enabled": False,
    }
