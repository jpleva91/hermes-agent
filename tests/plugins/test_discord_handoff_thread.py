from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


class FakeDMChannel:
    pass


class FakeThread:
    def __init__(self, *, parent=None, parent_id=None):
        self.parent = parent
        self.parent_id = parent_id


@pytest.mark.asyncio
async def test_create_handoff_thread_from_thread_origin_uses_parent_channel():
    """Cron attach_to_session jobs created from a Discord thread pass that
    thread id as parent_chat_id. The adapter must create a sibling thread under
    the parent channel, not attempt to create a child thread from the origin
    thread itself.
    """
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="token"))

    created_thread = SimpleNamespace(id=999)
    parent_channel = MagicMock()
    parent_channel.create_thread = AsyncMock(return_value=created_thread)
    origin_thread = FakeThread(parent=parent_channel)

    client = MagicMock()
    client.get_channel.return_value = origin_thread
    adapter._client = client

    fake_discord = SimpleNamespace(
        Thread=FakeThread,
        DMChannel=FakeDMChannel,
        ChannelType=SimpleNamespace(public_thread="public_thread"),
    )
    with patch("plugins.platforms.discord.adapter.DISCORD_AVAILABLE", True), \
         patch("plugins.platforms.discord.adapter.discord", fake_discord):
        thread_id = await adapter.create_handoff_thread("123", "spec-rb-001")

    assert thread_id == "999"
    parent_channel.create_thread.assert_awaited_once()
    assert parent_channel.create_thread.await_args.kwargs["name"] == "spec-rb-001"
    assert parent_channel.create_thread.await_args.kwargs["type"] == "public_thread"
