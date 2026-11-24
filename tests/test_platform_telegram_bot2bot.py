"""Tests for bot-to-bot (agent-to-agent) message gating and loop guards."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from olano.platforms.config import PlatformConfig
from olano.platforms.telegram import TelegramAdapter

SELF_BOT_ID = 111


def _adapter(**extra) -> TelegramAdapter:
    cfg = PlatformConfig(enabled=True, token="bot-token", extra=extra)
    adapter = TelegramAdapter(cfg, agent_name="test-telegram")
    bot = AsyncMock()
    bot.id = SELF_BOT_ID
    adapter._bot = bot
    return adapter


def _msg(is_bot=True, user_id=222, username="peerbot", chat_id=500, text="ping"):
    user = SimpleNamespace(
        id=user_id, is_bot=is_bot, username=username, full_name=username or "Human"
    )
    chat = SimpleNamespace(id=chat_id, type="private")
    return MagicMock(
        from_user=user,
        chat=chat,
        text=text,
        caption=None,
        message_thread_id=None,
        reply_to_message=None,
    )


def test_bot_messages_dropped_by_default():
    adapter = _adapter()
    assert adapter._should_process_message(_msg(is_bot=True)) is False


def test_human_messages_pass_by_default():
    adapter = _adapter()
    assert adapter._should_process_message(_msg(is_bot=False, username=None)) is True


def test_bot_messages_accepted_when_enabled():
    adapter = _adapter(allow_bot_messages=True)
    assert adapter._should_process_message(_msg(is_bot=True)) is True


def test_own_messages_always_dropped():
    adapter = _adapter(allow_bot_messages=True)
    assert adapter._should_process_message(_msg(is_bot=True, user_id=SELF_BOT_ID)) is False


def test_bot_username_allowlist():
    adapter = _adapter(allow_bot_messages=True, allowed_bot_usernames=["@GoodBot"])
    assert adapter._should_process_message(_msg(username="goodbot")) is True
    assert adapter._should_process_message(_msg(username="evilbot")) is False


def test_consecutive_bot_turn_cap_and_human_reset():
    adapter = _adapter(allow_bot_messages=True, bot_loop_max_turns=3)

    for _ in range(3):
        assert adapter._should_process_message(_msg()) is True
    # 4th consecutive bot turn trips the loop guard
    assert adapter._should_process_message(_msg()) is False

    # A human message in the chat resets the counter
    assert adapter._should_process_message(_msg(is_bot=False, username=None)) is True
    assert adapter._should_process_message(_msg()) is True


def test_bot_turn_counter_is_per_chat():
    adapter = _adapter(allow_bot_messages=True, bot_loop_max_turns=2)
    assert adapter._should_process_message(_msg(chat_id=1)) is True
    assert adapter._should_process_message(_msg(chat_id=1)) is True
    assert adapter._should_process_message(_msg(chat_id=1)) is False
    # An unrelated chat still has headroom
    assert adapter._should_process_message(_msg(chat_id=2)) is True


def test_bot_rate_limit_per_chat():
    adapter = _adapter(allow_bot_messages=True, bot_loop_max_turns=100, bot_rate_limit_per_minute=5)
    accepted = sum(1 for _ in range(10) if adapter._should_process_message(_msg()))
    assert accepted == 5
