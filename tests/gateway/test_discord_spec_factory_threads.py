from __future__ import annotations

import json

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.discord_spec_factory import (
    SpecKitContractSeedRuntime,
    build_source_context_packet,
    build_thread_open_request,
    initialize_local_spec_kit_seed,
    load_thread_registry,
    register_workflow_thread,
    workflow_id_for_message,
)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def _packet(repo):
    return build_source_context_packet(
        {
            "channel_id": "123456789012345678",
            "message_id": "333333333333333333",
            "author_id": "444444444444444444",
            "author_roles": ["555555555555555555"],
            "created_at": "2026-07-10T00:00:00Z",
            "text": "Build the Discord Spec Factory",
        },
        repository={"path": str(repo), "default_branch": "main"},
        received_at="2026-07-10T00:00:01Z",
    )


def test_thread_open_request_uses_existing_handoff_thread_adapter(tmp_path):
    repo = _repo(tmp_path)
    packet = _packet(repo)
    state = initialize_local_spec_kit_seed(packet)

    request = build_thread_open_request(packet, state)

    assert request["adapter_method"] == "create_handoff_thread"
    assert request["live_effect"] is False
    assert request["parent_channel_id"] == "123456789012345678"
    assert request["workflow_id"] == packet["workflow_id"]
    assert state["feature_directory"] in request["initial_thread_brief"]


def test_thread_registry_recovers_mapping_from_repository_state(tmp_path):
    repo = _repo(tmp_path)
    packet = _packet(repo)
    state = initialize_local_spec_kit_seed(packet)

    register_workflow_thread(
        repo,
        workflow_state=state,
        parent_channel_id="123456789012345678",
        thread_id="thread-123",
        source_message_id=packet["source"]["message_id"],
        thread_name="spec-deadbeef: demo",
        registered_at="2026-07-10T00:00:02Z",
    )

    reloaded = json.loads((repo / ".hermes" / "discord-spec-factory" / "thread-registry.json").read_text())
    assert reloaded["workflows"][packet["workflow_id"]] == "thread-123"
    assert reloaded["threads"]["thread-123"]["feature_directory"] == state["feature_directory"]


def _register(repo, workflow_id, thread_id):
    return register_workflow_thread(
        repo,
        workflow_state={"workflow_id": workflow_id, "feature_directory": f"specs/{workflow_id}-demo"},
        parent_channel_id="parent-1",
        thread_id=thread_id,
        source_message_id="source-1",
        thread_name=f"spec-{workflow_id}: demo",
        registered_at="2026-07-10T00:00:02Z",
    )


def test_thread_registry_rejects_same_thread_for_different_workflow(tmp_path):
    repo = _repo(tmp_path)
    _register(repo, "workflow-1", "thread-same")

    with pytest.raises(ValueError, match="different workflow"):
        _register(repo, "workflow-2", "thread-same")


def test_thread_registry_rejects_same_workflow_for_different_thread(tmp_path):
    repo = _repo(tmp_path)
    _register(repo, "workflow-same", "thread-1")

    with pytest.raises(ValueError, match="different thread"):
        _register(repo, "workflow-same", "thread-2")


def test_thread_registry_exact_reregistration_is_idempotent(tmp_path):
    repo = _repo(tmp_path)
    first = _register(repo, "workflow-same", "thread-same")
    second = _register(repo, "workflow-same", "thread-same")

    assert second == first
    registry = json.loads((repo / ".hermes" / "discord-spec-factory" / "thread-registry.json").read_text())
    assert list(registry["threads"]) == ["thread-same"]
    assert registry["workflows"] == {"workflow-same": "thread-same"}


def test_thread_registry_rejects_entry_whose_thread_id_disagrees_with_key(tmp_path):
    repo = _repo(tmp_path)
    registry_path = repo / ".hermes" / "discord-spec-factory" / "thread-registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "threads": {
                    "thread-1": {
                        "workflow_id": "workflow-1",
                        "thread_id": "thread-other",
                    }
                },
                "workflows": {"workflow-1": "thread-1"},
            }
        )
    )

    with pytest.raises(ValueError, match="inconsistent with its key"):
        _register(repo, "workflow-1", "thread-1")


