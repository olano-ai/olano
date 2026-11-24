"""Discord adapter wiring of the uniform group listen/respond modes.

Mirrors the Telegram reference coverage in tests/test_group_modes.py:
mode resolution (legacy ``require_mention`` mapping + new knobs), the
``group_respond`` gate inside ``_handle_message``, and observed-message
recording for dropped channel messages (``group_listen``).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("discord")

import discord as discord_lib

from olano.platforms.chat_directory import get_chat_directory
from olano.platforms.config import PlatformConfig
from olano.platforms.discord import DiscordAdapter

AGENT = "test-discord"
CHANNEL_ID = 555


@pytest.fixture(autouse=True)
def _clean_discord_env(monkeypatch: pytest.MonkeyPatch):
    """Pin the env fallbacks so ambient DISCORD_* settings can't skew tests."""
    for var in (
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_ALLOWED_GUILDS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
        "DISCORD_GROUP_RESPOND",
        "DISCORD_GROUP_LISTEN",
    ):
        monkeypatch.delenv(var, raising=False)


def make_adapter(extra: dict[str, Any] | None = None) -> DiscordAdapter:
    config = PlatformConfig(enabled=True, token="x", extra=extra or {})
    adapter = DiscordAdapter(config, agent_name=AGENT)
    client = MagicMock()
    bot_user = MagicMock()
    bot_user.id = 999
    client.user = bot_user
    adapter._client = client
    # Spy on both dispatch paths out of _handle_message, and never create
    # real Discord threads.
    adapter._enqueue_text_event = MagicMock()  # type: ignore[method-assign]
    adapter.handle_message = AsyncMock()  # type: ignore[method-assign]
    adapter._auto_create_thread = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return adapter


def make_group_message(
    *,
    content: str = "hello group",
    mentions: list[Any] | None = None,
    channel_id: int = CHANNEL_ID,
) -> MagicMock:
    """A server text-channel message (not a DM, not a thread)."""
    msg = MagicMock()
    msg.id = 1001
    msg.content = content
    msg.mentions = mentions or []
    msg.attachments = []
    msg.reference = None
    channel = MagicMock()
    channel.id = channel_id
    channel.name = "general"
    channel.topic = None
    channel.parent_id = None
    guild = MagicMock()
    guild.id = 42
    guild.name = "Test Guild"
    channel.guild = guild
    msg.channel = channel
    msg.guild = guild
    author = MagicMock()
    author.id = 7
    author.name = "ada"
    author.display_name = "Ada"
    author.bot = False
    msg.author = author
    return msg


async def _wait_for_observed(chat_id: str) -> list[dict[str, Any]]:
    """Poll the observed store: record_observed_event offloads its write to
    the default executor when an event loop is running (async tests)."""
    rows: list[dict[str, Any]] = []
    for _ in range(200):
        rows = get_chat_directory().recent_observed(AGENT, chat_id=chat_id)
        if rows:
            break
        await asyncio.sleep(0.02)
    return rows


# ─── Mode resolution ─────────────────────────────────────────────────────────


def test_default_modes():
    a = make_adapter({})
    assert a._discord_group_respond() == "mentions"  # legacy default: require mention
    assert a._discord_group_listen() == "off"


def test_legacy_require_mention_mapping():
    assert make_adapter({"require_mention": False})._discord_group_respond() == "all"
    assert make_adapter({"require_mention": True})._discord_group_respond() == "mentions"
    # Env fallback maps too (legacy default true handled in test_default_modes).
    assert make_adapter({"require_mention": "false"})._discord_group_respond() == "all"


def test_group_respond_wins_over_require_mention():
    a = make_adapter({"require_mention": True, "group_respond": "all"})
    assert a._discord_group_respond() == "all"
    b = make_adapter({"require_mention": False, "group_respond": "none"})
    assert b._discord_group_respond() == "none"


def test_group_listen_knob():
    assert make_adapter({"group_listen": "mentions"})._discord_group_listen() == "mentions"
    assert make_adapter({"group_listen": "all"})._discord_group_listen() == "all"
    assert make_adapter({"group_listen": "off"})._discord_group_listen() == "off"


