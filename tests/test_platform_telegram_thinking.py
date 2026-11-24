"""Tests for the Telegram "Thinking…" placeholder lifecycle.

While the agent works on a reply the adapter posts a visible
"💭 Thinking…" bubble (on top of the ephemeral typing status).  When the
reply is ready, send() edits that bubble in place so it becomes the final
message; if no reply ever claims it, on_processing_complete deletes it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from olano.platforms.base import MessageEvent, ProcessingOutcome
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


def _event(chat_id: str = "123", message_id: str = "55", thread_id=None) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id="777",
        thread_id=thread_id,
    )
    return MessageEvent(text="hello", source=source, message_id=message_id)


async def test_processing_start_posts_placeholder():
    adapter = _adapter()
    event = _event()

    await adapter.on_processing_start(event)

    adapter._bot.send_message.assert_awaited_once()
    kwargs = adapter._bot.send_message.await_args.kwargs
    assert "Thinking…" in kwargs["text"]
    assert kwargs["text"].startswith("💭 <i>")
    assert kwargs["reply_to_message_id"] == 55
    assert adapter._thinking_placeholders == {"123:55": PLACEHOLDER_ID}


async def test_placeholder_disabled_via_extra():
    adapter = _adapter(thinking_message=False)

    await adapter.on_processing_start(_event())

    adapter._bot.send_message.assert_not_awaited()
    assert adapter._thinking_placeholders == {}


async def test_placeholder_custom_text():
    adapter = _adapter(thinking_message_text="Un momento <3")

    await adapter.on_processing_start(_event())

    text = adapter._bot.send_message.await_args.kwargs["text"]
    # Custom text is HTML-escaped inside the italic wrapper
    assert "Un momento &lt;3" in text


async def test_send_edits_placeholder_into_reply():
    adapter = _adapter()
    event = _event()
    await adapter.on_processing_start(event)

    result = await adapter.send("123", "here is the answer", reply_to="55")

    assert result.success
    assert result.message_id == str(PLACEHOLDER_ID)
    adapter._bot.edit_message_text.assert_awaited_once()
    edit_kwargs = adapter._bot.edit_message_text.await_args.kwargs
    assert edit_kwargs["message_id"] == PLACEHOLDER_ID
    assert "here is the answer" in edit_kwargs["text"]
    # No new message was sent beyond the placeholder itself
    assert adapter._bot.send_message.await_count == 1
    assert adapter._thinking_placeholders == {}


async def test_send_without_matching_reply_leaves_placeholder():
    adapter = _adapter()
    await adapter.on_processing_start(_event())

    # An unrelated send to the same chat (agent notification, error notice)
    # must not consume the bubble.
    await adapter.send("123", "unrelated broadcast")
    await adapter.send("123", "reply to something else", reply_to="99")

    adapter._bot.edit_message_text.assert_not_awaited()
    assert adapter._thinking_placeholders == {"123:55": PLACEHOLDER_ID}


async def test_send_falls_back_when_edit_fails():
    adapter = _adapter()
    event = _event()
    await adapter.on_processing_start(event)
    adapter._bot.edit_message_text.side_effect = RuntimeError("message to edit not found")

    result = await adapter.send("123", "the answer", reply_to="55")

    assert result.success
    # Placeholder was removed and the reply went out as a fresh message
    adapter._bot.delete_message.assert_awaited_once_with(chat_id=123, message_id=PLACEHOLDER_ID)
    assert adapter._bot.send_message.await_count == 2
    assert adapter._thinking_placeholders == {}


async def test_send_multichunk_edits_first_sends_rest():
    adapter = _adapter()
    event = _event()
    await adapter.on_processing_start(event)

    long_text = "a" * (TelegramAdapter.MAX_MESSAGE_LENGTH + 500)
    result = await adapter.send("123", long_text, reply_to="55")

    assert result.success
    adapter._bot.edit_message_text.assert_awaited_once()
    # Placeholder send + one follow-up chunk
    assert adapter._bot.send_message.await_count == 2


async def test_processing_complete_deletes_unclaimed_placeholder():
    adapter = _adapter()
    event = _event()
    await adapter.on_processing_start(event)

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._bot.delete_message.assert_awaited_once_with(chat_id=123, message_id=PLACEHOLDER_ID)
    assert adapter._thinking_placeholders == {}


async def test_processing_complete_after_claim_is_noop():
    adapter = _adapter()
    event = _event()
    await adapter.on_processing_start(event)
    await adapter.send("123", "the answer", reply_to="55")

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._bot.delete_message.assert_not_awaited()


async def test_concurrent_group_turns_get_independent_placeholders():
    adapter = _adapter()
    bot = adapter._bot
    bot.send_message.side_effect = [
        MagicMock(message_id=901),
        MagicMock(message_id=902),
    ]
    event_a = _event(message_id="55")
    event_b = _event(message_id="66")

    await adapter.on_processing_start(event_a)
    await adapter.on_processing_start(event_b)
    assert adapter._thinking_placeholders == {"123:55": 901, "123:66": 902}

    # A's reply claims only A's bubble
    result = await adapter.send("123", "answer for A", reply_to="55")
    assert result.message_id == "901"
    assert adapter._thinking_placeholders == {"123:66": 902}

    # B's turn ends without a reply — its bubble is deleted
    await adapter.on_processing_complete(event_b, ProcessingOutcome.FAILURE)
    bot.delete_message.assert_awaited_once_with(chat_id=123, message_id=902)


async def test_placeholder_send_failure_is_nonfatal():
    adapter = _adapter()
    adapter._bot.send_message.side_effect = RuntimeError("boom")

    await adapter.on_processing_start(_event())

    assert adapter._thinking_placeholders == {}
