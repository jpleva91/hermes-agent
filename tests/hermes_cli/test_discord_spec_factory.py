from __future__ import annotations

import json

import pytest

from hermes_cli.discord_spec_factory import (
    PLACEHOLDER_CHANNEL_ID,
    apply_gate_decision,
    build_execution_packet,
    build_kanban_projection_packet,
    build_pr_handoff_packet,
    build_source_context_packet,
    initialize_local_spec_kit_seed,
    process_synthetic_discord_intake,
    register_workflow_thread,
    validate_intake_binding,
    validate_pr_target,
    validate_role_routes,
    workflow_id_for_message,
    workflow_thread_name,
)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _complete_routes():
    return {
        "intake-triage": {"profile": "default", "model": "fast", "toolsets": ["file"]},
        "spec-lead": {"profile": "spec-lead", "model": "reasoning", "toolsets": ["file", "terminal"]},
        "architect": {"profile": "architect", "model": "reasoning", "toolsets": ["file"]},
        "implementer": {"profile": "implementer", "model": "code", "toolsets": ["file", "terminal"]},
        "reviewer": {"profile": "reviewer", "model": "reasoning", "toolsets": ["file", "terminal"]},
        "release-captain": {"profile": "release", "model": "balanced", "toolsets": ["file", "terminal"]},
        "operator-notifier": {"profile": "default", "model": "fast", "toolsets": []},
    }


def _message():
    return {
        "channel_id": "123456789012345678",
        "message_id": "333333333333333333",
        "author_id": "444444444444444444",
        "author_display": "Maintainer",
        "author_roles": ["555555555555555555"],
        "created_at": "2026-07-10T00:00:00Z",
        "text": "Build the Discord Spec Factory",
    }


def _binding(repo_path):
    return {
        "enabled": True,
        "channel_id": "123456789012345678",
        "allowed_role_ids": ["555555555555555555"],
        "repository": {"path": str(repo_path), "default_branch": "main", "remote": "nous/hermes-agent"},
        "kanban_projection": {"enabled": False, "mode": "disabled"},
    }


def test_intake_binding_placeholder_channel_fails_closed():
    result = validate_intake_binding({
        "enabled": True,
        "channel_id": PLACEHOLDER_CHANNEL_ID,
        "allowed_role_ids": ["role-maintainers"],
    })

    assert result.ok is False
    assert "placeholder" in result.reason


def test_intake_binding_requires_authorization_constraint():
    result = validate_intake_binding({
        "enabled": True,
        "channel_id": "123456789012345678",
    })

    assert result.ok is False
    assert "allowed role or user" in result.reason


def test_intake_binding_accepts_concrete_enabled_channel_with_role():
    result = validate_intake_binding({
        "enabled": True,
        "channel_id": "123456789012345678",
        "allowed_role_ids": ["987654321098765432"],
    })

    assert result.ok is True


def test_workflow_id_is_deterministic_for_discord_message():
    a = workflow_id_for_message("chan", "msg")
    b = workflow_id_for_message("chan", "msg")
    c = workflow_id_for_message("chan", "other")

    assert a == b
    assert a.startswith("dsf_")
    assert a != c


def test_source_context_packet_captures_required_provenance_and_hashes_text():
    packet = build_source_context_packet(
        {
            "channel_id": "123456789012345678",
            "parent_channel_id": "111111111111111111",
            "thread_id": "222222222222222222",
            "message_id": "333333333333333333",
            "author_id": "444444444444444444",
            "author_display": "Maintainer",
            "author_roles": ["555555555555555555", ""],
            "created_at": "2026-07-10T00:00:00Z",
            "text": "Build the Discord Spec Factory",
            "attachments": [
                {
                    "id": "att-1",
                    "filename": "requirements.md",
                    "content_type": "text/markdown",
                    "size": "128",
                    "cached_path": "/tmp/requirements.md",
                }
            ],
            "voice": [{"attachment_id": "voice-1", "transcript_status": "unavailable"}],
        },
        repository={"path": "/tmp/repo", "default_branch": "main"},
        received_at="2026-07-10T00:00:01Z",
    )

    assert packet["schema_version"] == 1
    assert packet["workflow_id"] == workflow_id_for_message("123456789012345678", "333333333333333333")
    assert packet["source"]["platform"] == "discord"
    assert packet["source"]["parent_channel_id"] == "111111111111111111"
    assert packet["source"]["thread_id"] == "222222222222222222"
    assert packet["source"]["author_roles"] == ["555555555555555555"]
    assert len(packet["content"]["text_sha256"]) == 64
    assert packet["content"]["attachments"][0]["supported"] is True
    assert packet["content"]["attachments"][0]["size"] == 128
    assert packet["content"]["voice"][0]["transcript_status"] == "unavailable"
    assert packet["repository"]["default_branch"] == "main"
    assert packet["discord"]["workflow_thread_id"] is None
    assert packet["spec_kit"]["feature_directory"] is None


