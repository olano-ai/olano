"""Tests for the adapter-side progress surface.

``BasePlatformAdapter.progress_update`` / ``progress_clear`` own the message
bookkeeping; Telegram overrides the hand-off so progress takes over the
"💭 Thinking…" bubble instead of adding a second one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from olano.platforms.base import MessageEvent, ProcessingOutcome, SendResult
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.session import SessionSource
from olano.platforms.telegram import TelegramAdapter

PLACEHOLDER_ID = 900


def _adapter(**extra) -> TelegramAdapter:
    cfg = PlatformConfig(enabled=True, token="bot-token", extra=extra)
    adapter = TelegramAdapter(cfg, agent_name="test-telegram")
    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=PLACEHOLDER_ID)
    adapter._bot = bot
    return adapter


def _event(chat_id: str = "123", message_id: str = "55") -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id="777",
    )
    return MessageEvent(text="hello", source=source, message_id=message_id)


# ── Telegram hand-off ───────────────────────────────────────────────────────


async def test_progress_adopts_the_thinking_placeholder():
    adapter = _adapter()
    event = _event()
    await adapter.on_processing_start(event)
    key = adapter._thinking_key("123", "55")
    assert adapter._thinking_placeholders[key] == PLACEHOLDER_ID

    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="900"))
    assert await adapter.progress_update(event, "🧭 Working…") is True

    # Ownership transferred: the placeholder bookkeeping must forget it, or
    # send() would edit a half-finished tool log into the final reply.
    assert key not in adapter._thinking_placeholders
    assert adapter._progress_messages[adapter._progress_key(event)] == "900"
    adapter.edit_message.assert_awaited_once()


async def test_adopted_placeholder_is_not_claimed_by_the_final_reply():
    adapter = _adapter()
    event = _event()
    await adapter.on_processing_start(event)
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="900"))
    await adapter.progress_update(event, "🧭 Working…")

    assert adapter._claim_thinking_placeholder("123", "55") is None


async def test_second_update_edits_rather_than_posting_again():
    adapter = _adapter()
    event = _event()
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="900"))
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="901"))

    await adapter.progress_update(event, "first")
    await adapter.progress_update(event, "second")

    assert adapter.send.await_count == 1
    assert adapter.edit_message.await_count == 1


async def test_failed_edit_drops_the_message_so_the_next_update_reposts():
    adapter = _adapter()
    event = _event()
    adapter._progress_messages[adapter._progress_key(event)] = "900"
    adapter.edit_message = AsyncMock(return_value=SendResult(success=False, error="gone"))

    assert await adapter.progress_update(event, "text") is False
    assert adapter._progress_key(event) not in adapter._progress_messages


async def test_clear_deletes_the_progress_message():
    adapter = _adapter()
    event = _event()
    adapter._progress_messages[adapter._progress_key(event)] = "900"

    await adapter.progress_clear(event)

    adapter._bot.delete_message.assert_awaited_once()
    assert adapter._progress_messages == {}


async def test_release_keeps_the_message_but_drops_ownership():
    # Verbose mode keeps its log; the end-of-turn orphan sweep must not then
    # delete it.
    adapter = _adapter()
    event = _event()
    adapter._progress_messages[adapter._progress_key(event)] = "900"

    adapter.progress_release(event)
    await adapter.progress_clear(event)

    adapter._bot.delete_message.assert_not_awaited()
    assert adapter._progress_messages == {}


async def test_clear_is_a_no_op_without_a_progress_message():
    adapter = _adapter()
    await adapter.progress_clear(_event())
    adapter._bot.delete_message.assert_not_awaited()


async def test_business_chat_collapses_instead_of_deleting():
    # deleteMessage takes no business_connection_id, so an owner-sent business
    # message cannot be deleted — it collapses to a one-line residue instead.
    adapter = _adapter()
    event = _event(chat_id="123:biz-conn-1")
    adapter._progress_messages[adapter._progress_key(event)] = "900"
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True))

    await adapter.progress_clear(event)

    adapter._bot.delete_message.assert_not_awaited()
    adapter.edit_message.assert_awaited_once()


async def test_processing_complete_leaves_no_stranded_progress_bubble():
    adapter = _adapter()
    event = _event()
    adapter._progress_messages[adapter._progress_key(event)] = "900"

    # The generic cleanup lives in _process_message_background's finally, but
    # progress_clear is what it calls — assert the contract directly.
    await adapter.progress_clear(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    assert adapter._progress_messages == {}


# ── base-class defaults ─────────────────────────────────────────────────────


async def test_append_mode_posts_a_new_message_each_time():
    adapter = _adapter()
    adapter.PROGRESS_MODE = "append"
    event = _event()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="1"))

    await adapter.progress_update(event, "first")
    await adapter.progress_update(event, "second")

    assert adapter.send.await_count == 2
    assert adapter._progress_messages == {}


async def test_off_mode_delivers_nothing():
    adapter = _adapter()
    adapter.PROGRESS_MODE = "off"
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="1"))

    assert await adapter.progress_update(_event(), "text") is False
    adapter.send.assert_not_awaited()


async def test_progress_key_separates_concurrent_turns_in_one_chat():
    adapter = _adapter()
    assert adapter._progress_key(_event(message_id="1")) != adapter._progress_key(
        _event(message_id="2")
    )
