"""Adapters retrieve the attachment on a message a reply points at.

The reported failure was on WhatsApp — replying to a photo reached the agent
as ``[replying to an earlier message] (no text — media or unavailable)`` — but
every adapter that captured ``reply_to_text`` had the same hole: a photo with
no caption has no text to capture, so the thing the message was *about* never
arrived. WhatsApp's half is pinned in ``test_reply_context_adapters.py`` (the
bridge does the decryption there); this covers the two adapters that already
hold the file and only had to fetch it.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from olano.platforms.base import MessageEvent, MessageType
from olano.platforms.config import PlatformConfig
from olano.platforms.discord import DiscordAdapter
from olano.platforms.telegram import TelegramAdapter

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ───────────────────────────────── Telegram ─────────────────────────────────


class _File:
    """Stand-in for ``telegram.File`` — what ``get_file()`` resolves to."""

    def __init__(self, data: bytes, file_path: str = "photos/x.jpg", size: int | None = None):
        self._data = data
        self.file_path = file_path
        self.file_size = len(data) if size is None else size

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self._data)


class _Media:
    """Stand-in for PhotoSize / Voice / Audio / Document."""

    def __init__(self, file: _File, **attrs: Any):
        self._file = file
        for k, v in attrs.items():
            setattr(self, k, v)

    async def get_file(self) -> _File:
        return self._file


class _Quoted:
    """Duck-typed ``message.reply_to_message`` with no media by default."""

    def __init__(self, **attrs: Any):
        self.photo: list[Any] = []
        self.voice = None
        self.audio = None
        self.document = None
        for k, v in attrs.items():
            setattr(self, k, v)


def _telegram() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="123:abc"), agent_name="tester")


class TestTelegramQuotedMedia:
    async def test_quoted_photo_is_cached_onto_the_event(self) -> None:
        adapter = _telegram()
        event = MessageEvent(text="what does this say?", message_type=MessageType.TEXT)

        await adapter._attach_quoted_media(
            event, MagicMock(reply_to_message=_Quoted(photo=[_Media(_File(_PNG))]))
        )

        assert len(event.reply_to_media_urls) == 1
        assert Path(event.reply_to_media_urls[0]).read_bytes() == _PNG
        assert event.reply_to_media_types == ["image/jpg"]

    async def test_extension_follows_the_telegram_file_path(self) -> None:
        adapter = _telegram()
        event = MessageEvent(text="", message_type=MessageType.TEXT)

        await adapter._attach_quoted_media(
            event,
            MagicMock(
                reply_to_message=_Quoted(photo=[_Media(_File(_PNG, file_path="photos/file_7.png"))])
            ),
        )

        assert event.reply_to_media_urls[0].endswith(".png")
        assert event.reply_to_media_types == ["image/png"]

    async def test_quoted_voice_note_is_cached_as_audio(self) -> None:
        adapter = _telegram()
        event = MessageEvent(text="what did she say?", message_type=MessageType.TEXT)

        await adapter._attach_quoted_media(
            event,
            MagicMock(reply_to_message=_Quoted(voice=_Media(_File(b"ogg-bytes", "voice/a.oga")))),
        )

        assert event.reply_to_media_types == ["audio/ogg"]
        assert Path(event.reply_to_media_urls[0]).read_bytes() == b"ogg-bytes"

    async def test_oversized_quoted_media_is_skipped(self) -> None:
        # A quoted attachment is a side-quest to the message the user sent —
        # it must never stall the turn or blow the Bot API's getFile limit.
        adapter = _telegram()
        event = MessageEvent(text="x", message_type=MessageType.TEXT)
        huge = _File(_PNG, size=adapter.QUOTED_MEDIA_MAX_BYTES + 1)

        await adapter._attach_quoted_media(
            event, MagicMock(reply_to_message=_Quoted(photo=[_Media(huge)]))
        )

        assert event.reply_to_media_urls == []

    async def test_download_failure_never_sinks_the_message(self) -> None:
        adapter = _telegram()
        event = MessageEvent(text="still answer me", message_type=MessageType.TEXT)
        broken = _Media(_File(_PNG))
        broken.get_file = AsyncMock(side_effect=RuntimeError("file_id expired"))  # type: ignore[method-assign]

        await adapter._attach_quoted_media(
            event, MagicMock(reply_to_message=_Quoted(photo=[broken]))
        )

        assert event.reply_to_media_urls == []
        assert event.text == "still answer me"

    async def test_text_only_quote_downloads_nothing(self) -> None:
        adapter = _telegram()
        event = MessageEvent(text="yes", message_type=MessageType.TEXT)

        await adapter._attach_quoted_media(event, MagicMock(reply_to_message=_Quoted()))

        assert event.reply_to_media_urls == []

    async def test_non_reply_downloads_nothing(self) -> None:
        adapter = _telegram()
        event = MessageEvent(text="hi", message_type=MessageType.TEXT)

        await adapter._attach_quoted_media(event, MagicMock(reply_to_message=None))

        assert event.reply_to_media_urls == []


# ────────────────────────────────── Discord ─────────────────────────────────


class _Attachment:
    def __init__(self, content_type: str | None, data: bytes = _PNG, filename: str = "a.png"):
        self.content_type = content_type
        self.filename = filename
        self.url = f"https://cdn.discordapp.com/attachments/{filename}"
        self._data = data

    async def read(self) -> bytes:
        return self._data


def _discord() -> DiscordAdapter:
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"), agent_name="tester")
    adapter._client = MagicMock()
    return adapter


class TestDiscordQuotedMedia:
    async def test_quoted_image_is_cached(self) -> None:
        adapter = _discord()
        quoted = MagicMock(attachments=[_Attachment("image/png")])

        urls, mimes = await adapter._cache_quoted_attachments(quoted)

        assert mimes == ["image/png"]
        assert Path(urls[0]).read_bytes() == _PNG

    async def test_non_media_attachments_are_left_alone(self) -> None:
        # A quote should never quietly pull an archive into the turn; only the
        # kinds an agent can act on from a path are fetched.
        adapter = _discord()
        quoted = MagicMock(attachments=[_Attachment("application/zip", b"PK", "x.zip")])

        urls, mimes = await adapter._cache_quoted_attachments(quoted)

        assert (urls, mimes) == ([], [])

    async def test_gallery_is_capped(self) -> None:
        adapter = _discord()
        quoted = MagicMock(
            attachments=[_Attachment("image/png") for _ in range(10)],
        )

        urls, _ = await adapter._cache_quoted_attachments(quoted)

        assert len(urls) == adapter.QUOTED_ATTACHMENT_LIMIT

    async def test_deleted_reference_has_no_attachments(self) -> None:
        # ``message.reference.resolved`` is a DeletedReferencedMessage when the
        # quoted post is gone — it carries no ``attachments`` at all.
        adapter = _discord()

        assert await adapter._cache_quoted_attachments(object()) == ([], [])

    async def test_one_bad_attachment_does_not_lose_the_others(self, monkeypatch) -> None:
        adapter = _discord()
        good = _Attachment("image/png")
        bad = _Attachment("image/png", filename="bad.png")
        bad.read = AsyncMock(side_effect=RuntimeError("410 Gone"))  # type: ignore[method-assign]

        async def boom(url: str, **kw: Any) -> str:
            raise RuntimeError("410 Gone")

        monkeypatch.setattr("olano.platforms.discord.cache_image_from_url", boom)
        urls, _ = await adapter._cache_quoted_attachments(MagicMock(attachments=[bad, good]))

        assert len(urls) == 1