@pytest.mark.parametrize("missing_key", ["channel_id", "message_id", "author_id", "created_at"])
def test_source_context_packet_requires_core_message_fields(missing_key):
    message = {
        "channel_id": "c",
        "message_id": "m",
        "author_id": "u",
        "created_at": "2026-07-10T00:00:00Z",
    }
    message.pop(missing_key)

    with pytest.raises(ValueError, match=missing_key):
        build_source_context_packet(message, repository={"path": "/tmp/repo", "default_branch": "main"})


def test_initialize_local_spec_kit_seed_persists_packet_and_state(tmp_path):
    repo = _repo(tmp_path)
    packet = build_source_context_packet(
        _message(),
        repository={"path": str(repo), "default_branch": "main"},
        received_at="2026-07-10T00:00:01Z",
    )

    state = initialize_local_spec_kit_seed(packet, feature_title="Discord Spec Factory")

    feature_dir = repo / state["feature_directory"]
    source_packet_path = repo / state["source_packet_path"]
    assert feature_dir.is_dir()
    assert source_packet_path.is_file()
    assert (feature_dir / "workflow-state.json").is_file()
    assert json.loads(source_packet_path.read_text())["workflow_id"] == packet["workflow_id"]
    assert state["current_phase"] == "specify"
    assert state["artifact_paths"]["spec"] == f"{state['feature_directory']}/spec.md"


def test_initialize_local_spec_kit_seed_resumes_exact_duplicate_without_overwrite(tmp_path):
    repo = _repo(tmp_path)
    packet = build_source_context_packet(
        _message(),
        repository={"path": str(repo), "default_branch": "main"},
        received_at="2026-07-10T00:00:01Z",
    )
    first = initialize_local_spec_kit_seed(packet, feature_title="Discord Spec Factory", initialized_at="2026-07-10T00:00:02Z")
    packet_path = repo / first["source_packet_path"]
    original_packet = packet_path.read_text(encoding="utf-8")

    second = initialize_local_spec_kit_seed(packet, feature_title="Discord Spec Factory", initialized_at="2026-07-10T00:00:03Z")

    assert second == first
    assert packet_path.read_text(encoding="utf-8") == original_packet


def test_initialize_local_spec_kit_seed_records_edited_message_without_mutating_source_packet(tmp_path):
    repo = _repo(tmp_path)
    original = build_source_context_packet(
        _message(),
        repository={"path": str(repo), "default_branch": "main"},
        received_at="2026-07-10T00:00:01Z",
    )
    state = initialize_local_spec_kit_seed(original, feature_title="Discord Spec Factory")
    packet_path = repo / state["source_packet_path"]
    original_packet = packet_path.read_text(encoding="utf-8")
    edited_message = dict(_message(), text="Edited Discord Spec Factory request")
    edited = build_source_context_packet(
        edited_message,
        repository={"path": str(repo), "default_branch": "main"},
        received_at="2026-07-10T00:05:00Z",
    )

    observed = initialize_local_spec_kit_seed(edited, feature_title="Discord Spec Factory", initialized_at="2026-07-10T00:05:01Z")

    assert packet_path.read_text(encoding="utf-8") == original_packet
    assert observed["events"][-1]["type"] == "source_packet_conflict_observed"
    assert observed["events"][-1]["incoming_text_sha256"] == edited["content"]["text_sha256"]