class FakeSpecKitRuntime(SpecKitContractSeedRuntime):
    def __init__(self):
        self.calls = []

    def start_contract_seed(self, *, workflow_path, repository_path, workflow_id, inputs, authority_packet):
        self.calls.append(
            {
                "workflow_path": str(workflow_path),
                "repository_path": str(repository_path),
                "workflow_id": workflow_id,
                "inputs": inputs,
                "authority_packet": authority_packet,
            }
        )
        return {
            "schema_version": 1,
            "workflow_id": workflow_id,
            "feature_directory": f"specs/{workflow_id}-official",
            "current_phase": "specify",
            "phase_status": {"specify": "active"},
            "source_packet_path": f".hermes/discord-spec-factory/source-packets/{workflow_id}.json",
            "artifact_paths": {"spec": f"specs/{workflow_id}-official/spec.md"},
            "gates": [
                {
                    "gate_id": "review-spec",
                    "workflow_id": workflow_id,
                    "phase": "review-spec",
                    "status": "pending",
                    "required_role_ids": ["role-maintainer"],
                }
            ],
            "discord": {"workflow_thread_id": None},
            "spec_kit": {"official_workflow_loaded": True, "official_workflow_executed": False},
            "events": [],
        }


class FakeAdapter:
    def __init__(self):
        self.created_threads = []
        self.sent = []

    async def create_handoff_thread(self, parent_chat_id, name):
        self.created_threads.append((parent_chat_id, name))
        return f"thread-{len(self.created_threads)}"

    async def send(self, chat_id, content, metadata=None, reply_to=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata, "reply_to": reply_to})


def _discord_source(*, chat_id="123456789012345678", thread_id=None, parent_chat_id=None):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        parent_chat_id=parent_chat_id,
        chat_type="thread" if thread_id else "channel",
        thread_id=thread_id,
        user_id="user-1",
        user_name="Maintainer",
        message_id="msg-1",
    )


def test_gateway_spec_factory_disabled_by_default(tmp_path, monkeypatch):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    event = MessageEvent(text="Build a thing", source=_discord_source(), message_id="msg-1")

    assert runner._discord_spec_factory_intake_binding(event) is None


@pytest.mark.asyncio
async def test_gateway_intake_uses_official_workflow_runtime_and_registers_handoff_thread(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / ".specify" / "workflows" / "speckit").mkdir(parents=True)
    (repo / ".specify" / "workflows" / "speckit" / "workflow.yml").write_text("workflow:\n  id: speckit\n")
    cfg = {
        "discord": {
            "spec_factory": {
                "enabled": True,
                "channel_id": "123456789012345678",
                "allowed_role_ids": ["role-maintainer"],
                "repository": {"path": str(repo), "default_branch": "main"},
            }
        }
    }
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    runtime = FakeSpecKitRuntime()
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: FakeAdapter()}
    runner._discord_spec_factory_runtime = runtime

    event = MessageEvent(
        text="Build the official factory",
        source=_discord_source(),
        message_id="msg-1",
        metadata={"author_roles": ["role-maintainer"]},
    )

    response = await runner._handle_discord_spec_factory_intake(event)

    assert response == ""
    assert runtime.calls and runtime.calls[0]["inputs"]["spec"] == "Build the official factory"
    assert runtime.calls[0]["authority_packet"]["mode"] == "discord-spec-factory"
    assert runner.adapters[Platform.DISCORD].created_threads[0][0] == "123456789012345678"
    registry = load_thread_registry(repo)
    assert registry["threads"]["thread-1"]["workflow_id"] == runtime.calls[0]["workflow_id"]


