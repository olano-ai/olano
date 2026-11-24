"""Slack adapter wiring of the uniform group listen/respond modes.

Mirrors the Telegram reference coverage in tests/test_group_modes.py:
mode resolution (legacy ``require_mention`` mapping + new knobs), the
``group_respond`` gate inside ``_handle_slack_message``, and observed-message
recording for dropped channel messages (``group_listen``).

Slack events are plain dicts, so the adapter is exercised without slack_bolt.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any
from unittest.mock import AsyncMock

import pytest

from olano.platforms.chat_directory import get_chat_directory
from olano.platforms.config import PlatformConfig
from olano.platforms.slack import SlackAdapter

AGENT = "test-slack"
CHANNEL_ID = "C123"
BOT_UID = "UBOT"
USER_ID = "U777"

_ts_counter = itertools.count(1000)


@pytest.fixture(autouse=True)
def _clean_slack_env(monkeypatch: pytest.MonkeyPatch):
    """Pin the env fallbacks so ambient SLACK_* settings can't skew tests."""
    for var in (
        "SLACK_REQUIRE_MENTION",
        "SLACK_FREE_RESPONSE_CHANNELS",
        "SLACK_ALLOW_BOTS",
        "SLACK_ALLOWED_USERS",
    ):
        monkeypatch.delenv(var, raising=False)


def make_adapter(extra: dict[str, Any] | None = None) -> SlackAdapter:
    config = PlatformConfig(enabled=True, token="xoxb-test", extra=extra or {})
    adapter = SlackAdapter(config, agent_name=AGENT)
    adapter._bot_user_id = BOT_UID
    # Cached display name — _maybe_record_observed must not call the network.
    adapter._user_name_cache[USER_ID] = "Ada"
    # Spy on the dispatch out of _handle_slack_message.
    adapter.handle_message = AsyncMock()  # type: ignore[method-assign]
    return adapter