def test_repository_confinement_requires_real_repo_marker(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    packet = build_source_context_packet(
        _message(),
        repository={"path": str(repo), "default_branch": "main"},
        received_at="2026-07-10T00:00:01Z",
    )

    with pytest.raises(ValueError, match="repository marker"):
        initialize_local_spec_kit_seed(packet)


def test_repository_confinement_rejects_symlink_escape_for_source_packet(tmp_path):
    repo = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    hermes_dir = repo / ".hermes"
    hermes_dir.symlink_to(outside, target_is_directory=True)
    packet = build_source_context_packet(
        _message(),
        repository={"path": str(repo), "default_branch": "main"},
        received_at="2026-07-10T00:00:01Z",
    )

    with pytest.raises(ValueError, match="escapes repository"):
        initialize_local_spec_kit_seed(packet)


def test_workflow_thread_name_is_bounded_and_slugged():
    name = workflow_thread_name("dsf_abcdef1234567890", "Build: Discord → Spec Kit factory!!!", limit=40)

    assert name == "spec-abcdef12: build-discord-spec-kit"
    assert len(name) <= 40


def test_role_routes_require_all_factory_roles():
    routes = _complete_routes()
    routes.pop("reviewer")

    result = validate_role_routes(routes)

    assert result.ok is False
    assert "reviewer" in result.reason


def test_role_routes_require_profile_model_and_toolsets_list():
    routes = _complete_routes()
    routes["architect"] = {"profile": "architect", "model": "reasoning", "toolsets": "file"}

    result = validate_role_routes(routes)

    assert result.ok is False
    assert "toolsets" in result.reason


def test_role_routes_complete_contract_passes():
    assert validate_role_routes(_complete_routes()).ok is True


def test_profile_existence_dry_run_fails_closed_for_missing_profile(tmp_path):
    hermes_home = tmp_path / "hermes"
    (hermes_home / "profiles" / "spec-lead").mkdir(parents=True)
    routes = _complete_routes()
    routes["architect"]["profile"] = "missing-architect"

    result = validate_role_routes(routes, hermes_home=hermes_home, require_profiles_exist=True)

    assert result.ok is False
    assert "missing-architect" in result.reason


def test_synthetic_discord_intake_builds_local_steel_thread_without_live_effects(tmp_path):
    repo = _repo(tmp_path)

    result = process_synthetic_discord_intake(
        _message(),
        binding=_binding(repo),
        received_at="2026-07-10T00:00:01Z",
    )

    assert result["ok"] is True
    assert result["live_effects"] == []
    assert result["source_packet"]["workflow_id"] == result["workflow_state"]["workflow_id"]
    assert (repo / result["workflow_state"]["source_packet_path"]).is_file()
    assert result["thread_open_request"]["adapter_method"] == "create_handoff_thread"
    assert result["thread_open_request"]["parent_channel_id"] == "123456789012345678"
    assert result["kanban_projection"]["enabled"] is False


def test_workflow_thread_registry_maps_thread_to_workflow(tmp_path):
    repo = _repo(tmp_path)
    state = {"workflow_id": "dsf_abc123", "feature_directory": "specs/dsf_abc123-demo"}

    entry = register_workflow_thread(
        repo,
        workflow_state=state,
        parent_channel_id="parent-1",
        thread_id="thread-1",
        source_message_id="message-1",
        thread_name="spec-abc123: demo",
    )

    registry = json.loads((repo / ".hermes" / "discord-spec-factory" / "thread-registry.json").read_text())
    assert entry["status"] == "active"
    assert registry["threads"]["thread-1"]["workflow_id"] == "dsf_abc123"


def test_gate_decision_requires_authorized_actor_and_thread_mapping():
    gate = {
        "gate_id": "clarify-1",
        "workflow_id": "dsf_abc123",
        "phase": "clarify",
        "status": "pending",
        "required_actor_ids": ["actor-1"],
        "required_role_ids": ["role-reviewer"],
    }
    registry = {"threads": {"thread-1": {"workflow_id": "dsf_abc123"}}}

    denied = apply_gate_decision(
        gate,
        {"thread_id": "thread-1", "message_id": "m1", "author_id": "actor-2", "author_roles": []},
        registry=registry,
        decision="approved",
        decided_at="2026-07-10T00:00:02Z",
    )
    approved = apply_gate_decision(
        gate,
        {"thread_id": "thread-1", "message_id": "m2", "author_id": "actor-2", "author_roles": ["role-reviewer"]},
        registry=registry,
        decision="approved",
        decided_at="2026-07-10T00:00:03Z",
    )

    assert denied["ok"] is False
    assert denied["gate"]["status"] == "pending"
    assert approved["ok"] is True
    assert approved["gate"]["status"] == "approved"
    assert approved["gate"]["decision"]["actor_id"] == "actor-2"


def test_gate_decision_fails_closed_when_constraints_are_absent():
    gate = {
        "gate_id": "review-1",
        "workflow_id": "dsf_abc123",
        "phase": "review",
        "status": "pending",
    }
    registry = {"threads": {"thread-1": {"workflow_id": "dsf_abc123"}}}

    result = apply_gate_decision(
        gate,
        {"thread_id": "thread-1", "message_id": "m1", "author_id": "actor-1", "author_roles": ["role-reviewer"]},
        registry=registry,
        decision="approved",
    )

    assert result["ok"] is False
    assert "authorization constraint" in result["reason"]


def test_execution_packet_uses_role_profile_model_route_after_gate(tmp_path):
    hermes_home = tmp_path / "hermes"
    for profile in {route["profile"] for route in _complete_routes().values()} - {"default"}:
        (hermes_home / "profiles" / profile).mkdir(parents=True)
    state = {
        "workflow_id": "dsf_abc123",
        "feature_directory": "specs/dsf_abc123-demo",
        "source_packet_path": ".hermes/discord-spec-factory/source-packets/dsf_abc123.json",
        "artifact_paths": {"spec": "specs/dsf_abc123-demo/spec.md"},
    }

    packet = build_execution_packet(
        state,
        routes=_complete_routes(),
        role="implementer",
        hermes_home=hermes_home,
        require_profile_exists=True,
        approval_gate={"gate_id": "tasks-approved", "status": "approved"},
    )

    assert packet["role"] == "implementer"
    assert packet["profile"] == "implementer"
    assert packet["model"] == "code"
    assert packet["approval_gate_id"] == "tasks-approved"
    assert packet["source_packet_path"].endswith("dsf_abc123.json")


def test_execution_packet_requires_approved_gate_for_local_git_even_if_route_opts_out():
    routes = _complete_routes()
    routes["implementer"] = {
        **routes["implementer"],
        "side_effects": "local-git",
        "approval_required": False,
    }

    with pytest.raises(ValueError, match="requires an approved gate"):
        build_execution_packet(
            {"workflow_id": "dsf_abc123", "feature_directory": "specs/dsf_abc123-demo"},
            routes=routes,
            role="implementer",
        )


def test_execution_packet_requires_approved_gate_for_remote_github_even_if_omitted():
    routes = _complete_routes()
    routes["release-captain"] = {
        **routes["release-captain"],
        "side_effects": "remote-github",
    }

    with pytest.raises(ValueError, match="requires an approved gate"):
        build_execution_packet(
            {"workflow_id": "dsf_abc123", "feature_directory": "specs/dsf_abc123-demo"},
            routes=routes,
            role="release-captain",
        )


def test_kanban_projection_disabled_is_derived_noop():
    state = {
        "workflow_id": "dsf_abc123",
        "feature_directory": "specs/dsf_abc123-demo",
        "source_packet_path": ".hermes/discord-spec-factory/source-packets/dsf_abc123.json",
        "current_phase": "plan",
        "discord": {"workflow_thread_id": "thread-1"},
    }

    projection = build_kanban_projection_packet(state, {"enabled": False, "mode": "disabled"})

    assert projection["enabled"] is False
    assert projection["card_requests"] == []
    assert projection["canonical_source"] == "spec_kit"


def test_pr_target_validation_and_handoff_packet_require_local_evidence():
    state = {
        "workflow_id": "dsf_abc123",
        "feature_directory": "specs/dsf_abc123-demo",
        "source_packet_path": ".hermes/discord-spec-factory/source-packets/dsf_abc123.json",
        "discord": {"workflow_thread_id": "thread-1"},
    }
    target = {
        "repository": "nous/hermes-agent",
        "base_branch": "main",
        "feature_branch": "spec/dsf_abc123-demo",
        "title": "[Spec Factory] Demo",
        "test_evidence": ["python -m pytest tests/hermes_cli/test_discord_spec_factory.py -q"],
        "rollback_plan": "Revert the feature branch commit and disable the intake binding.",
        "approval_gate_id": "release-approved",
    }

    assert validate_pr_target(target).ok is True
    handoff = build_pr_handoff_packet(state, target)

    assert handoff["remote_effects_enabled"] is False
    assert "Spec Kit Artifacts" in handoff["pr_body"]
    assert "thread-1" in handoff["pr_body"]


def test_pr_target_validation_fails_without_rollback_plan():
    result = validate_pr_target({
        "repository": "nous/hermes-agent",
        "base_branch": "main",
        "feature_branch": "spec/dsf_abc123-demo",
        "title": "[Spec Factory] Demo",
        "test_evidence": ["pytest"],
        "approval_gate_id": "release-approved",
    })

    assert result.ok is False
    assert "rollback" in result.reason