@pytest.mark.asyncio
async def test_gateway_thread_reply_persists_gate_decision_and_skips_agent(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    workflow_id = "dsf_gate"
    state = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "feature_directory": f"specs/{workflow_id}-official",
        "current_phase": "review-spec",
        "source_packet_path": f".hermes/discord-spec-factory/source-packets/{workflow_id}.json",
        "artifact_paths": {},
        "discord": {"workflow_thread_id": "thread-42"},
        "spec_kit": {"official_workflow_loaded": True, "official_workflow_executed": False},
        "gates": [
            {
                "gate_id": "review-spec",
                "workflow_id": workflow_id,
                "phase": "review-spec",
                "status": "pending",
                "required_role_ids": ["role-maintainer"],
            }
        ],
        "events": [],
    }
    feature = repo / state["feature_directory"]
    feature.mkdir(parents=True)
    (feature / "workflow-state.json").write_text(json.dumps(state))
    register_workflow_thread(
        repo,
        workflow_state=state,
        parent_channel_id="123456789012345678",
        thread_id="thread-42",
        source_message_id="msg-1",
        thread_name="spec-gate: demo",
        registered_at="2026-07-10T00:00:02Z",
    )
    cfg = {
        "discord": {
            "spec_factory": {
                "enabled": True,
                "channel_id": "123456789012345678",
                "allowed_role_ids": ["role-maintainer"],
                "repository": {"path": str(repo), "default_branch": "main"},
            }
        }
    }
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: FakeAdapter()}
    event = MessageEvent(
        text="/approve",
        source=_discord_source(chat_id="thread-42", thread_id="thread-42", parent_chat_id="123456789012345678"),
        message_id="reply-1",
        metadata={"author_roles": ["role-maintainer"]},
    )

    response = await runner._handle_discord_spec_factory_thread_reply(event)

    assert response == ""
    updated = json.loads((feature / "workflow-state.json").read_text())
    assert updated["gates"][0]["status"] == "approved"
    assert updated["gates"][0]["decision"]["discord_message_id"] == "reply-1"


@pytest.mark.asyncio
async def test_gateway_exact_duplicate_intake_reuses_existing_thread_and_persisted_state(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / ".specify" / "workflows" / "speckit").mkdir(parents=True)
    (repo / ".specify" / "workflows" / "speckit" / "workflow.yml").write_text("workflow:\n  id: speckit\nsteps:\n  - id: specify\n    type: task\n")
    cfg = {
        "discord": {
            "spec_factory": {
                "enabled": True,
                "channel_id": "123456789012345678",
                "allowed_role_ids": ["role-maintainer"],
                "repository": {"path": str(repo), "default_branch": "main"},
            }
        }
    }
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    runtime = FakeSpecKitRuntime()
    adapter = FakeAdapter()
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: adapter}
    runner._discord_spec_factory_runtime = runtime
    event = MessageEvent(
        text="Build the official factory",
        source=_discord_source(),
        message_id="msg-1",
        metadata={"author_roles": ["role-maintainer"]},
    )

    assert await runner._handle_discord_spec_factory_intake(event) == ""
    assert await runner._handle_discord_spec_factory_intake(event) == ""

    assert adapter.created_threads == [("123456789012345678", adapter.created_threads[0][1])]
    assert len(runtime.calls) == 1
    registry = load_thread_registry(repo)
    workflow_id = runtime.calls[0]["workflow_id"]
    assert registry["workflows"][workflow_id] == "thread-1"
    state_path = repo / f"specs/{workflow_id}-official" / "workflow-state.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["discord"]["workflow_thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_gateway_does_not_build_discarded_execution_kanban_or_pr_packets(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / ".specify" / "workflows" / "speckit").mkdir(parents=True)
    (repo / ".specify" / "workflows" / "speckit" / "workflow.yml").write_text("workflow:\n  id: speckit\nsteps:\n  - id: specify\n    type: task\n")
    cfg = {
        "discord": {
            "spec_factory": {
                "enabled": True,
                "channel_id": "123456789012345678",
                "allowed_role_ids": ["role-maintainer"],
                "repository": {"path": str(repo), "default_branch": "main"},
                "kanban_projection": {"enabled": True, "mode": "notify-only"},
                "routes": {"intake-triage": {"profile": "default", "model": "fast", "toolsets": []}},
                "github_handoff": {"enabled": True},
            }
        }
    }
    import hermes_cli.discord_spec_factory as dsf

    def forbidden(*args, **kwargs):
        raise AssertionError("discarded side-effect packet builder should not be called from gateway intake")

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    monkeypatch.setattr(dsf, "build_kanban_projection_packet", forbidden)
    monkeypatch.setattr(dsf, "build_execution_packet", forbidden)
    monkeypatch.setattr(dsf, "build_pr_handoff_packet", forbidden)
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: FakeAdapter()}
    runner._discord_spec_factory_runtime = FakeSpecKitRuntime()
    event = MessageEvent(
        text="Build the official factory",
        source=_discord_source(),
        message_id="msg-1",
        metadata={"author_roles": ["role-maintainer"]},
    )

    assert await runner._handle_discord_spec_factory_intake(event) == ""


