"""Tests for Telegram channel adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def test_telegram_channel_imports():
    from olano.channels.telegram_channel import TelegramChannel

    assert TelegramChannel.name == "telegram"
    assert TelegramChannel.use_utf16_length is True
    assert TelegramChannel.max_message_length == 4096


def test_telegram_config_overrides_max_length():
    from olano.channels.telegram_channel import TelegramChannel

    ch = TelegramChannel({"max_message_length": 2048})
    assert ch.max_message_length == 2048


def test_thread_for_chat_persistent():
    from olano.channels.telegram_channel import _CHAT_THREADS, _thread_for_chat

    _CHAT_THREADS.clear()
    tid1 = _thread_for_chat("12345")
    tid2 = _thread_for_chat("12345")
    assert tid1 == tid2
    assert tid1.startswith("tg_")


def test_thread_for_chat_unique_per_chat():
    from olano.channels.telegram_channel import _CHAT_THREADS, _thread_for_chat

    _CHAT_THREADS.clear()
    tid_a = _thread_for_chat("chat_a")
    tid_b = _thread_for_chat("chat_b")
    assert tid_a != tid_b


async def test_telegram_send_calls_bot():
    from olano.channels.telegram_channel import TelegramChannel

    ch = TelegramChannel({"token": "fake-token"})
    mock_bot = AsyncMock()
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    ch._app = mock_app

    await ch.send(recipient="99999", text="Hello from test")

    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args
    assert call_kwargs.kwargs["chat_id"] == 99999


async def test_telegram_send_message_returns_result_with_id():
    from olano.channels.telegram_channel import TelegramChannel

    ch = TelegramChannel({"token": "t"})
    returned = MagicMock()
    returned.message_id = 4242
    mock_bot = AsyncMock()
    mock_bot.send_message.return_value = returned
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    ch._app = mock_app

    result = await ch.send_message("1234", "hello")
    assert result.success is True
    assert result.message_id == "4242"


async def test_telegram_markdown_fallback_plain_on_entity_error():
    from olano.channels.telegram_channel import TelegramChannel

    ch = TelegramChannel({"token": "t"})
    first_err = RuntimeError("can't parse entities: unexpected *")
    good = MagicMock()
    good.message_id = 99
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = [first_err, good]
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    ch._app = mock_app

    result = await ch.send_message("1", "bad *markdown")
    assert result.success is True
    assert mock_bot.send_message.await_count == 2
    # Second call should drop parse_mode.
    second_call = mock_bot.send_message.await_args_list[1]
    assert "parse_mode" not in second_call.kwargs


async def test_telegram_send_message_classifies_connection_error():
    from olano.channels.telegram_channel import TelegramChannel

    ch = TelegramChannel({"token": "t"})
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = RuntimeError("ConnectError: network dropped")
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    ch._app = mock_app

    result = await ch.send_message("1", "hi")
    assert result.success is False
    assert result.retryable is True


def test_telegram_channel_no_token():
    from olano.channels.telegram_channel import TelegramChannel

    ch = TelegramChannel({})
    # start() should raise if no token
    assert ch._app is None


async def test_telegram_watchdog_starts_even_if_bootstrap_fails(monkeypatch):
    """A DNS failure during start() must still leave the watchdog running.

    Regression guard: the watchdog was the last line of start(), so a raise in
    initialize()/start_polling() (e.g. api.telegram.org momentarily unresolvable
    during a single-agent restart) aborted start() before the watchdog existed —
    leaving the channel attached but "green but dead" with nothing to retry it.
    """
    import telegram.ext as tgext

    from olano.channels.telegram_channel import TelegramChannel

    ch = TelegramChannel({"token": "fake-token"})

    # A mock Application whose network bootstrap fails like a DNS error.
    mock_app = MagicMock()
    mock_app.initialize = AsyncMock(
        side_effect=RuntimeError("nodename nor servname provided, or not known")
    )
    mock_app.bot = AsyncMock()

    builder = MagicMock()
    builder.token.return_value = builder
    builder.concurrent_updates.return_value = builder
    builder.build.return_value = mock_app
    monkeypatch.setattr(tgext.Application, "builder", staticmethod(lambda: builder))

    with pytest.raises(RuntimeError):
        await ch.start()

    # The bootstrap raised, but the watchdog must be running so the channel
    # can self-heal once DNS recovers.
    assert ch._watchdog_task is not None
    ch._watchdog_task.cancel()


async def test_telegram_health_check_flags_stopped_updater():
    """A half-started channel (poller not running) must read as unhealthy."""
    from olano.channels.telegram_channel import TelegramChannel

    ch = TelegramChannel({"token": "t"})
    mock_app = MagicMock()
    mock_app.updater.running = False  # start_polling() never completed
    mock_app.bot = AsyncMock()
    ch._app = mock_app

    with pytest.raises(RuntimeError):
        await ch._health_check()
    # get_me() must NOT mask the dead poller as healthy.
    mock_app.bot.get_me.assert_not_called()


async def test_telegram_health_check_passes_when_polling():
    from olano.channels.telegram_channel import TelegramChannel

    ch = TelegramChannel({"token": "t"})
    mock_app = MagicMock()
    mock_app.updater.running = True
    mock_app.bot = AsyncMock()
    ch._app = mock_app

    await ch._health_check()
    mock_app.bot.get_me.assert_awaited_once()