def make_event(
    text: str = "hello channel",
    *,
    channel: str = CHANNEL_ID,
    channel_type: str = "channel",
    user: str = USER_ID,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """A Slack message event dict (channel message unless channel_type says im)."""
    event: dict[str, Any] = {
        "type": "message",
        "text": text,
        "channel": channel,
        "channel_type": channel_type,
        "user": user,
        "ts": f"{next(_ts_counter)}.000100",  # unique — beats the dedup cache
        "team": "T1",
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


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
    assert a._slack_group_respond() == "mentions"  # legacy default: require mention
    assert a._slack_group_listen() == "off"


def test_legacy_require_mention_mapping():
    assert make_adapter({"require_mention": False})._slack_group_respond() == "all"
    assert make_adapter({"require_mention": True})._slack_group_respond() == "mentions"
    # Slack's explicit-false parsing: only false-y spellings disable gating.
    assert make_adapter({"require_mention": "false"})._slack_group_respond() == "all"
    assert make_adapter({"require_mention": "banana"})._slack_group_respond() == "mentions"


def test_require_mention_env_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SLACK_REQUIRE_MENTION", "false")
    assert make_adapter({})._slack_group_respond() == "all"


def test_group_respond_wins_over_require_mention():
    a = make_adapter({"require_mention": True, "group_respond": "all"})
    assert a._slack_group_respond() == "all"
    b = make_adapter({"require_mention": False, "group_respond": "none"})
    assert b._slack_group_respond() == "none"


def test_group_listen_knob():
    assert make_adapter({"group_listen": "mentions"})._slack_group_listen() == "mentions"
    assert make_adapter({"group_listen": "all"})._slack_group_listen() == "all"
    assert make_adapter({"group_listen": "off"})._slack_group_listen() == "off"


# ─── group_respond gating in _handle_slack_message ───────────────────────────


async def test_respond_none_drops_channel_messages_even_mentions():
    adapter = make_adapter({"group_respond": "none"})
    await adapter._handle_slack_message(make_event("plain channel chatter"))
    await adapter._handle_slack_message(make_event(f"<@{BOT_UID}> ping"))
    # Even thread participation doesn't override "none".
    adapter._mentioned_threads.add("111.000")
    await adapter._handle_slack_message(make_event("thread follow-up", thread_ts="111.000"))
    adapter.handle_message.assert_not_called()


async def test_respond_none_free_response_channel_still_responds():
    # A per-channel exemption is more specific than the global mode, so the
    # free-response channel keeps answering even under group_respond="none".
    adapter = make_adapter({"group_respond": "none", "free_response_channels": [CHANNEL_ID]})
    await adapter._handle_slack_message(make_event("no mention needed"))
    adapter.handle_message.assert_called_once()


async def test_respond_none_leaves_dms_alone():
    adapter = make_adapter({"group_respond": "none"})
    await adapter._handle_slack_message(
        make_event("direct hello", channel="D999", channel_type="im")
    )
    adapter.handle_message.assert_called_once()


async def test_mentions_mode_drops_unaddressed_and_processes_mentions():
    adapter = make_adapter({})  # legacy default == "mentions"
    await adapter._handle_slack_message(make_event("no mention here"))
    adapter.handle_message.assert_not_called()

    await adapter._handle_slack_message(make_event(f"<@{BOT_UID}> hi"))
    adapter.handle_message.assert_called_once()


async def test_mentions_mode_still_honors_thread_participation():
    # A reply in a thread where the bot was previously @mentioned processes
    # without a fresh mention — unchanged legacy behavior.
    adapter = make_adapter({})
    adapter._mentioned_threads.add("222.000")
    await adapter._handle_slack_message(make_event("follow-up", thread_ts="222.000"))
    adapter.handle_message.assert_called_once()


async def test_all_mode_processes_unaddressed_messages():
    adapter = make_adapter({"require_mention": False})  # legacy "all"
    await adapter._handle_slack_message(make_event("no mention"))
    adapter.handle_message.assert_called_once()


# ─── group_listen recording of dropped messages ──────────────────────────────


async def test_listen_all_records_dropped_channel_message():
    adapter = make_adapter({"group_listen": "all"})  # respond stays "mentions"
    await adapter._handle_slack_message(make_event("observed only"))
    adapter.handle_message.assert_not_called()
    rows = await _wait_for_observed(CHANNEL_ID)
    assert rows and rows[0]["text"] == "observed only"
    assert rows[0]["sender_name"] == "Ada"
    assert rows[0]["sender_id"] == USER_ID


async def test_respond_none_listen_all_records():
    adapter = make_adapter({"group_respond": "none", "group_listen": "all"})
    await adapter._handle_slack_message(make_event("none-mode observed"))
    adapter.handle_message.assert_not_called()
    rows = await _wait_for_observed(CHANNEL_ID)
    assert rows and rows[0]["text"] == "none-mode observed"


async def test_respond_none_listen_mentions_records_only_mentions():
    adapter = make_adapter({"group_respond": "none", "group_listen": "mentions"})
    await adapter._handle_slack_message(make_event("unaddressed chatter"))
    await adapter._handle_slack_message(make_event(f"<@{BOT_UID}> addressed"))
    adapter.handle_message.assert_not_called()
    rows = await _wait_for_observed(CHANNEL_ID)
    assert [r["text"] for r in rows] == [f"<@{BOT_UID}> addressed"]


async def test_listen_off_records_nothing():
    adapter = make_adapter({})
    await adapter._handle_slack_message(make_event("dropped silently"))
    adapter.handle_message.assert_not_called()
    # listen="off" never schedules a store write, so the immediate check holds.
    assert get_chat_directory().recent_observed(AGENT, chat_id=CHANNEL_ID) == []


def test_maybe_record_observed_mentions_filter_inline():
    adapter = make_adapter({"group_listen": "mentions"})
    event = make_event("hey bot")
    # No running loop in a sync test, so record_observed_event writes inline.
    adapter._maybe_record_observed(event, channel_id=CHANNEL_ID, user_id=USER_ID, mentioned=False)
    assert get_chat_directory().recent_observed(AGENT, chat_id=CHANNEL_ID) == []
    adapter._maybe_record_observed(event, channel_id=CHANNEL_ID, user_id=USER_ID, mentioned=True)
    rows = get_chat_directory().recent_observed(AGENT, chat_id=CHANNEL_ID)
    assert [r["text"] for r in rows] == ["hey bot"]


def test_maybe_record_observed_records_thread_id():
    adapter = make_adapter({"group_listen": "all"})
    event = make_event("threaded note", thread_ts="333.000")
    adapter._maybe_record_observed(
        event, channel_id=CHANNEL_ID, user_id=USER_ID, thread_ts="333.000", mentioned=False
    )
    rows = get_chat_directory().recent_observed(AGENT, chat_id=CHANNEL_ID, thread_id="333.000")
    assert [r["text"] for r in rows] == ["threaded note"]


def test_maybe_record_observed_skips_empty_text():
    adapter = make_adapter({"group_listen": "all"})
    adapter._maybe_record_observed(
        make_event("   "), channel_id=CHANNEL_ID, user_id=USER_ID, mentioned=False
    )
    assert get_chat_directory().recent_observed(AGENT, chat_id=CHANNEL_ID) == []
