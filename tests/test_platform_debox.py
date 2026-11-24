"""Tests for the DeBox platform adapter.

Covers both transports without touching the network: the Bot-API polling
path (update normalisation, mention/allowlist gating, sendMessage/edit
payloads, markdown→HTML) and the OpenPlatform webhook path (X-API-KEY
verification, group/DM chat_id routing, dedupe, send payloads).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from olano.platforms.base import MessageType, SendResult
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.debox import (
    DeBoxAdapter,
    _has_mention,
    _markdown_to_html,
    check_debox_requirements,
)


class FakeRequest:
    """Minimal stand-in for aiohttp.web.Request for handler unit tests."""

    def __init__(self, body: bytes, headers: dict | None = None, content_length=None):
        self._body = body
        self.headers = headers or {}
        self.content_length = content_length if content_length is not None else len(body)

    async def read(self) -> bytes:
        return self._body


def _adapter(*, api_key: str = "app-key", webhook_key: str = "hook-key", **extra) -> DeBoxAdapter:
    """Webhook-transport adapter (OpenPlatform credentials, no bot token)."""
    base = {"webhook_key": webhook_key}
    base.update(extra)
    cfg = PlatformConfig(enabled=True, api_key=api_key, extra=base)
    adapter = DeBoxAdapter(cfg, agent_name="test-debox")
    # Capture dispatched events instead of running the real handler pipeline.
    adapter.handle_message = AsyncMock()  # type: ignore[method-assign]
    return adapter


def _polling_adapter(*, token: str = "bot-token", self_id: str = "botid", **extra) -> DeBoxAdapter:
    """Polling-transport adapter (Bot API token)."""
    cfg = PlatformConfig(enabled=True, token=token, extra=extra)
    adapter = DeBoxAdapter(cfg, agent_name="test-debox")
    adapter._self_id = self_id
    adapter.handle_message = AsyncMock()  # type: ignore[method-assign]
    return adapter


def _update(
    *,
    text: str = "hello",
    chat_id: str = "chat1",
    chat_type: str = "private",
    user_id: str = "uvg2p6ho",
    is_bot: bool = False,
    mention_users: list | None = None,
    update_id: int = 1,
) -> dict:
    return {
        "id": update_id,
        "message": {
            "message_id": f"m{update_id}",
            "text": text,
            "from": {"user_id": user_id, "is_bot": is_bot, "name": "Kai"},
            "chat": {"id": chat_id, "type": chat_type},
            "mention_users": mention_users or [],
        },
    }


def _post_json(payload: dict, *, key: str | None = "hook-key") -> FakeRequest:
    headers = {"X-API-KEY": key} if key is not None else {}
    return FakeRequest(json.dumps(payload).encode("utf-8"), headers=headers)


# ---------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------


def test_requirements_and_platform():
    assert check_debox_requirements() is True  # aiohttp is a project dependency
    adapter = _adapter()
    assert adapter.platform is Platform.DEBOX
    assert adapter._path == "/debox/webhook"


def test_transport_resolution():
    # Webhook Key present → webhook: DeBox's receive modes are mutually
    # exclusive server-side, and the key only exists once a Webhook URL is
    # configured for the bot.
    assert _adapter()._transport == "webhook"
    # No Webhook Key → polling, whichever credential field carries the API Key.
    assert _polling_adapter()._transport == "polling"
    assert _adapter(webhook_key="")._transport == "polling"
    # Explicit setting always wins.
    assert _polling_adapter(transport="webhook")._transport == "webhook"
    assert _adapter(transport="polling")._transport == "polling"


def test_token_and_api_key_are_one_credential():
    # BotMother issues a single API Key — accept it in either config field.
    polling_only_token = _polling_adapter()
    assert polling_only_token._api_key == "bot-token"
    webhookless_api_key = _adapter(webhook_key="")
    assert webhookless_api_key._token == "app-key"


def test_has_mention_variants():
    assert _has_mention("uvg2p6ho") is True
    assert _has_mention(["a", "b"]) is True
    assert _has_mention("") is False
    assert _has_mention(None) is False
    assert _has_mention([]) is False


def test_parse_chat_id_routing():
    adapter = _adapter()
    assert adapter._parse_chat_id("fxi3hqo5") == (False, "fxi3hqo5")
    assert adapter._parse_chat_id("group:fxi3hqo5") == (False, "fxi3hqo5")
    assert adapter._parse_chat_id("user:uvg2p6ho") == (True, "uvg2p6ho")
    assert adapter._parse_chat_id("dm:uvg2p6ho") == (True, "uvg2p6ho")


# ---------------------------------------------------------------------------
# inbound webhook
# ---------------------------------------------------------------------------


async def test_inbound_group_message_builds_event():
    adapter = _adapter()
    payload = {
        "from_user_id": "uvg2p6ho",
        "to_user_id": "botid",
        "language": "en",
        "group_id": "fxi3hqo5",
        "message": "hello bot",
        "mention_users": "botid",
        "message_raw": "@bot hello bot",
    }
    resp = await adapter._handle_webhook(_post_json(payload))
    assert resp.status == 200
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "hello bot"
    assert event.message_type is MessageType.TEXT
    assert event.source.chat_type == "group"
    assert event.source.chat_id == "fxi3hqo5"
    assert event.source.user_id == "uvg2p6ho"
    # The sender is remembered for optional @mention on replies.
    assert adapter._reply_targets["fxi3hqo5"] == "uvg2p6ho"


async def test_inbound_dm_uses_user_prefixed_chat_id():
    adapter = _adapter()
    payload = {
        "from_user_id": "uvg2p6ho",
        "group_id": "",
        "message": "hi there",
    }
    resp = await adapter._handle_webhook(_post_json(payload))
    assert resp.status == 200
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "dm"
    assert event.source.chat_id == "user:uvg2p6ho"


async def test_inbound_rejects_bad_signature():
    adapter = _adapter(webhook_key="hook-key")
    resp = await adapter._handle_webhook(
        _post_json({"from_user_id": "x", "message": "hi"}, key="wrong")
    )
    assert resp.status == 401
    adapter.handle_message.assert_not_awaited()


async def test_inbound_accepts_when_no_webhook_key_configured():
    adapter = _adapter(webhook_key="")
    resp = await adapter._handle_webhook(
        _post_json({"from_user_id": "x", "message": "hi"}, key=None)
    )
    assert resp.status == 200
    adapter.handle_message.assert_awaited_once()


async def test_inbound_ignores_empty_message():
    adapter = _adapter()
    resp = await adapter._handle_webhook(
        _post_json({"from_user_id": "x", "group_id": "g", "message": ""})
    )
    assert resp.status == 200
    adapter.handle_message.assert_not_awaited()


async def test_require_mention_filters_unmentioned_group_messages():
    adapter = _adapter(require_mention=True)
    unmentioned = {"from_user_id": "x", "group_id": "g", "message": "hello", "mention_users": ""}
    resp = await adapter._handle_webhook(_post_json(unmentioned))
    assert resp.status == 200
    adapter.handle_message.assert_not_awaited()

    mentioned = {"from_user_id": "x", "group_id": "g", "message": "hello", "mention_users": "botid"}
    resp = await adapter._handle_webhook(_post_json(mentioned))
    assert resp.status == 200
    adapter.handle_message.assert_awaited_once()


async def test_require_mention_needs_the_bot_when_bot_id_known():
    """With to_user_id (the bot) in the payload, mentioning another user doesn't count."""
    adapter = _adapter(require_mention=True)
    other = {
        "from_user_id": "x",
        "to_user_id": "botid",
        "group_id": "g",
        "message": "hey",
        "mention_users": "someoneelse",
    }
    await adapter._handle_webhook(_post_json(other))
    adapter.handle_message.assert_not_awaited()

    bot = {
        "from_user_id": "x",
        "to_user_id": "botid",
        "group_id": "g",
        "message": "hey",
        "mention_users": ["someoneelse", "botid"],
    }
    await adapter._handle_webhook(_post_json(bot))
    adapter.handle_message.assert_awaited_once()


