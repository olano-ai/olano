"""Tests for Telegram Business "secretary mode" on the platform adapter.

Covers: chat-ref namespacing, connection persistence, inbound routing
(observe vs auto-reply), rights gating, owner/self filtering, the customer
allowlist, and business_connection_id threading on outbound sends.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from olano.platforms.config import PlatformConfig
from olano.platforms.telegram import TelegramAdapter

CONN_ID = "bconn1"
OWNER_ID = 100
OWNER_CHAT_ID = 100  # private chat between owner and the bot
CUSTOMER_ID = 200
CUSTOMER_CHAT_ID = 200  # owner<->customer business chat


def _adapter(monkeypatch=None, tmp_path=None, **extra) -> TelegramAdapter:
    if monkeypatch is not None and tmp_path is not None:
        monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    cfg = PlatformConfig(enabled=True, token="bot-token", extra=extra)
    adapter = TelegramAdapter(cfg, agent_name="test-telegram")
    adapter._bot = AsyncMock()
    return adapter


def _connection_entry(can_reply: bool = True, is_enabled: bool = True) -> dict:
    return {
        "id": CONN_ID,
        "user_id": str(OWNER_ID),
        "user_name": "Olga Owner",
        "username": "olga",
        "user_chat_id": str(OWNER_CHAT_ID),
        "is_enabled": is_enabled,
        "date": None,
        "rights": {"can_reply": can_reply, "can_read_messages": True},
    }


def _biz_message(text="hi", from_id=CUSTOMER_ID, from_name="Carla Customer"):
    user = SimpleNamespace(
        id=from_id, full_name=from_name, username=None, first_name=from_name, is_bot=False
    )
    chat = SimpleNamespace(id=CUSTOMER_CHAT_ID, type="private", title=None, full_name=from_name)
    return MagicMock(
        text=text,
        caption=None,
        from_user=user,
        chat=chat,
        message_id=42,
        business_connection_id=CONN_ID,
        message_thread_id=None,
        reply_to_message=None,
        date=None,
        forum_topic_created=None,
    )


def _biz_update(msg=None, edited=None):
    return MagicMock(business_message=msg, edited_business_message=edited, update_id=7)


# ── chat-ref namespacing ────────────────────────────────────────────────


def test_parse_chat_ref_roundtrip():
    plain, conn = TelegramAdapter._parse_chat_ref(f"biz:{CONN_ID}:12345")
    assert (plain, conn) == ("12345", CONN_ID)


def test_parse_chat_ref_plain_passthrough():
    assert TelegramAdapter._parse_chat_ref("12345") == ("12345", None)
    assert TelegramAdapter._parse_chat_ref("-1001234") == ("-1001234", None)


def test_parse_chat_ref_malformed_prefix_left_intact():
    plain, conn = TelegramAdapter._parse_chat_ref("biz:no-chat-part")
    assert conn is None
    assert plain == "biz:no-chat-part"


# ── connection tracking + persistence ───────────────────────────────────


async def test_business_connection_update_persisted(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True)
    rights = MagicMock()
    rights.to_dict.return_value = {"can_reply": True, "can_read_messages": True}
    conn = SimpleNamespace(
        id=CONN_ID,
        user=SimpleNamespace(id=OWNER_ID, full_name="Olga Owner", username="olga"),
        user_chat_id=OWNER_CHAT_ID,
        is_enabled=True,
        date=None,
        rights=rights,
    )
    update = MagicMock(business_connection=conn)

    await adapter._handle_business_connection(update, MagicMock())

    entry = adapter._business_connections[CONN_ID]
    assert entry["user_chat_id"] == str(OWNER_CHAT_ID)
    assert entry["rights"]["can_reply"] is True

    state_file = tmp_path / "run" / "telegram-business-test-telegram.json"
    on_disk = json.loads(state_file.read_text())
    assert on_disk[CONN_ID]["is_enabled"] is True

    # A fresh adapter for the same agent loads the persisted state.
    reloaded = _adapter(monkeypatch, tmp_path, business_mode=True)
    assert reloaded._business_connections[CONN_ID]["user_chat_id"] == str(OWNER_CHAT_ID)


# ── inbound routing ─────────────────────────────────────────────────────


async def test_business_message_dropped_when_mode_off(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path)  # business_mode defaults off
    adapter.handle_message = AsyncMock()

    await adapter._handle_text_message(_biz_update(msg=_biz_message()), MagicMock())

    adapter.handle_message.assert_not_awaited()


async def test_edited_business_message_ignored(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True, business_auto_reply=True)
    adapter._business_connections[CONN_ID] = _connection_entry()
    adapter.handle_message = AsyncMock()

    await adapter._handle_text_message(_biz_update(msg=None, edited=_biz_message()), MagicMock())

    adapter.handle_message.assert_not_awaited()


async def test_auto_reply_routes_to_business_chat_namespace(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True, business_auto_reply=True)
    adapter._business_connections[CONN_ID] = _connection_entry(can_reply=True)
    adapter.handle_message = AsyncMock()

    await adapter._handle_text_message(_biz_update(msg=_biz_message()), MagicMock())

    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_id == f"biz:{CONN_ID}:{CUSTOMER_CHAT_ID}"
    assert event.text == "hi"


async def test_observe_mode_briefs_owner_chat(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True)  # auto_reply off
    adapter._business_connections[CONN_ID] = _connection_entry(can_reply=True)
    adapter.handle_message = AsyncMock()

    await adapter._handle_text_message(_biz_update(msg=_biz_message()), MagicMock())

    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_id == str(OWNER_CHAT_ID)  # plain owner chat, no biz ref
    assert "Carla Customer" in event.text
    assert "hi" in event.text
    assert event.message_id is None  # customer msg id doesn't exist in owner chat


async def test_auto_reply_without_can_reply_falls_back_to_observe(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True, business_auto_reply=True)
    adapter._business_connections[CONN_ID] = _connection_entry(can_reply=False)
    adapter.handle_message = AsyncMock()

    await adapter._handle_text_message(_biz_update(msg=_biz_message()), MagicMock())

    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_id == str(OWNER_CHAT_ID)


async def test_owner_own_message_recorded_as_context_in_auto_reply(monkeypatch, tmp_path):
    # The owner speaking for themselves is never customer input — but it is
    # context the agent must see, or the customer's next reply lands on an
    # agent blind to half the thread. Routed record-only into the same
    # biz-namespaced session the customer's messages run on.
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True, business_auto_reply=True)
    adapter._business_connections[CONN_ID] = _connection_entry()
    adapter.handle_message = AsyncMock()

    owner_msg = _biz_message(text="my own reply", from_id=OWNER_ID, from_name="Olga Owner")
    await adapter._handle_text_message(_biz_update(msg=owner_msg), MagicMock())

    event = adapter.handle_message.await_args.args[0]
    assert event.record_only is True
    assert event.source.chat_id == f"biz:{CONN_ID}:{CUSTOMER_CHAT_ID}"
    assert "my own reply" in event.text
    assert "Olga Owner" in event.text  # self-describing: names the owner


async def test_owner_own_message_recorded_in_observe_mode(monkeypatch, tmp_path):
    # Observe mode briefs the owner's own chat with the bot, so the owner's
    # direct reply to the customer is recorded there too.
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True)  # auto_reply off
    adapter._business_connections[CONN_ID] = _connection_entry(can_reply=True)
    adapter.handle_message = AsyncMock()

    owner_msg = _biz_message(text="I'll handle this one", from_id=OWNER_ID, from_name="Olga Owner")
    await adapter._handle_text_message(_biz_update(msg=owner_msg), MagicMock())

    event = adapter.handle_message.await_args.args[0]
    assert event.record_only is True
    assert event.source.chat_id == str(OWNER_CHAT_ID)
    assert "I'll handle this one" in event.text
    assert f"chat={CUSTOMER_CHAT_ID}" in event.text  # names which customer chat


async def test_owner_media_only_message_not_recorded(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True, business_auto_reply=True)
    adapter._business_connections[CONN_ID] = _connection_entry()
    adapter.handle_message = AsyncMock()

    owner_msg = _biz_message(text=None, from_id=OWNER_ID, from_name="Olga Owner")
    await adapter._handle_text_message(_biz_update(msg=owner_msg), MagicMock())

    adapter.handle_message.assert_not_awaited()


async def test_owner_message_in_non_allowed_chat_not_recorded(monkeypatch, tmp_path):
    # A chat the agent is not serving (customer allowlist) gets no context
    # recorded either.
    adapter = _adapter(
        monkeypatch,
        tmp_path,
        business_mode=True,
        business_auto_reply=True,
        business_allowed_customers=[999],
    )
    adapter._business_connections[CONN_ID] = _connection_entry()
    adapter.handle_message = AsyncMock()

    owner_msg = _biz_message(text="my own reply", from_id=OWNER_ID, from_name="Olga Owner")
    await adapter._handle_text_message(_biz_update(msg=owner_msg), MagicMock())

    adapter.handle_message.assert_not_awaited()


async def test_disabled_connection_dropped(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True, business_auto_reply=True)
    adapter._business_connections[CONN_ID] = _connection_entry(is_enabled=False)
    adapter.handle_message = AsyncMock()

    await adapter._handle_text_message(_biz_update(msg=_biz_message()), MagicMock())

    adapter.handle_message.assert_not_awaited()


async def test_business_customer_allowlist(monkeypatch, tmp_path):
    adapter = _adapter(
        monkeypatch,
        tmp_path,
        business_mode=True,
        business_auto_reply=True,
        business_allowed_customers=[999],
    )
    adapter._business_connections[CONN_ID] = _connection_entry()
    adapter.handle_message = AsyncMock()

    await adapter._handle_text_message(_biz_update(msg=_biz_message()), MagicMock())
    adapter.handle_message.assert_not_awaited()

    allowed_msg = _biz_message(from_id=999, from_name="VIP")
    await adapter._handle_text_message(_biz_update(msg=allowed_msg), MagicMock())
    adapter.handle_message.assert_awaited_once()


async def test_unknown_connection_fetched_from_telegram(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path, business_mode=True, business_auto_reply=True)
    rights = MagicMock()
    rights.to_dict.return_value = {"can_reply": True}
    adapter._bot.get_business_connection.return_value = SimpleNamespace(
        id=CONN_ID,
        user=SimpleNamespace(id=OWNER_ID, full_name="Olga Owner", username="olga"),
        user_chat_id=OWNER_CHAT_ID,
        is_enabled=True,
        date=None,
        rights=rights,
    )
    adapter.handle_message = AsyncMock()

    await adapter._handle_text_message(_biz_update(msg=_biz_message()), MagicMock())

    adapter._bot.get_business_connection.assert_awaited_once_with(CONN_ID)
    adapter.handle_message.assert_awaited_once()
    assert CONN_ID in adapter._business_connections


# ── outbound threading ─────────────────────────────────────────────────


async def test_send_attaches_business_connection_id():
    adapter = _adapter()
    adapter._bot.send_message.return_value = MagicMock(message_id=1)

    result = await adapter.send(f"biz:{CONN_ID}:{CUSTOMER_CHAT_ID}", "hello there")

    assert result.success
    kwargs = adapter._bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == CUSTOMER_CHAT_ID
    assert kwargs["business_connection_id"] == CONN_ID


async def test_send_plain_chat_has_no_business_connection():
    adapter = _adapter()
    adapter._bot.send_message.return_value = MagicMock(message_id=1)

    await adapter.send("123", "hello")

    kwargs = adapter._bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert kwargs["business_connection_id"] is None


async def test_send_typing_attaches_business_connection_id():
    adapter = _adapter()

    await adapter.send_typing(f"biz:{CONN_ID}:{CUSTOMER_CHAT_ID}")

    kwargs = adapter._bot.send_chat_action.await_args.kwargs
    assert kwargs["chat_id"] == CUSTOMER_CHAT_ID
    assert kwargs["business_connection_id"] == CONN_ID


async def test_reactions_skipped_in_business_chats():
    adapter = _adapter()
    assert await adapter._set_reaction(f"biz:{CONN_ID}:{CUSTOMER_CHAT_ID}", "5", "👍") is False
    adapter._bot.set_message_reaction.assert_not_awaited()


# ── "Thinking…" placeholder suppression in secretary mode ───────────────


def _biz_event(chat_id: str):
    from olano.platforms.base import MessageEvent
    from olano.platforms.config import Platform
    from olano.platforms.session import SessionSource

    return MessageEvent(
        text="hi",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, user_id="777"),
        message_id="55",
    )


async def test_thinking_bubble_suppressed_in_business_chats():
    # Secretary replies must not announce themselves as a bot.
    adapter = _adapter(business_mode=True, business_auto_reply=True)
    adapter._bot.send_message.return_value = MagicMock(message_id=900)

    await adapter.on_processing_start(_biz_event(f"biz:{CONN_ID}:{CUSTOMER_CHAT_ID}"))

    adapter._bot.send_message.assert_not_awaited()
    assert adapter._thinking_placeholders == {}


async def test_thinking_bubble_still_shows_in_normal_chats():
    # The regular chat behaviour is untouched by the business default.
    adapter = _adapter(business_mode=True, business_auto_reply=True)
    adapter._bot.send_message.return_value = MagicMock(message_id=900)

    await adapter.on_processing_start(_biz_event("123"))

    adapter._bot.send_message.assert_awaited_once()
    assert adapter._thinking_placeholders == {"123:55": 900}


async def test_business_thinking_bubble_opt_in():
    adapter = _adapter(business_mode=True, business_auto_reply=True, business_thinking_message=True)
    adapter._bot.send_message.return_value = MagicMock(message_id=901)
    chat = f"biz:{CONN_ID}:{CUSTOMER_CHAT_ID}"

    await adapter.on_processing_start(_biz_event(chat))

    adapter._bot.send_message.assert_awaited_once()
    kwargs = adapter._bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == CUSTOMER_CHAT_ID
    assert kwargs["business_connection_id"] == CONN_ID
    assert adapter._thinking_placeholders == {f"{chat}:55": 901}


async def test_business_thinking_off_does_not_disable_normal_thinking_off():
    # thinking_message=False must still suppress regular chats.
    adapter = _adapter(thinking_message=False)
    await adapter.on_processing_start(_biz_event("123"))
    adapter._bot.send_message.assert_not_awaited()
