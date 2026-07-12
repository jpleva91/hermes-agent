from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
async def test_send_exec_approval_includes_workdir_and_reply_syntax():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))

    sent_msg = SimpleNamespace(id=1234)
    channel = SimpleNamespace(send=AsyncMock(return_value=sent_msg))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send_exec_approval(
        chat_id="555",
        command="git push origin main",
        session_key="agent:discord:555",
        description="Git remote write",
        metadata={"workdir": "/home/red/project"},
    )

    assert result.success is True
    assert result.message_id == "1234"
    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    embed = kwargs["embed"]
    assert embed.title == "⚠️ Command Approval Required"
    assert "git push origin main" in embed.description

    fields = {field["name"]: field["value"] for field in embed.fields}
    assert fields["Reason"] == "Git remote write"
    assert fields["Working directory"] == "`/home/red/project`"
    assert "/approve" in fields["Approve / deny"]
    assert "/approve session" in fields["Approve / deny"]
    assert "/approve always" in fields["Approve / deny"]
    assert "/deny" in fields["Approve / deny"]
    assert kwargs["view"].session_key == "agent:discord:555"


@pytest.mark.asyncio
async def test_exec_approval_view_deny_resolves_gateway_approval_and_marks_decision():
    from plugins.platforms.discord import adapter as discord_adapter
    from plugins.platforms.discord.adapter import ExecApprovalView

    view = ExecApprovalView(session_key="session-1", allowed_user_ids={"11111"})
    view.add_item(discord_adapter.discord.ui.Button(label="Allow Once"))
    view.add_item(discord_adapter.discord.ui.Button(label="Deny"))

    embed = discord_adapter.discord.Embed(
        title="⚠️ Command Approval Required",
        description="```\nrm -rf ./build-cache\n```",
        color=discord_adapter.discord.Color.orange(),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=11111, display_name="Norbert", roles=[]),
        message=SimpleNamespace(embeds=[embed]),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
    )

    from unittest.mock import patch

    with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
        await view._resolve(
            interaction,
            "deny",
            discord_adapter.discord.Color.red(),
            "Denied",
        )

    resolve.assert_called_once_with("session-1", "deny")
    interaction.response.edit_message.assert_awaited_once_with(embed=embed, view=view)
    interaction.response.send_message.assert_not_called()
    assert view.resolved is True
    assert all(getattr(child, "disabled", False) for child in view.children)
    footer = embed.footer
    footer_text = footer["text"] if isinstance(footer, dict) else footer.text
    assert footer_text == "Denied by Norbert"


@pytest.mark.asyncio
async def test_exec_approval_view_rejects_unauthorized_user_without_resolving():
    from plugins.platforms.discord import adapter as discord_adapter
    from plugins.platforms.discord.adapter import ExecApprovalView
    from unittest.mock import patch

    view = ExecApprovalView(session_key="session-1", allowed_user_ids={"11111"})
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=99999, display_name="Mallory", roles=[]),
        message=SimpleNamespace(embeds=[]),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
    )

    with patch("tools.approval.resolve_gateway_approval") as resolve:
        await view._resolve(
            interaction,
            "once",
            discord_adapter.discord.Color.green(),
            "Approved once",
        )

    resolve.assert_not_called()
    interaction.response.edit_message.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "You're not authorized to approve commands~", ephemeral=True
    )
    assert view.resolved is False