@pytest.mark.asyncio
async def test_gateway_free_text_does_not_approve_approval_gate(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    workflow_id = "dsf_gate"
    state = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "feature_directory": f"specs/{workflow_id}-official",
        "current_phase": "review-spec",
        "source_packet_path": f".hermes/discord-spec-factory/source-packets/{workflow_id}.json",
        "artifact_paths": {},
        "discord": {"workflow_thread_id": "thread-42"},
        "spec_kit": {"official_workflow_loaded": True, "official_workflow_executed": False},
        "gates": [{"gate_id": "review-spec", "workflow_id": workflow_id, "phase": "approval", "type": "approval", "status": "pending", "options": ["approve", "reject"], "required_role_ids": ["role-maintainer"]}],
        "events": [],
    }
    feature = repo / state["feature_directory"]
    feature.mkdir(parents=True)
    (feature / "workflow-state.json").write_text(json.dumps(state))
    register_workflow_thread(repo, workflow_state=state, parent_channel_id="123456789012345678", thread_id="thread-42", source_message_id="msg-1", thread_name="spec-gate: demo")
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"discord": {"spec_factory": {"enabled": True, "channel_id": "123456789012345678", "allowed_role_ids": ["role-maintainer"], "repository": {"path": str(repo), "default_branch": "main"}}}})
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: FakeAdapter()}
    event = MessageEvent(text="looks reasonable", source=_discord_source(chat_id="thread-42", thread_id="thread-42", parent_chat_id="123456789012345678"), message_id="reply-1", metadata={"author_roles": ["role-maintainer"]})

    assert await runner._handle_discord_spec_factory_thread_reply(event) is None
    updated = json.loads((feature / "workflow-state.json").read_text())
    assert updated["gates"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_gateway_clarify_gate_accepts_free_text_answer(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    workflow_id = "dsf_clarify"
    state = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "feature_directory": f"specs/{workflow_id}-official",
        "current_phase": "clarify",
        "source_packet_path": f".hermes/discord-spec-factory/source-packets/{workflow_id}.json",
        "artifact_paths": {},
        "discord": {"workflow_thread_id": "thread-42"},
        "spec_kit": {"official_workflow_loaded": True, "official_workflow_executed": False},
        "gates": [{"gate_id": "clarify-scope", "workflow_id": workflow_id, "phase": "clarify", "type": "clarify", "status": "pending", "required_role_ids": ["role-maintainer"]}],
        "events": [],
    }
    feature = repo / state["feature_directory"]
    feature.mkdir(parents=True)
    (feature / "workflow-state.json").write_text(json.dumps(state))
    register_workflow_thread(repo, workflow_state=state, parent_channel_id="123456789012345678", thread_id="thread-42", source_message_id="msg-1", thread_name="spec-gate: demo")
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"discord": {"spec_factory": {"enabled": True, "channel_id": "123456789012345678", "allowed_role_ids": ["role-maintainer"], "repository": {"path": str(repo), "default_branch": "main"}}}})
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: FakeAdapter()}
    event = MessageEvent(text="Please constrain this to config only.", source=_discord_source(chat_id="thread-42", thread_id="thread-42", parent_chat_id="123456789012345678"), message_id="reply-1", metadata={"author_roles": ["role-maintainer"]})

    assert await runner._handle_discord_spec_factory_thread_reply(event) == ""
    updated = json.loads((feature / "workflow-state.json").read_text())
    assert updated["gates"][0]["status"] == "answered"
    assert updated["gates"][0]["decision"]["answer"] == "Please constrain this to config only."


