"""Mattermost adapter wiring of the uniform group listen/respond modes.

Mirrors the Telegram reference coverage and the Discord/WhatsApp siblings:
mode resolution (legacy ``MATTERMOST_REQUIRE_MENTION`` mapping + new knobs),
the ``group_respond`` gate inside ``_handle_ws_event``, and observed-message
recording for dropped channel messages (``group_listen``).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from olano.platforms.chat_directory import get_chat_directory
from olano.platforms.config import PlatformConfig
from olano.platforms.mattermost import MattermostAdapter

AGENT = "test-mattermost"
CHANNEL_ID = "chan-town-square"
BOT_USER_ID = "bot-user-id"
BOT_USERNAME = "lionbot"

_post_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def _clean_mattermost_env(monkeypatch: pytest.MonkeyPatch):
    """Pin the env fallbacks so ambient MATTERMOST_* settings can't skew tests."""
    for var in (
        "MATTERMOST_REQUIRE_MENTION",
        "MATTERMOST_FREE_RESPONSE_CHANNELS",
        "MATTERMOST_URL",
        "MATTERMOST_TOKEN",
        "MATTERMOST_REPLY_MODE",
    ):
        monkeypatch.delenv(var, raising=False)


def make_adapter(extra: dict[str, Any] | None = None) -> MattermostAdapter:
    config = PlatformConfig(enabled=True, token="x", extra=extra or {})
    adapter = MattermostAdapter(config, agent_name=AGENT)
    adapter._bot_user_id = BOT_USER_ID
    adapter._bot_username = BOT_USERNAME
    # Spy on the dispatch path out of _handle_ws_event.
    adapter.handle_message = AsyncMock()  # type: ignore[method-assign]
    return adapter


def make_ws_event(
    *,
    text: str = "hello channel",
    channel_type: str = "O",
    channel_id: str = CHANNEL_ID,
    sender_id: str = "u-ada",
    sender_name: str = "@ada",
    root_id: str = "",
) -> dict[str, Any]:
    """A Mattermost "posted" WebSocket event (open channel by default)."""
    post = {
        "id": f"post-{next(_post_ids)}",
        "user_id": sender_id,
        "channel_id": channel_id,
        "message": text,
        "root_id": root_id,
        "file_ids": [],
    }
    return {
        "event": "posted",
        "data": {
            "post": json.dumps(post),
            "channel_type": channel_type,
            "channel_display_name": "Town Square",
            "sender_name": sender_name,
        },
    }


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
    assert a._mattermost_group_respond() == "mentions"  # legacy default: require mention
    assert a._mattermost_group_listen() == "off"


def test_legacy_require_mention_env_mapping(monkeypatch):
    monkeypatch.setenv("MATTERMOST_REQUIRE_MENTION", "false")
    assert make_adapter({})._mattermost_group_respond() == "all"
    monkeypatch.setenv("MATTERMOST_REQUIRE_MENTION", "true")
    assert make_adapter({})._mattermost_group_respond() == "mentions"


def test_group_respond_wins_over_require_mention(monkeypatch):
    monkeypatch.setenv("MATTERMOST_REQUIRE_MENTION", "true")
    assert make_adapter({"group_respond": "all"})._mattermost_group_respond() == "all"
    monkeypatch.setenv("MATTERMOST_REQUIRE_MENTION", "false")
    assert make_adapter({"group_respond": "none"})._mattermost_group_respond() == "none"


def test_group_listen_knob():
    assert make_adapter({"group_listen": "mentions"})._mattermost_group_listen() == "mentions"
    assert make_adapter({"group_listen": "all"})._mattermost_group_listen() == "all"
    assert make_adapter({"group_listen": "off"})._mattermost_group_listen() == "off"


# ─── group_respond gating in _handle_ws_event ────────────────────────────────


async def test_respond_none_drops_channel_messages_even_mentions():
    adapter = make_adapter({"group_respond": "none"})
    await adapter._handle_ws_event(make_ws_event())
    await adapter._handle_ws_event(make_ws_event(text=f"@{BOT_USERNAME} hi"))
    adapter.handle_message.assert_not_called()


