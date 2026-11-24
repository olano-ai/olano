"""Adapters populate the reply fields the renderer reads.

``platforms/reply_context`` only renders what an adapter put on the event, so
the renderer passing its own tests proves nothing about the reported bug. These
pin the producing half: a WhatsApp long-press reply and a Telegram
``reply_to_message`` must both arrive as ``reply_to_message_id`` +
``reply_to_text`` on the MessageEvent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from telegram import Chat, Message, User

from olano.platforms.base import MessageType
from olano.platforms.config import PlatformConfig
from olano.platforms.reply_context import render_reply_context
from olano.platforms.telegram import TelegramAdapter
from olano.platforms.whatsapp import WhatsAppAdapter
from olano.security.untrusted_ingress import UNTRUSTED_TAG


def _adapter(tmp_path, monkeypatch) -> WhatsAppAdapter:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("WHATSAPP_REQUIRE_MENTION", raising=False)
    return WhatsAppAdapter(PlatformConfig(enabled=True), agent_name="tester")


def _msg(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "messageId": "MSG1",
        "chatId": "12345@s.whatsapp.net",
        "chatName": "Ana",
        "isGroup": False,
        "senderId": "12345",
        "senderName": "Ana",
        "body": "does this still apply?",
        "hasMedia": False,
        "mediaType": "",
        "mediaUrls": [],
        "ts": 1,
        "botIds": [],
        "mentionedIds": [],
        "quotedParticipant": "",
    }
    base.update(over)
    return base


class TestWhatsAppQuotedMedia:
    """Replying to a photo must hand the agent the photo.

    The bridge decrypts the quoted attachment (only it can — WhatsApp serves
    media encrypted) and reports absolute paths on ``quotedMediaUrls``; the
    adapter's job is to type them and put them on the event, where
    ``build_agent_message`` folds them into the turn.
    """

    async def test_quoted_media_paths_and_mimes_reach_the_event(
        self, tmp_path, monkeypatch
    ) -> None:
        adapter = _adapter(tmp_path, monkeypatch)
        event = await adapter._build_message_event(
            _msg(
                quotedId="MSG0",
                quotedText="",
                quotedMediaType="image/jpeg",
                quotedMediaMime="image/jpeg",
                quotedMediaUrls=["/var/media/img_abc123.jpg"],
            )
        )
        assert event is not None
        assert event.reply_to_media_urls == ["/var/media/img_abc123.jpg"]
        assert event.reply_to_media_types == ["image/jpeg"]

    async def test_mime_comes_off_the_extension_the_bridge_chose(
        self, tmp_path, monkeypatch
    ) -> None:
        # Same rule as the message's own media: the file on disk is what the
        # transcriber opens, and `mediaType` is coarse ("audio/mpeg" for every
        # voice note). A .ogg file is audio/ogg whatever the sender declared.
        adapter = _adapter(tmp_path, monkeypatch)
        event = await adapter._build_message_event(
            _msg(
                quotedId="MSG0",
                quotedMediaType="audio/mpeg",
                quotedMediaMime="audio/mpeg",
                quotedMediaUrls=["/var/media/audio_abc123.ogg"],
            )
        )
        assert event is not None
        assert event.reply_to_media_types == ["audio/ogg"]

    async def test_reply_to_text_message_carries_no_quoted_media(
        self, tmp_path, monkeypatch
    ) -> None:
        adapter = _adapter(tmp_path, monkeypatch)
        event = await adapter._build_message_event(_msg(quotedId="MSG0", quotedText="hi"))
        assert event is not None
        assert event.reply_to_media_urls == []
        assert event.reply_to_media_types == []

    async def test_old_bridge_without_the_fields_degrades_quietly(
        self, tmp_path, monkeypatch
    ) -> None:
        # A bridge predating this feature sends no quoted* media keys at all.
        # The reply still renders; it just cannot say what was in it.
        adapter = _adapter(tmp_path, monkeypatch)
        event = await adapter._build_message_event(_msg(quotedId="MSG0"))
        assert event is not None
        assert event.reply_to_message_id == "MSG0"
        assert event.reply_to_media_urls == []

    async def test_declared_media_without_a_file_is_not_invented(
        self, tmp_path, monkeypatch
    ) -> None:
        # Oversized, expired or failed download: the bridge reports the kind
        # but hands over no path. Claiming "attached below" with nothing
        # attached is worse than the honest "unavailable" wording.
        adapter = _adapter(tmp_path, monkeypatch)
        event = await adapter._build_message_event(
            _msg(quotedId="MSG0", quotedMediaType="video/mp4")
        )
        assert event is not None
        assert event.reply_to_media_urls == []
        block = render_reply_context(event, is_trusted=True, allowlist_configured=False)
        assert block == "[replying to an earlier message] (no text — media or unavailable)"

    async def test_rendered_block_names_the_quoted_photo(self, tmp_path, monkeypatch) -> None:
        # End to end over the real adapter: the exact shape a user sees when
        # they long-press a caption-less photo and ask about it.
        adapter = _adapter(tmp_path, monkeypatch)
        event = await adapter._build_message_event(
            _msg(
                body="Este",
                quotedId="MSG0",
                quotedText="",
                quotedMediaType="image/jpeg",
                quotedMediaUrls=["/var/media/img_abc123.jpg"],
            )
        )
        assert event is not None
        block = render_reply_context(event, is_trusted=True, allowlist_configured=False)
        assert block == "[replying to an earlier message] (a photo, attached below)"


class TestWhatsAppQuotedReply:
    async def test_quoted_fields_reach_the_event(self, tmp_path, monkeypatch) -> None:
        adapter = _adapter(tmp_path, monkeypatch)
        event = await adapter._build_message_event(
            _msg(quotedId="MSG0", quotedText="We are moving the deploy to Friday.")
        )
        assert event is not None
        assert event.reply_to_message_id == "MSG0"
        assert event.reply_to_text == "We are moving the deploy to Friday."

    async def test_plain_message_has_no_reply_context(self, tmp_path, monkeypatch) -> None:
        adapter = _adapter(tmp_path, monkeypatch)
        event = await adapter._build_message_event(_msg())
        assert event is not None
        assert event.reply_to_message_id is None
        assert event.reply_to_text is None

    async def test_empty_bridge_values_normalize_to_none(self, tmp_path, monkeypatch) -> None:
        # The bridge sends '' rather than omitting the key when a message is
        # not a reply; '' must not read as "a reply with no text".
        adapter = _adapter(tmp_path, monkeypatch)
        event = await adapter._build_message_event(_msg(quotedId="", quotedText=""))
        assert event is not None
        assert event.reply_to_message_id is None
        assert event.reply_to_text is None


class TestTelegramQuotedReply:
    """The reported bug: a Telegram reply reached the agent with no referent.

    Drives the real ``_build_message_event`` with real PTB objects, so this
    fails if the adapter's extraction is changed or removed.
    """

    @staticmethod
    def _event(quoted: Message | None) -> Any:
        chat = Chat(id=-100123, type="supergroup", title="Builders")
        user = User(id=777, first_name="Ana", is_bot=False, username="ana")
        message = Message(
            message_id=99,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="does this still apply?",
            reply_to_message=quoted,
        )
        adapter = TelegramAdapter(
            PlatformConfig(enabled=True, token="123:abc"), agent_name="tester"
        )
        return adapter._build_message_event(message, MessageType.TEXT)

    @staticmethod
    def _quoted(**over: Any) -> Message:
        base: dict[str, Any] = {
            "message_id": 42,
            "date": datetime.now(timezone.utc),
            "chat": Chat(id=-100123, type="supergroup", title="Builders"),
            "text": None,
        }
        base.update(over)
        return Message(**base)

    def test_text_reply_carries_the_quoted_text(self) -> None:
        event = self._event(self._quoted(text="We are moving the deploy to Friday."))
        assert event.reply_to_message_id == "42"
        assert event.reply_to_text == "We are moving the deploy to Friday."

    def test_caption_reply_falls_back_to_caption(self) -> None:
        # Replying to a photo: no .text, but the caption is the content.
        event = self._event(self._quoted(caption="on the roof"))
        assert event.reply_to_text == "on the roof"

    def test_media_only_reply_keeps_the_id(self) -> None:
        # No text and no caption. The id alone still tells the renderer a reply
        # happened, which is what it announces.
        event = self._event(self._quoted())
        assert event.reply_to_message_id == "42"
        assert event.reply_to_text is None

    def test_non_reply_has_no_reply_context(self) -> None:
        event = self._event(None)
        assert event.reply_to_message_id is None
        assert event.reply_to_text is None

    def test_rendered_block_reaches_the_prompt_text(self) -> None:
        # End to end over the real objects: adapter → renderer. This is the
        # exact shape the agent was missing when a user replied in Telegram.
        # The chat is a supergroup, so the quote is enveloped even though the
        # sender is trusted — a quote's author need not be its sender.
        event = self._event(self._quoted(text="We are moving the deploy to Friday."))
        block = render_reply_context(event, is_trusted=True, allowlist_configured=False)
        assert block == (
            "[replying to an earlier message]\n"
            f"<{UNTRUSTED_TAG}>\n"
            "> We are moving the deploy to Friday.\n"
            f"</{UNTRUSTED_TAG}>"
        )