async def test_allowed_users_blocks_unlisted_senders():
    adapter = _adapter(allowed_users=["UVG2p6ho"])  # mixed case → matching is case-insensitive
    blocked = {"from_user_id": "stranger", "group_id": "g", "message": "hi"}
    resp = await adapter._handle_webhook(_post_json(blocked))
    assert resp.status == 200  # 200 so DeBox doesn't retry, but nothing dispatched
    adapter.handle_message.assert_not_awaited()

    allowed = {"from_user_id": "uvg2p6ho", "group_id": "g", "message": "hi"}
    await adapter._handle_webhook(_post_json(allowed))
    adapter.handle_message.assert_awaited_once()


async def test_dm_without_sender_is_ignored():
    adapter = _adapter()
    resp = await adapter._handle_webhook(_post_json({"group_id": "", "message": "hi"}))
    assert resp.status == 200
    adapter.handle_message.assert_not_awaited()


async def test_inbound_dedupes_retries():
    adapter = _adapter(dedup_ttl_seconds=60)
    payload = {"from_user_id": "x", "group_id": "g", "message": "same"}
    await adapter._handle_webhook(_post_json(payload))
    await adapter._handle_webhook(_post_json(payload))
    assert adapter.handle_message.await_count == 1


async def test_inbound_falls_back_to_message_raw():
    adapter = _adapter()
    payload = {"from_user_id": "x", "group_id": "g", "message": "", "message_raw": "@bot raw text"}
    await adapter._handle_webhook(_post_json(payload))
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "@bot raw text"