def test_gateway_spec_factory_config_loads_from_real_temp_config_path(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "discord:\n  spec_factory:\n    enabled: true\n    channel_id: '123456789012345678'\n    allowed_role_ids:\n      - role-maintainer\n    repository:\n      path: /tmp/repo\n      default_branch: main\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = gateway_run.GatewayRunner(GatewayConfig())

    cfg = runner._discord_spec_factory_config()

    assert cfg["enabled"] is True
    assert cfg["channel_id"] == "123456789012345678"
    assert cfg["repository"]["default_branch"] == "main"


@pytest.mark.asyncio
async def test_gateway_intake_failure_message_is_generic_with_workflow_id(tmp_path, monkeypatch):
    missing_repo = tmp_path / "missing-repo"
    cfg = {"discord": {"spec_factory": {"enabled": True, "channel_id": "123456789012345678", "allowed_role_ids": ["role-maintainer"], "repository": {"path": str(missing_repo), "default_branch": "main"}}}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    runner = gateway_run.GatewayRunner(GatewayConfig())
    adapter = FakeAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    event = MessageEvent(text="Build the official factory", source=_discord_source(), message_id="msg-1", metadata={"author_roles": ["role-maintainer"]})

    assert await runner._handle_discord_spec_factory_intake(event) == ""

    assert adapter.sent
    content = adapter.sent[-1]["content"]
    assert "Discord Spec Factory intake failed closed" in content
    assert "workflow" in content.lower()
    assert str(missing_repo) not in content
    assert "repository.path" not in content


@pytest.mark.asyncio
async def test_gateway_disabled_binding_does_not_consume_registered_thread_reply(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    workflow_id = "dsf_disabled"
    state = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "feature_directory": f"specs/{workflow_id}-contract-seed",
        "current_phase": "clarify",
        "artifact_paths": {},
        "discord": {"workflow_thread_id": "thread-42"},
        "gates": [{"gate_id": "clarify", "workflow_id": workflow_id, "type": "clarify", "status": "pending", "required_role_ids": ["role-maintainer"]}],
        "events": [],
    }
    feature = repo / state["feature_directory"]
    feature.mkdir(parents=True)
    (feature / "workflow-state.json").write_text(json.dumps(state))
    register_workflow_thread(repo, workflow_state=state, parent_channel_id="parent", thread_id="thread-42", source_message_id="msg-1", thread_name="spec-disabled")
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"discord": {"spec_factory": {"enabled": False, "repository": {"path": str(repo)}}}})
    runner = gateway_run.GatewayRunner(GatewayConfig())
    event = MessageEvent(text="an answer", source=_discord_source(chat_id="thread-42", thread_id="thread-42", parent_chat_id="parent"), message_id="reply-1", metadata={"author_roles": ["role-maintainer"]})

    assert await runner._handle_discord_spec_factory_thread_reply(event) is None
    persisted = json.loads((feature / "workflow-state.json").read_text())
    assert persisted["gates"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_gateway_duplicate_resume_rejects_registry_path_escape(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "workflow-state.json").write_text(json.dumps({"current_phase": "EXTERNAL_SENTINEL"}))
    workflow_id = workflow_id_for_message("123456789012345678", "msg-1")
    registry_path = repo / ".hermes" / "discord-spec-factory" / "thread-registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps({"schema_version": 1, "threads": {"thread-1": {"workflow_id": workflow_id, "thread_id": "thread-1", "feature_directory": "../outside"}}, "workflows": {workflow_id: "thread-1"}}))
    cfg = {"discord": {"spec_factory": {"enabled": True, "channel_id": "123456789012345678", "allowed_role_ids": ["role-maintainer"], "repository": {"path": str(repo), "default_branch": "main"}}}}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: cfg)
    adapter = FakeAdapter()
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {Platform.DISCORD: adapter}
    event = MessageEvent(text="Build", source=_discord_source(), message_id="msg-1", metadata={"author_roles": ["role-maintainer"]})

    assert await runner._handle_discord_spec_factory_intake(event) == ""
    assert adapter.created_threads == []
    assert adapter.sent
    assert all("EXTERNAL_SENTINEL" not in item["content"] for item in adapter.sent)
