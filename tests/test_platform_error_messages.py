"""What a messenger user reads when a turn fails.

The WhatsApp report that motivated this file: a customer asked a follow-up
question and the agent answered

    Sorry, I encountered an error (AgentError).
    API error (ReadError):
    Try again or use /new to start a fresh session.

Three separate defects in four lines. Two exception class names shown to
someone who is not an operator; a line ending on a bare colon, because
``str(httpx.ReadError())`` is the empty string and there was nothing to put
after it; and ``/new``, a slash command, offered to a person on WhatsApp. The
same customer resent the identical message and it worked first time — the
failure was a dropped socket, and nothing in the stack had retried it.

So the adapter's own fallback must stay free of anything technical, and a
transport failure must classify (``tests/test_actionable_errors.py``) rather
than fall through to it at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx

from olano.platforms.base import MessageEvent, SendResult
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.session import SessionSource
from olano.platforms.telegram import TelegramAdapter


def _adapter() -> TelegramAdapter:
    cfg = PlatformConfig(enabled=True, token="bot-token", extra={})
    adapter = TelegramAdapter(cfg, agent_name="test-telegram")
    bot = AsyncMock()
    bot.send_message.return_value = MagicMock(message_id=900)
    adapter._bot = bot
    return adapter


def _event() -> MessageEvent:
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="777")
    return MessageEvent(text="so they have quarterly and yearly?", source=source, message_id="55")


async def _reply_to_a_failing_turn(exc: Exception) -> str:
    """The text the adapter sends when the agent turn raises *exc*."""
    adapter = _adapter()
    sent: list[str] = []

    async def boom(_event):
        raise exc

    adapter._message_handler = boom
    adapter.send = AsyncMock(
        side_effect=lambda **kw: sent.append(kw["content"])
        or SendResult(success=True, message_id="1")
    )
    await adapter._process_message_background(_event(), "sess")
    assert sent, "a failed turn must never leave the sender in silence"
    return sent[-1]


async def test_an_unclassified_failure_says_nothing_technical():
    """Everything diagnosable is already in the log line the handler writes
    with ``exc_info=True``. The chat bubble only owes the reader the one thing
    they can do about it."""
    text = await _reply_to_a_failing_turn(RuntimeError("KeyError: 'choices'"))
    for leak in ("RuntimeError", "KeyError", "choices", "AgentError", "/new"):
        assert leak not in text
    assert "again" in text.lower()


async def test_a_dropped_connection_reads_as_a_light_nudge():
    """Classified now, so it goes through the shared classifier rather than the
    adapter's generic wording — and arrives with no warning sign, because
    resending the message is the entire fix."""
    text = await _reply_to_a_failing_turn(httpx.ReadError(""))
    assert "ReadError" not in text
    assert not text.startswith("⚠️")
    assert "again" in text.lower()


async def test_a_credential_failure_still_reaches_the_operator_intact():
    """The softened fallback must not swallow an error somebody has to act on:
    a dead key is not a "send it again" problem, and it keeps its marker."""
    text = await _reply_to_a_failing_turn(
        Exception("Error code: 401 - invalid_api_key: incorrect api key provided")
    )
    assert "⚠️" in text
    assert "API key" in text