# ---------------------------------------------------------------------------
# outbound send
# ---------------------------------------------------------------------------


async def test_send_group_message_payload():
    adapter = _adapter()
    adapter._post = AsyncMock(return_value=SendResult(success=True, message_id="m1"))  # type: ignore[method-assign]
    result = await adapter.send("fxi3hqo5", "hi group")
    assert result.success
    path, payload = adapter._post.await_args.args
    assert path == "/openapi/send_robot_group_message"
    assert payload == {"group_id": "fxi3hqo5", "object_name": "RC:TxtMsg", "message": "hi group"}


async def test_send_dm_uses_private_endpoint():
    adapter = _adapter()
    adapter._post = AsyncMock(return_value=SendResult(success=True))  # type: ignore[method-assign]
    await adapter.send("user:uvg2p6ho", "hi dm")
    path, payload = adapter._post.await_args.args
    assert path == "/openapi/send_robot_message"
    assert payload == {"to_user_id": "uvg2p6ho", "object_name": "RC:TxtMsg", "message": "hi dm"}


async def test_send_group_mentions_sender_when_enabled():
    adapter = _adapter(mention_sender=True)
    adapter._reply_targets["fxi3hqo5"] = "uvg2p6ho"
    adapter._post = AsyncMock(return_value=SendResult(success=True))  # type: ignore[method-assign]
    await adapter.send("fxi3hqo5", "answer")
    _, payload = adapter._post.await_args.args
    assert payload["to_user_id"] == "uvg2p6ho"


async def test_send_group_metadata_to_user_id_overrides():
    adapter = _adapter(mention_sender=True)
    adapter._reply_targets["fxi3hqo5"] = "uvg2p6ho"
    adapter._post = AsyncMock(return_value=SendResult(success=True))  # type: ignore[method-assign]
    await adapter.send("fxi3hqo5", "answer", metadata={"to_user_id": "override"})
    _, payload = adapter._post.await_args.args
    assert payload["to_user_id"] == "override"


async def test_send_without_api_key_fails_clearly():
    adapter = _adapter(api_key="")
    result = await adapter.send("fxi3hqo5", "hi")
    assert result.success is False
    assert "API key" in (result.error or "")


async def test_send_chunks_long_content():
    adapter = _adapter(max_message_length=10)
    adapter._post = AsyncMock(return_value=SendResult(success=True))  # type: ignore[method-assign]
    await adapter.send("fxi3hqo5", "abcdefghijklmnopqrstuvwxyz0123456789")
    assert adapter._post.await_count >= 2


async def test_send_stops_on_first_failure():
    adapter = _adapter(max_message_length=10)
    adapter._post = AsyncMock(return_value=SendResult(success=False, error="boom"))  # type: ignore[method-assign]
    result = await adapter.send("fxi3hqo5", "abcdefghijklmnopqrstuvwxyz0123456789")
    assert result.success is False
    assert adapter._post.await_count == 1


async def test_send_strips_markdown_for_plain_text_messages():
    adapter = _adapter()
    adapter._post = AsyncMock(return_value=SendResult(success=True))  # type: ignore[method-assign]
    await adapter.send("fxi3hqo5", "**bold** and [link](https://x.com)")
    _, payload = adapter._post.await_args.args
    assert payload["message"] == "bold and link"


async def test_send_keeps_content_for_rich_object_types():
    adapter = _adapter()
    adapter._post = AsyncMock(return_value=SendResult(success=True))  # type: ignore[method-assign]
    await adapter.send("fxi3hqo5", "**rich** body", metadata={"object_name": "RCD:Graphic"})
    _, payload = adapter._post.await_args.args
    assert payload["object_name"] == "RCD:Graphic"
    assert payload["message"] == "**rich** body"


async def test_get_chat_info():
    adapter = _adapter()
    assert (await adapter.get_chat_info("fxi3hqo5"))["type"] == "group"
    assert (await adapter.get_chat_info("user:uvg2p6ho"))["type"] == "dm"


# ---------------------------------------------------------------------------
# polling transport: inbound updates
# ---------------------------------------------------------------------------


async def test_polling_private_update_builds_dm_event():
    adapter = _polling_adapter()
    await adapter._process_update(_update(chat_type="private", chat_id="c9"))
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "dm"
    assert event.source.chat_id == "user:c9"
    assert event.source.user_name == "Kai"
    assert event.message_id == "m1"
    # Exact platform chat.type remembered for outbound routing.
    assert adapter._chat_types["user:c9"] == "private"


async def test_polling_group_update_builds_group_event():
    adapter = _polling_adapter()
    await adapter._process_update(_update(chat_type="group", chat_id="g7"))
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "group"
    assert event.source.chat_id == "g7"
    assert adapter._chat_types["g7"] == "group"


