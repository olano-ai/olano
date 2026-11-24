"""Tests for Discord channel adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def test_discord_channel_imports():
    from olano.channels.discord_channel import DiscordChannel

    assert DiscordChannel.name == "discord"
    assert DiscordChannel.max_message_length == 1900
    # Discord counts plain chars, not UTF-16 units.
    assert DiscordChannel.use_utf16_length is False


def test_discord_config_overrides_max_length():
    from olano.channels.discord_channel import DiscordChannel

    ch = DiscordChannel({"max_message_length": 1500})
    assert ch.max_message_length == 1500


def test_thread_for_channel_persistent():
    from olano.channels.discord_channel import _CHANNEL_THREADS, _thread_for_channel

    _CHANNEL_THREADS.clear()
    t1 = _thread_for_channel("dc_123")
    t2 = _thread_for_channel("dc_123")
    assert t1 == t2
    assert t1.startswith("dc_")


def test_thread_for_channel_unique():
    from olano.channels.discord_channel import _CHANNEL_THREADS, _thread_for_channel

    _CHANNEL_THREADS.clear()
    t1 = _thread_for_channel("ch_a")
    t2 = _thread_for_channel("ch_b")
    assert t1 != t2


async def test_discord_send_channel():
    from olano.channels.discord_channel import DiscordChannel

    ch = DiscordChannel({"token": "fake-token"})
    mock_channel = AsyncMock()
    mock_client = MagicMock()
    mock_client.get_channel.return_value = mock_channel
    ch._client = mock_client

    await ch.send(recipient="777", text="Test message")

    mock_channel.send.assert_called()


async def test_discord_send_fetches_user_if_no_channel():
    from olano.channels.discord_channel import DiscordChannel

    ch = DiscordChannel({"token": "fake-token"})
    mock_client = MagicMock()
    mock_client.get_channel.return_value = None  # channel not found

    mock_user = AsyncMock()
    mock_dm = AsyncMock()
    mock_user.create_dm.return_value = mock_dm
    mock_client.fetch_user = AsyncMock(return_value=mock_user)

    ch._client = mock_client

    await ch.send(recipient="888", text="DM message")

    mock_dm.send.assert_called()


async def test_discord_send_message_returns_result_with_id():
    from olano.channels.discord_channel import DiscordChannel

    ch = DiscordChannel({"token": "t"})
    sent = MagicMock()
    sent.id = 12345
    mock_channel = AsyncMock()
    mock_channel.send.return_value = sent
    mock_client = MagicMock()
    mock_client.get_channel.return_value = mock_channel
    ch._client = mock_client

    result = await ch.send_message("1", "hi")
    assert result.success is True
    assert result.message_id == "12345"


async def test_discord_send_message_unknown_target():
    from olano.channels.discord_channel import DiscordChannel

    ch = DiscordChannel({"token": "t"})
    mock_client = MagicMock()
    mock_client.get_channel.return_value = None
    mock_client.fetch_user = AsyncMock(side_effect=RuntimeError("no such user"))
    ch._client = mock_client

    result = await ch.send_message("999", "hi")
    assert result.success is False
    assert "no channel or user" in result.error


def test_discord_channel_no_token():
    from olano.channels.discord_channel import DiscordChannel

    ch = DiscordChannel({})
    assert ch._client is None
