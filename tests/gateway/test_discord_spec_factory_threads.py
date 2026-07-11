from __future__ import annotations

import json

import pytest

from hermes_cli.discord_spec_factory import (
    build_source_context_packet,
    build_thread_open_request,
    initialize_local_spec_kit_seed,
    register_workflow_thread,
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