# ─── group_respond gating in _handle_message ─────────────────────────────────


async def test_respond_none_drops_group_messages_even_mentions():
    adapter = make_adapter({"group_respond": "none"})
    await adapter._handle_message(make_group_message())
    bot = adapter._client.user
    await adapter._handle_message(make_group_message(content=f"<@{bot.id}> hi", mentions=[bot]))
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_called()


async def test_respond_none_free_response_channel_still_responds():
    # A per-channel exemption is more specific than the global mode, so the
    # free-response channel keeps answering even under group_respond="none".
    adapter = make_adapter({"group_respond": "none", "free_response_channels": [str(CHANNEL_ID)]})
    await adapter._handle_message(make_group_message())
    adapter._enqueue_text_event.assert_called_once()


async def test_respond_none_leaves_dms_alone():
    adapter = make_adapter({"group_respond": "none"})
    msg = make_group_message(content="direct hello")
    dm_channel = MagicMock(spec=discord_lib.DMChannel)
    dm_channel.id = 777
    msg.channel = dm_channel
    await adapter._handle_message(msg)
    adapter._enqueue_text_event.assert_called_once()


async def test_mentions_mode_drops_unaddressed_and_processes_mentions():
    adapter = make_adapter({})  # legacy default == "mentions"
    await adapter._handle_message(make_group_message(content="no mention here"))
    adapter._enqueue_text_event.assert_not_called()

    bot = adapter._client.user
    await adapter._handle_message(make_group_message(content=f"<@{bot.id}> hi", mentions=[bot]))
    adapter._enqueue_text_event.assert_called_once()


async def test_all_mode_processes_unaddressed_messages():
    adapter = make_adapter({"require_mention": False})  # legacy "all"
    await adapter._handle_message(make_group_message(content="no mention"))
    adapter._enqueue_text_event.assert_called_once()


# ─── group_listen recording of dropped messages ──────────────────────────────


async def test_listen_all_records_dropped_group_message():
    adapter = make_adapter({"group_listen": "all"})  # respond stays "mentions"
    await adapter._handle_message(make_group_message(content="observed only"))
    adapter._enqueue_text_event.assert_not_called()
    rows = await _wait_for_observed(str(CHANNEL_ID))
    assert rows and rows[0]["text"] == "observed only"
    assert rows[0]["sender_name"] == "Ada"


async def test_respond_none_listen_all_records():
    adapter = make_adapter({"group_respond": "none", "group_listen": "all"})
    await adapter._handle_message(make_group_message(content="none-mode observed"))
    adapter._enqueue_text_event.assert_not_called()
    rows = await _wait_for_observed(str(CHANNEL_ID))
    assert rows and rows[0]["text"] == "none-mode observed"


async def test_listen_off_records_nothing():
    adapter = make_adapter({})
    await adapter._handle_message(make_group_message(content="dropped silently"))
    adapter._enqueue_text_event.assert_not_called()
    # listen="off" never schedules a store write, so the immediate check holds.
    assert get_chat_directory().recent_observed(AGENT, chat_id=str(CHANNEL_ID)) == []


def test_maybe_record_observed_mentions_filter_inline():
    adapter = make_adapter({"group_listen": "mentions"})
    msg = make_group_message(content="hey bot")
    # No running loop in a sync test, so record_observed_event writes inline.
    adapter._maybe_record_observed(msg, mentioned=False)
    assert get_chat_directory().recent_observed(AGENT, chat_id=str(CHANNEL_ID)) == []
    adapter._maybe_record_observed(msg, mentioned=True)
    rows = get_chat_directory().recent_observed(AGENT, chat_id=str(CHANNEL_ID))
    assert [r["text"] for r in rows] == ["hey bot"]


def test_maybe_record_observed_skips_empty_text():
    adapter = make_adapter({"group_listen": "all"})
    adapter._maybe_record_observed(make_group_message(content="   "), mentioned=False)
    assert get_chat_directory().recent_observed(AGENT, chat_id=str(CHANNEL_ID)) == []
