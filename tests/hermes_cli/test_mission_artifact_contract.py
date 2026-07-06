"""Kanban-level proof for artifact-driven Mission Engine handoff preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import mission_guardrail_policy as mgp
from tests.mission_policy_fixtures import MISSION_BOARD_SLUG, write_mission_policy


@pytest.fixture(autouse=True)
def _reset_policy_cache():
    mgp.clear_cache()
    yield
    mgp.clear_cache()


@pytest.fixture
def mission_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.set_current_board(MISSION_BOARD_SLUG)
    write_mission_policy(home, artifact_contract=True)
    with kb.connect(board=MISSION_BOARD_SLUG) as conn:
        parent = kb.create_task(
            conn,
            title="Mission parent: artifact-driven preflight",
            body="Spec Kit: /tmp/specs/003/spec.md\nSource packet: /tmp/specs/003/source-packet.md",
            assignee="missioncommander",
            created_by="missioncommander",
        )
    return parent


def test_commander_child_card_without_artifacts_is_rejected(mission_board):
    with kb.connect(board=MISSION_BOARD_SLUG) as conn:
        with pytest.raises(ValueError, match="artifact-driven handoff preflight.*spec_kit, source_packet"):
            kb.create_task(
                conn,
                title="Implement Mission Control gate queue",
                body="Build it and write a handoff after.",
                assignee="runtimesteward",
                created_by="missioncommander",
                parents=[mission_board],
            )


def test_commander_child_card_with_spec_and_source_packets_is_accepted(mission_board):
    with kb.connect(board=MISSION_BOARD_SLUG) as conn:
        task_id = kb.create_task(
            conn,
            title="Implement Mission Control gate queue",
            body=(
                "Spec Kit: /home/red/.hermes/mission-control/specs/003-artifact-driven-preflight/spec.md\n"
                "Source packet: /home/red/.hermes/mission-control/specs/003-artifact-driven-preflight/source-packet.md\n"
                "Evidence: /home/red/.hermes/mission-control/specs/003-artifact-driven-preflight/evidence.md"
            ),
            assignee="runtimesteward",
            created_by="missioncommander",
            parents=[mission_board],
        )
        assert task_id.startswith("t_")