async def test_respond_none_free_channel_still_responds(monkeypatch):
    # A per-channel exemption is more specific than the global mode, so the
    # free-response channel keeps answering even under group_respond="none".
    monkeypatch.setenv("MATTERMOST_FREE_RESPONSE_CHANNELS", CHANNEL_ID)
    adapter = make_adapter({"group_respond": "none"})
    await adapter._handle_ws_event(make_ws_event())
    adapter.handle_message.assert_called_once()


async def test_respond_none_leaves_dms_alone():
    adapter = make_adapter({"group_respond": "none"})
    await adapter._handle_ws_event(make_ws_event(text="direct hello", channel_type="D"))
    adapter.handle_message.assert_called_once()


async def test_mentions_mode_drops_unaddressed_and_processes_mentions():
    adapter = make_adapter({})  # legacy default == "mentions"
    await adapter._handle_ws_event(make_ws_event(text="no mention here"))
    adapter.handle_message.assert_not_called()

    await adapter._handle_ws_event(make_ws_event(text=f"@{BOT_USERNAME} hi"))
    adapter.handle_message.assert_called_once()
    event = adapter.handle_message.call_args[0][0]
    assert event.text == "hi"  # mention stripped, same as before the rework


async def test_all_mode_processes_unaddressed_messages(monkeypatch):
    monkeypatch.setenv("MATTERMOST_REQUIRE_MENTION", "false")  # legacy "all"
    adapter = make_adapter({})
    await adapter._handle_ws_event(make_ws_event(text="no mention"))
    adapter.handle_message.assert_called_once()


# ─── group_listen recording of dropped messages ──────────────────────────────


async def test_listen_all_records_dropped_channel_message():
    adapter = make_adapter({"group_listen": "all"})  # respond stays "mentions"
    await adapter._handle_ws_event(make_ws_event(text="observed only"))
    adapter.handle_message.assert_not_called()
    rows = await _wait_for_observed(CHANNEL_ID)
    assert rows and rows[0]["text"] == "observed only"
    assert rows[0]["sender_name"] == "ada"
    assert rows[0]["platform"] == "mattermost"


async def test_respond_none_listen_all_records():
    adapter = make_adapter({"group_respond": "none", "group_listen": "all"})
    await adapter._handle_ws_event(make_ws_event(text="none-mode observed"))
    adapter.handle_message.assert_not_called()
    rows = await _wait_for_observed(CHANNEL_ID)
    assert rows and rows[0]["text"] == "none-mode observed"


async def test_listen_all_records_thread_id():
    adapter = make_adapter({"group_listen": "all"})
    await adapter._handle_ws_event(make_ws_event(text="threaded", root_id="root-1"))
    rows = await _wait_for_observed(CHANNEL_ID)
    assert rows and rows[0]["thread_id"] == "root-1"


async def test_listen_off_records_nothing():
    adapter = make_adapter({})
    await adapter._handle_ws_event(make_ws_event(text="dropped silently"))
    adapter.handle_message.assert_not_called()
    # listen="off" never schedules a store write, so the immediate check holds.
    assert get_chat_directory().recent_observed(AGENT, chat_id=CHANNEL_ID) == []


def test_maybe_record_observed_mentions_filter_inline():
    adapter = make_adapter({"group_listen": "mentions"})
    post = {"id": "p1", "user_id": "u-ada", "channel_id": CHANNEL_ID, "message": "hey bot"}
    data = {"sender_name": "@ada", "channel_display_name": "Town Square"}
    # No running loop in a sync test, so record_observed_event writes inline.
    adapter._maybe_record_observed(post, data, chat_type="channel", mentioned=False)
    assert get_chat_directory().recent_observed(AGENT, chat_id=CHANNEL_ID) == []
    adapter._maybe_record_observed(post, data, chat_type="channel", mentioned=True)
    rows = get_chat_directory().recent_observed(AGENT, chat_id=CHANNEL_ID)
    assert [r["text"] for r in rows] == ["hey bot"]


def test_maybe_record_observed_skips_empty_text():
    adapter = make_adapter({"group_listen": "all"})
    post = {"id": "p2", "user_id": "u-ada", "channel_id": CHANNEL_ID, "message": "   "}
    adapter._maybe_record_observed(post, {}, chat_type="channel", mentioned=False)
    assert get_chat_directory().recent_observed(AGENT, chat_id=CHANNEL_ID) == []
