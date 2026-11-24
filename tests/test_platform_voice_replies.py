"""Voice-in / voice-out: the auto-TTS reply path, end to end per adapter.

Three separate defects made this path look "unsupported" on two platforms and
half-working on a third, and none of them were visible to a test:

* the synthesized file was always named ``.mp3``, and Telegram only renders a
  round voice bubble for Ogg/Opus — so every reply arrived as a file card;
* WhatsApp had no ``send_voice`` at all, so the base fallback posted the
  *server-side file path as text* and deleted the audio;
* WeChat prefers its own inbound transcription and then skips the audio
  download, which left the event typed TEXT — auto-TTS never even ran.

These tests pin each gate: the event must stay typed VOICE, the container must
survive to the adapter, and the adapter must reach a native send.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from olano.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.session import SessionSource
from olano.platforms.telegram import TelegramAdapter
from olano.platforms.weixin import (
    ITEM_FILE,
    ITEM_TEXT,
    ITEM_VOICE,
    _contains_voice_item,
    _message_type_from_media,
)
from olano.platforms.whatsapp import WhatsAppAdapter

# ── Telegram: the container decides bubble vs file card ─────────────────────


def _telegram() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t"), agent_name="tg")
    bot = AsyncMock()
    bot.send_voice.return_value = MagicMock(message_id=1)
    bot.send_audio.return_value = MagicMock(message_id=2)
    adapter._bot = bot
    return adapter


async def test_telegram_ogg_is_a_voice_bubble(tmp_path) -> None:
    clip = tmp_path / "tts.ogg"
    clip.write_bytes(b"OggS")
    adapter = _telegram()

    assert (await adapter.send_voice("42", str(clip))).success
    adapter._bot.send_voice.assert_awaited_once()
    adapter._bot.send_audio.assert_not_awaited()


async def test_telegram_mp3_falls_back_to_an_audio_file(tmp_path) -> None:
    """The regression: an .mp3 can only be sendAudio, never a bubble."""
    clip = tmp_path / "tts.mp3"
    clip.write_bytes(b"ID3")
    adapter = _telegram()

    assert (await adapter.send_voice("42", str(clip))).success
    adapter._bot.send_audio.assert_awaited_once()
    adapter._bot.send_voice.assert_not_awaited()


async def test_telegram_honours_as_voice_false(tmp_path) -> None:
    """An attached clip with no [[audio_as_voice]] is a file, not a bubble."""
    clip = tmp_path / "podcast.ogg"
    clip.write_bytes(b"OggS")
    adapter = _telegram()

    await adapter.send_voice("42", str(clip), as_voice=False)
    adapter._bot.send_audio.assert_awaited_once()
    adapter._bot.send_voice.assert_not_awaited()


# ── WhatsApp: a native send, not the path-as-text fallback ──────────────────


def _whatsapp() -> WhatsAppAdapter:
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={}), agent_name="wa")
    adapter._send_media_to_bridge = AsyncMock(
        return_value=SendResult(success=True, message_id="wamid")
    )
    adapter.send = AsyncMock(return_value=SendResult(success=True))
    return adapter


def test_whatsapp_overrides_send_voice() -> None:
    """The bug itself: WhatsApp inherited the "🔊 Audio: /path" text fallback."""
    assert WhatsAppAdapter.send_voice is not BasePlatformAdapter.send_voice


async def test_whatsapp_ogg_is_sent_as_push_to_talk(tmp_path) -> None:
    clip = tmp_path / "tts.ogg"
    clip.write_bytes(b"OggS")
    adapter = _whatsapp()

    assert (await adapter.send_voice("1@c.us", str(clip))).success
    _, kwargs = adapter._send_media_to_bridge.await_args
    args = adapter._send_media_to_bridge.await_args.args
    assert args[2] == "audio"
    assert kwargs["extra"] == {"ptt": True}
    adapter.send.assert_not_awaited()  # no path leaked into the chat


async def test_whatsapp_mp3_is_a_plain_audio_message(tmp_path) -> None:
    """Baileys ptt needs Ogg/Opus; an mp3 bubble would not play."""
    clip = tmp_path / "tts.mp3"
    clip.write_bytes(b"ID3")
    adapter = _whatsapp()

    await adapter.send_voice("1@c.us", str(clip))
    assert adapter._send_media_to_bridge.await_args.kwargs["extra"] == {"ptt": False}


async def test_whatsapp_caption_is_delivered_not_dropped(tmp_path) -> None:
    """WhatsApp audio messages carry no caption field."""
    clip = tmp_path / "tts.ogg"
    clip.write_bytes(b"OggS")
    adapter = _whatsapp()

    await adapter.send_voice("1@c.us", str(clip), caption="here you go")
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.kwargs["content"] == "here you go"


# ── WeChat: voice-ness survives the skipped download ────────────────────────


def test_weixin_voice_with_native_transcript_stays_typed_voice() -> None:
    """WeChat transcribed it, so no audio was downloaded — still a voice note.

    This is the root cause of "WeChat never replies with speech": with no
    ``audio/*`` entry the message classified as TEXT and the auto-TTS gate in
    ``BasePlatformAdapter`` never opened.
    """
    assert _message_type_from_media([], "你好", saw_voice=True) == MessageType.VOICE


def test_weixin_plain_text_is_still_text() -> None:
    assert _message_type_from_media([], "你好") == MessageType.TEXT
    assert _message_type_from_media([], "/help") == MessageType.COMMAND


def test_weixin_downloaded_voice_is_still_voice() -> None:
    assert _message_type_from_media(["audio/silk"], "") == MessageType.VOICE


def test_weixin_image_still_wins_over_a_quoted_voice() -> None:
    assert _message_type_from_media(["image/jpeg"], "", saw_voice=True) == MessageType.PHOTO


def test_weixin_contains_voice_item_sees_direct_and_quoted() -> None:
    assert _contains_voice_item([{"type": ITEM_VOICE, "voice_item": {"text": "hi"}}])
    assert _contains_voice_item(
        [{"type": ITEM_TEXT, "ref_msg": {"message_item": {"type": ITEM_VOICE}}}]
    )
    assert not _contains_voice_item([{"type": ITEM_TEXT}, {"type": ITEM_FILE}])


# ── The shared gate in BasePlatformAdapter ──────────────────────────────────


class _Dummy(BasePlatformAdapter):
    """Minimal adapter that records what the auto-TTS path handed it."""

    name = "dummy"

    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM, agent_name="d")
        self.voice_calls: list[str] = []
        self.texts: list[str] = []

    async def connect(self) -> bool:  # pragma: no cover - not exercised
        return True

    async def disconnect(self) -> None:  # pragma: no cover - not exercised
        return None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        self.texts.append(content)
        return SendResult(success=True, message_id="1")

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:  # pragma: no cover
        return {}

    async def send_voice(self, chat_id: str, audio_path: str, **kwargs) -> SendResult:
        self.voice_calls.append(audio_path)
        return SendResult(success=True, message_id="2")


def _event(message_type: MessageType) -> MessageEvent:
    return MessageEvent(
        text="hola",
        message_type=message_type,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1"),
        message_id="m1",
    )


@pytest.fixture()
def tts_enabled(monkeypatch, tmp_path):
    """Auto-TTS switched on, synthesizing an Ogg without touching the network."""
    import olano.speech as speech

    monkeypatch.setattr(speech, "tts_available", lambda: True)
    monkeypatch.setattr(speech, "tts_audio_format", lambda: "opus")

    def fake_synthesize(
        text: str, output_path: str, voice: str = "", audio_format: str = ""
    ) -> str:
        Path(output_path).write_bytes(b"OggS")
        return output_path

    monkeypatch.setattr(speech, "synthesize_speech", fake_synthesize)
    return speech


async def test_voice_message_gets_a_spoken_reply(tts_enabled) -> None:
    adapter = _Dummy()
    adapter.set_message_handler(AsyncMock(return_value="hello back"))

    await adapter._process_message_background(_event(MessageType.VOICE), "s1")

    assert len(adapter.voice_calls) == 1
    # The container the provider was asked for reaches the adapter intact —
    # naming it .mp3 here is exactly what cost Telegram its voice bubbles.
    assert adapter.voice_calls[0].endswith(".ogg")
    assert adapter.texts == ["hello back"]


async def test_typed_message_gets_no_spoken_reply(tts_enabled) -> None:
    adapter = _Dummy()
    adapter.set_message_handler(AsyncMock(return_value="hello back"))

    await adapter._process_message_background(_event(MessageType.TEXT), "s1")

    assert adapter.voice_calls == []


async def test_spoken_reply_cleans_up_the_temp_clip(tts_enabled) -> None:
    adapter = _Dummy()
    adapter.set_message_handler(AsyncMock(return_value="hello back"))

    await adapter._process_message_background(_event(MessageType.VOICE), "s1")

    assert not Path(adapter.voice_calls[0]).exists()