async def test_polling_skips_bots_and_self():
    adapter = _polling_adapter(self_id="botid")
    await adapter._process_update(_update(is_bot=True))
    await adapter._process_update(_update(user_id="botid"))
    adapter.handle_message.assert_not_awaited()


async def test_polling_enforces_allowed_users():
    adapter = _polling_adapter(allowed_users=["uvg2p6ho"])
    await adapter._process_update(_update(user_id="stranger"))
    adapter.handle_message.assert_not_awaited()
    await adapter._process_update(_update(user_id="uvg2p6ho"))
    adapter.handle_message.assert_awaited_once()


async def test_polling_require_mention_checks_bot_id():
    adapter = _polling_adapter(require_mention=True, self_id="botid")
    # Group message mentioning someone else → ignored.
    await adapter._process_update(_update(chat_type="group", mention_users=[{"user_id": "other"}]))
    adapter.handle_message.assert_not_awaited()
    # Mentioning the bot → answered.
    await adapter._process_update(_update(chat_type="group", mention_users=[{"user_id": "botid"}]))
    adapter.handle_message.assert_awaited_once()
    # DMs are never gated on mentions.
    await adapter._process_update(_update(chat_type="private"))
    assert adapter.handle_message.await_count == 2


async def test_polling_ignores_non_message_updates():
    adapter = _polling_adapter()
    await adapter._process_update({"id": 5, "callback_query": {"data": "x"}})
    adapter.handle_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# polling transport: outbound
# ---------------------------------------------------------------------------


async def test_polling_send_uses_bot_send_message():
    adapter = _polling_adapter()
    adapter._chat_types["user:c9"] = "private"
    adapter._bot_request = AsyncMock(return_value=(True, {"message_id": "m9"}, ""))  # type: ignore[method-assign]
    result = await adapter.send("user:c9", "hi **there**")
    assert result.success and result.message_id == "m9"
    method, params = adapter._bot_request.await_args.args
    assert method == "bot/sendMessage"
    assert params == {
        "chat_id": "c9",
        "chat_type": "private",
        "text": "hi <b>there</b>",
        "parse_mode": "HTML",
    }


async def test_polling_send_falls_back_to_prefix_chat_type():
    adapter = _polling_adapter()
    adapter._bot_request = AsyncMock(return_value=(True, {}, ""))  # type: ignore[method-assign]
    await adapter.send("g7", "hello")
    _, params = adapter._bot_request.await_args.args
    assert params["chat_type"] == "group"
    await adapter.send("user:u1", "hello")
    _, params = adapter._bot_request.await_args.args
    assert params["chat_type"] == "private"


async def test_polling_send_text_mode_strips_markdown():
    adapter = _polling_adapter(parse_mode="text")
    adapter._bot_request = AsyncMock(return_value=(True, {}, ""))  # type: ignore[method-assign]
    await adapter.send("g7", "**bold** stuff")
    _, params = adapter._bot_request.await_args.args
    assert params["text"] == "bold stuff"
    assert "parse_mode" not in params or params["parse_mode"] == ""


async def test_polling_send_failure_propagates_error():
    adapter = _polling_adapter()
    adapter._bot_request = AsyncMock(return_value=(False, None, "error_code=401: bad token"))  # type: ignore[method-assign]
    result = await adapter.send("g7", "hello")
    assert result.success is False
    assert "401" in (result.error or "")


async def test_polling_edit_message():
    adapter = _polling_adapter()
    adapter._chat_types["g7"] = "group"
    adapter._bot_request = AsyncMock(return_value=(True, {}, ""))  # type: ignore[method-assign]
    result = await adapter.edit_message("g7", "m3", "updated **text**")
    assert result.success
    method, params = adapter._bot_request.await_args.args
    assert method == "bot/editMessageText"
    assert params["message_id"] == "m3"
    assert params["text"] == "updated <b>text</b>"


async def test_edit_message_unsupported_in_webhook_mode():
    adapter = _adapter()
    result = await adapter.edit_message("g7", "m3", "updated")
    assert result.success is False


# ---------------------------------------------------------------------------
# markdown → DeBox HTML
# ---------------------------------------------------------------------------


def test_markdown_to_html_conversion():
    assert _markdown_to_html("**bold**") == "<b>bold</b>"
    assert _markdown_to_html("# Title") == "<b>Title</b>"
    assert _markdown_to_html("a\nb") == "a<br/>b"
    assert _markdown_to_html("[x](https://e.com)") == '<a href="https://e.com">x</a>'
    assert _markdown_to_html("*em* and `code`") == "em and code"


def test_markdown_to_html_escapes_injection():
    out = _markdown_to_html("<script>alert(1)</script> & <b>raw</b>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&lt;b&gt;raw&lt;/b&gt;" in out
