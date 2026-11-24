"""Secretary-mode reply pacing: the silent wait before a business reply.

Covers ``TelegramAdapter.quiet_reply_delay`` (which chats it applies to and
how it is configured) and the ``_process_message_background`` window it
drives — nothing visible may happen while the adapter is holding its breath,
and a follow-up sent during the window folds into one turn.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from olano.platforms.base import MessageEvent, MessageType
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.session import SessionSource
from olano.platforms.telegram import TelegramAdapter

CONN_ID = "bconn1"
BIZ_CHAT = f"biz:{CONN_ID}:200"


def _adapter(**extra) -> TelegramAdapter:
    cfg = PlatformConfig(enabled=True, token="bot-token", extra=extra)
    adapter = TelegramAdapter(cfg, agent_name="test-telegram")
    adapter._bot = AsyncMock()
    return adapter


def _event(chat_id: str = BIZ_CHAT, text: str = "hello", message_id: str = "42") -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id="200",
    )
    return MessageEvent(
        text=text,
        source=source,
        message_id=message_id,
        message_type=MessageType.TEXT,
    )


def _delay(**extra) -> float:
    """Resolved quiet delay for a business chat with these connector settings."""
    return _adapter(**extra).quiet_reply_delay(_event())


# ── Which chats are paced, and by how much ─────────────────────────────────


def test_business_chats_default_to_thirty_seconds():
    assert _delay(business_mode=True, business_auto_reply=True) == 30.0


def test_delay_is_configurable():
    assert _delay(business_mode=True, business_reply_delay_seconds=7.5) == 7.5


def test_delay_accepts_a_string_from_the_dashboard():
    assert _delay(business_mode=True, business_reply_delay_seconds="12") == 12.0


def test_zero_disables_the_wait():
    assert _delay(business_mode=True, business_reply_delay_seconds=0) == 0.0


def test_blank_and_garbage_values_fall_back_to_the_default():
    assert _delay(business_mode=True, business_reply_delay_seconds="") == 30.0
    assert _delay(business_mode=True, business_reply_delay_seconds="soon") == 30.0
    # A negative delay is a typo, not a request to travel back in time.
    assert _delay(business_mode=True, business_reply_delay_seconds=-5) == 0.0


def test_env_var_sets_the_default(monkeypatch):
    monkeypatch.setenv("LIONCLAW_TELEGRAM_BUSINESS_REPLY_DELAY", "45")
    assert _delay(business_mode=True) == 45.0
    # An explicit config value still wins over the environment.
    assert _delay(business_mode=True, business_reply_delay_seconds=5) == 5.0


def test_regular_chats_are_never_paced():
    adapter = _adapter(business_mode=True, business_reply_delay_seconds=30)
    # A plain chat id: a normal DM, and the owner's own briefing chat in
    # observe mode — both answer as fast as the agent can.
    assert adapter.quiet_reply_delay(_event(chat_id="123")) == 0.0


def test_business_chat_is_not_paced_when_secretary_mode_is_off():
    adapter = _adapter(business_reply_delay_seconds=30)
    assert adapter.quiet_reply_delay(_event()) == 0.0


def test_other_adapters_are_unaffected_by_default():
    from olano.platforms.base import BasePlatformAdapter

    assert BasePlatformAdapter.quiet_reply_delay(_adapter(), _event()) == 0.0


# ── The window itself ──────────────────────────────────────────────────────


async def _run_background(adapter: TelegramAdapter, event: MessageEvent, session_key: str):
    adapter._active_sessions[session_key] = asyncio.Event()
    return await adapter._process_message_background(event, session_key)


async def test_nothing_visible_happens_until_the_wait_elapses():
    """No typing status and no "thinking" bubble while the adapter is silent."""
    adapter = _adapter(
        business_mode=True, business_auto_reply=True, business_reply_delay_seconds=0.15
    )
    stamps: dict[str, float] = {}

    def _stamp(label):
        def _record(*_a, **_k):
            stamps.setdefault(label, time.monotonic())

        return _record

    adapter.send_typing = AsyncMock(side_effect=_stamp("typing"))
    adapter.on_processing_start = AsyncMock(side_effect=_stamp("thinking"))

    async def _handler(_event):
        stamps.setdefault("agent", time.monotonic())
        await asyncio.sleep(0.05)  # let the typing loop get a turn
        return ""

    adapter._message_handler = _handler

    began = time.monotonic()
    await _run_background(adapter, _event(), "s1")

    assert {"typing", "thinking", "agent"} <= set(stamps), stamps
    # Every visible side effect — and the agent turn itself — happens only
    # after the silent window, never during it.
    for label, at in stamps.items():
        assert at - began >= 0.15, f"{label} fired {at - began:.3f}s in, during the quiet window"


async def test_follow_up_during_the_window_becomes_one_turn():
    """A second message while we are silent must not cost a discarded turn."""
    adapter = _adapter(business_mode=True, business_auto_reply=True)
    first = _event(text="hi", message_id="1")
    second = _event(text="are you there?", message_id="2")

    interrupt = asyncio.Event()
    interrupt.set()  # a follow-up already landed
    adapter._pending_messages["s1"] = second

    handoff = await adapter._wait_quietly(first, "s1", interrupt, 30.0)

    assert handoff is second
    assert handoff.text == "hi\nare you there?"  # chronological order
    assert "s1" not in adapter._pending_messages


async def test_quiet_window_returns_none_when_undisturbed():
    adapter = _adapter(business_mode=True)
    interrupt = asyncio.Event()
    assert await adapter._wait_quietly(_event(), "s1", interrupt, 0.01) is None


async def test_media_follow_ups_use_the_normal_interrupt_path():
    """Photo bursts have their own merge logic — don't second-guess it here."""
    adapter = _adapter(business_mode=True)
    first = _event(text="hi", message_id="1")
    photo = _event(text="", message_id="2")
    photo.message_type = MessageType.PHOTO
    photo.media_urls = ["http://example.invalid/p.jpg"]

    interrupt = asyncio.Event()
    interrupt.set()
    adapter._pending_messages["s1"] = photo

    assert await adapter._wait_quietly(first, "s1", interrupt, 30.0) is None
    # Left untouched for the ordinary pending-message handling.
    assert adapter._pending_messages["s1"] is photo


async def test_a_broken_delay_hook_never_swallows_the_message():
    adapter = _adapter()
    adapter.quiet_reply_delay = MagicMock(side_effect=RuntimeError("boom"))
    adapter._message_handler = AsyncMock(return_value="")
    adapter.send_typing = AsyncMock()

    await _run_background(adapter, _event(chat_id="123"), "s1")

    adapter._message_handler.assert_awaited_once()
