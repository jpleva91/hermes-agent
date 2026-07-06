"""Tests for deterministic Mission Control/Fable "what's next" gateway guard."""

from gateway.config import Platform
from gateway.session import SessionSource


def _discord_source(**overrides):
    data = dict(
        platform=Platform.DISCORD,
        user_id="u1",
        user_name="red",
        chat_id="c1",
        chat_name="red's server / #clawta / Mission Control fable emulation workflow",
        chat_type="group",
        thread_id="t1",
    )
    data.update(overrides)
    return SessionSource(**data)


class TestMissionControlWhatNextGuard:
    def test_injects_guard_for_what_next_in_mission_control_context(self):
        from gateway.run import _mission_control_what_next_guard_prompt

        prompt = _mission_control_what_next_guard_prompt(
            "what's next?",
            source=_discord_source(),
            channel_prompt=None,
        )

        assert prompt is not None
        assert "Mission Control / Fable runtime guard" in prompt
        assert "fable-emulation-workflow" in prompt
        assert "Mission Engine" in prompt
        assert "Obsidian" in prompt
        assert "NotebookLM" in prompt

    def test_does_not_inject_for_generic_what_next_without_mission_context(self):
        from gateway.run import _mission_control_what_next_guard_prompt

        prompt = _mission_control_what_next_guard_prompt(
            "what's next?",
            source=_discord_source(
                chat_name="red's server / #cooking",
                chat_topic="dinner ideas",
            ),
            channel_prompt=None,
        )

        assert prompt is None

    def test_does_not_inject_for_mission_context_without_what_next_intent(self):
        from gateway.run import _mission_control_what_next_guard_prompt

        prompt = _mission_control_what_next_guard_prompt(
            "summarize the fable workflow audit",
            source=_discord_source(),
            channel_prompt=None,
        )

        assert prompt is None

    def test_explicit_mission_terms_in_message_are_enough_context(self):
        from gateway.run import _mission_control_what_next_guard_prompt

        prompt = _mission_control_what_next_guard_prompt(
            "what should we do next for ReadyBench Mission Control?",
            source=_discord_source(
                chat_name="red's server / #general",
                chat_topic="",
            ),
            channel_prompt=None,
        )

        assert prompt is not None
        assert "inspect the relevant Kanban board" in prompt

    def test_channel_prompt_can_supply_mission_context(self):
        from gateway.run import _mission_control_what_next_guard_prompt

        prompt = _mission_control_what_next_guard_prompt(
            "what's next?",
            source=_discord_source(
                chat_name="red's server / #general",
                chat_topic="",
            ),
            channel_prompt="This channel is for Mission Control / Fable lane intake.",
        )

        assert prompt is not None
        assert "Mission Control / Fable runtime guard" in prompt
