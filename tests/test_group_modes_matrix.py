"""Matrix adapter wiring of the uniform group listen/respond modes.

Mirrors the Telegram reference coverage in tests/test_group_modes.py and the
Discord port in tests/test_group_modes_discord.py: mode resolution (legacy
``MATRIX_REQUIRE_MENTION`` env mapping + new ``group_respond``/``group_listen``
knobs), the ``group_respond`` gate inside ``_resolve_message_context``, and
observed-message recording for dropped room messages (``group_listen``).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from olano.platforms.chat_directory import get_chat_directory
from olano.platforms.config import PlatformConfig
from olano.platforms.matrix import MatrixAdapter

AGENT = "test-matrix"
ROOM = "!room:example.org"
BOT = "@lionbot:example.org"
SENDER = "@ada:example.org"


@pytest.fixture(autouse=True)
def _clean_matrix_env(monkeypatch: pytest.MonkeyPatch):
    """Pin the env fallbacks so ambient MATRIX_* settings can't skew tests."""
    for var in (
        "MATRIX_REQUIRE_MENTION",
        "MATRIX_FREE_RESPONSE_ROOMS",
        "MATRIX_AUTO_THREAD",
        "MATRIX_DM_MENTION_THREADS",
        "MATRIX_USER_ID",
        "MATRIX_REACTIONS",
        "LIONCLAW_MATRIX_TEXT_BATCH_DELAY_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)


def make_adapter(extra: dict[str, Any] | None = None) -> MatrixAdapter:
    merged: dict[str, Any] = {"user_id": BOT}
    merged.update(extra or {})
    config = PlatformConfig(enabled=True, token="syt_token", extra=merged)
    adapter = MatrixAdapter(config, agent_name=AGENT)
    # Spy on both dispatch paths out of _handle_text_message, and never let a
    # read receipt reach for a homeserver client.
    adapter._enqueue_text_event = MagicMock()  # type: ignore[method-assign]
    adapter.handle_message = AsyncMock()  # type: ignore[method-assign]
    adapter._background_read_receipt = MagicMock()  # type: ignore[method-assign]
    return adapter


async def gate_text(
    adapter: MatrixAdapter,
    body: str,
    *,
    room_id: str = ROOM,
    relates_to: dict[str, Any] | None = None,
    event_id: str = "$evt1",
) -> None:
    """Run a plaintext room message through the adapter's text handler."""
    await adapter._handle_text_message(
        room_id, SENDER, event_id, 0.0, {"body": body}, relates_to or {}
    )


def dispatched(adapter: MatrixAdapter) -> int:
    """How many messages reached processing (batched TEXT or direct)."""
    handle = adapter.handle_message
    assert isinstance(handle, AsyncMock)
    return adapter._enqueue_text_event.call_count + handle.await_count  # type: ignore[attr-defined]


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
    assert a._matrix_group_respond() == "mentions"  # MATRIX_REQUIRE_MENTION default: true
    assert a._matrix_group_listen() == "off"


def test_legacy_require_mention_env_mapping(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MATRIX_REQUIRE_MENTION", "false")
    assert make_adapter({})._matrix_group_respond() == "all"
    monkeypatch.setenv("MATRIX_REQUIRE_MENTION", "true")
    assert make_adapter({})._matrix_group_respond() == "mentions"


def test_group_respond_wins_over_require_mention(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MATRIX_REQUIRE_MENTION", "true")
    assert make_adapter({"group_respond": "all"})._matrix_group_respond() == "all"
    monkeypatch.setenv("MATRIX_REQUIRE_MENTION", "false")
    assert make_adapter({"group_respond": "none"})._matrix_group_respond() == "none"


def test_group_listen_knob():
    assert make_adapter({"group_listen": "mentions"})._matrix_group_listen() == "mentions"
    assert make_adapter({"group_listen": "all"})._matrix_group_listen() == "all"
    assert make_adapter({"group_listen": "off"})._matrix_group_listen() == "off"


# ─── group_respond gating in _resolve_message_context ────────────────────────


async def test_respond_none_drops_room_messages_even_mentions():
    adapter = make_adapter({"group_respond": "none"})
    await gate_text(adapter, "hello room")
    await gate_text(adapter, f"{BOT} hi", event_id="$evt2")
    assert dispatched(adapter) == 0


async def test_respond_none_drops_bot_thread_messages():
    # Not even a thread the bot participated in gets a turn under "none".
    adapter = make_adapter({"group_respond": "none"})
    adapter._threads.mark("$parent")
    await gate_text(
        adapter, "hello thread", relates_to={"rel_type": "m.thread", "event_id": "$parent"}
    )
    assert dispatched(adapter) == 0


async def test_respond_none_free_response_room_still_responds(monkeypatch: pytest.MonkeyPatch):
    # A per-room exemption is more specific than the global mode, so the
    # free-response room keeps answering even under group_respond="none".
    monkeypatch.setenv("MATRIX_FREE_RESPONSE_ROOMS", ROOM)
    adapter = make_adapter({"group_respond": "none"})
    await gate_text(adapter, "hello free room")
    assert dispatched(adapter) == 1


async def test_respond_none_leaves_dms_alone():
    adapter = make_adapter({"group_respond": "none"})
    adapter._dm_rooms[ROOM] = True
    await gate_text(adapter, "direct hello")
    assert dispatched(adapter) == 1


async def test_mentions_mode_drops_unaddressed_and_processes_mentions():
    adapter = make_adapter({})  # legacy default == "mentions"
    await gate_text(adapter, "no mention here")
    assert dispatched(adapter) == 0

    await gate_text(adapter, f"{BOT} hi", event_id="$evt2")
    assert dispatched(adapter) == 1


async def test_mentions_mode_keeps_bot_thread_bypass():
    # Thread-participation bypass is unchanged: an unaddressed message in a
    # thread the bot already replied in still gets a turn under "mentions".
    adapter = make_adapter({})
    adapter._threads.mark("$parent")
    await gate_text(
        adapter, "follow-up", relates_to={"rel_type": "m.thread", "event_id": "$parent"}
    )
    assert dispatched(adapter) == 1


async def test_all_mode_processes_unaddressed_messages(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MATRIX_REQUIRE_MENTION", "false")  # legacy "all"
    adapter = make_adapter({})
    await gate_text(adapter, "no mention")
    assert dispatched(adapter) == 1


# ─── group_listen recording of dropped messages ──────────────────────────────


async def test_listen_all_records_dropped_room_message():
    adapter = make_adapter({"group_listen": "all"})  # respond stays "mentions"
    await gate_text(adapter, "observed only")
    assert dispatched(adapter) == 0
    rows = await _wait_for_observed(ROOM)
    assert rows and rows[0]["text"] == "observed only"
    assert rows[0]["sender_id"] == SENDER
    assert rows[0]["sender_name"] == "ada"


async def test_respond_none_listen_all_records():
    adapter = make_adapter({"group_respond": "none", "group_listen": "all"})
    await gate_text(adapter, "none-mode observed")
    assert dispatched(adapter) == 0
    rows = await _wait_for_observed(ROOM)
    assert rows and rows[0]["text"] == "none-mode observed"


async def test_listen_off_records_nothing():
    adapter = make_adapter({})
    await gate_text(adapter, "dropped silently")
    assert dispatched(adapter) == 0
    # listen="off" never schedules a store write, so the immediate check holds.
    assert get_chat_directory().recent_observed(AGENT, chat_id=ROOM) == []


async def test_media_caption_recorded_on_drop():
    adapter = make_adapter({"group_listen": "all"})
    await adapter._handle_media_message(
        ROOM,
        SENDER,
        "$evt-media",
        0.0,
        {"body": "photo caption", "msgtype": "m.image"},
        {},
        "m.image",
    )
    assert isinstance(adapter.handle_message, AsyncMock)
    adapter.handle_message.assert_not_awaited()
    rows = await _wait_for_observed(ROOM)
    assert rows and rows[0]["text"] == "photo caption"


def test_maybe_record_observed_mentions_filter_inline():
    adapter = make_adapter({"group_listen": "mentions"})
    # No running loop in a sync test, so record_observed_event writes inline.
    adapter._maybe_record_observed(ROOM, SENDER, "hey bot", mentioned=False)
    assert get_chat_directory().recent_observed(AGENT, chat_id=ROOM) == []
    adapter._maybe_record_observed(ROOM, SENDER, "hey bot", mentioned=True)
    rows = get_chat_directory().recent_observed(AGENT, chat_id=ROOM)
    assert [r["text"] for r in rows] == ["hey bot"]


def test_maybe_record_observed_skips_empty_text():
    adapter = make_adapter({"group_listen": "all"})
    adapter._maybe_record_observed(ROOM, SENDER, "   ", mentioned=False)
    assert get_chat_directory().recent_observed(AGENT, chat_id=ROOM) == []
