"""DeBox adapter wiring of the uniform group listen/respond modes.

Mirrors the Telegram reference coverage and the Mattermost/Discord siblings:
mode resolution (legacy ``require_mention`` mapping + new knobs), the
``group_respond`` gate on both transports (Bot-API polling ``_process_update``
and OpenPlatform webhook ``_handle_webhook``), and observed-message recording
for dropped group messages (``group_listen``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from olano.platforms.chat_directory import get_chat_directory
from olano.platforms.config import PlatformConfig
from olano.platforms.debox import DeBoxAdapter

AGENT = "test-debox-modes"
GROUP_ID = "fxi3hqo5"
BOT_ID = "botid"


@pytest.fixture(autouse=True)
def _clean_debox_env(monkeypatch: pytest.MonkeyPatch):
    """Pin the env fallbacks so ambient DEBOX_* settings can't skew tests."""
    for var in (
        "DEBOX_BOT_TOKEN",
        "DEBOX_API_KEY",
        "DEBOX_API_SECRET",
        "DEBOX_APP_SECRET",
        "DEBOX_WEBHOOK_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


class FakeRequest:
    """Minimal stand-in for aiohttp.web.Request for handler unit tests."""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}
        self.content_length = len(body)

    async def read(self) -> bytes:
        return self._body


def make_polling_adapter(extra: dict[str, Any] | None = None) -> DeBoxAdapter:
    """Polling-transport adapter (Bot API token) with a known self id."""
    cfg = PlatformConfig(enabled=True, token="bot-token", extra=extra or {})
    adapter = DeBoxAdapter(cfg, agent_name=AGENT)
    adapter._self_id = BOT_ID
    adapter.handle_message = AsyncMock()  # type: ignore[method-assign]
    return adapter


def make_webhook_adapter(extra: dict[str, Any] | None = None) -> DeBoxAdapter:
    """Webhook-transport adapter (OpenPlatform credentials, no bot token)."""
    base: dict[str, Any] = {"webhook_key": "hook-key"}
    base.update(extra or {})
    cfg = PlatformConfig(enabled=True, api_key="app-key", extra=base)
    adapter = DeBoxAdapter(cfg, agent_name=AGENT)
    adapter.handle_message = AsyncMock()  # type: ignore[method-assign]
    return adapter


def make_update(
    *,
    text: str = "hello group",
    chat_id: str = GROUP_ID,
    chat_type: str = "group",
    user_id: str = "uvg2p6ho",
    mention_users: list | None = None,
    update_id: int = 1,
) -> dict:
    return {
        "id": update_id,
        "message": {
            "message_id": f"m{update_id}",
            "text": text,
            "from": {"user_id": user_id, "is_bot": False, "name": "Kai"},
            "chat": {"id": chat_id, "type": chat_type},
            "mention_users": mention_users or [],
        },
    }


def make_webhook_request(
    *,
    text: str = "hello group",
    group_id: str = GROUP_ID,
    from_user_id: str = "uvg2p6ho",
    mention_users: Any = "",
) -> FakeRequest:
    payload = {
        "from_user_id": from_user_id,
        "to_user_id": BOT_ID,
        "group_id": group_id,
        "message": text,
        "mention_users": mention_users,
    }
    return FakeRequest(json.dumps(payload).encode("utf-8"), headers={"X-API-KEY": "hook-key"})


async def _wait_for_observed(chat_id: str) -> list[dict[str, Any]]:
    """Poll the observed store: record_observed_event offloads its write to
    the default executor when an event loop is running (async tests)."""
    rows: list[dict[str, Any]] = []
    for _ in range(200):
        rows = get_chat_directory().recent_observed(AGENT, chat_id=chat_id)
        if rows:
            break
        await asyncio.sleep(0.02)
    return rows


# ─── Mode resolution ─────────────────────────────────────────────────────────


def test_default_modes():
    a = make_polling_adapter({})
    assert a._debox_group_respond() == "all"  # legacy default: require_mention False
    assert a._debox_group_listen() == "off"


def test_legacy_require_mention_mapping():
    assert make_polling_adapter({"require_mention": True})._debox_group_respond() == "mentions"
    assert make_polling_adapter({"require_mention": False})._debox_group_respond() == "all"
    # Same resolution on the webhook transport.
    assert make_webhook_adapter({"require_mention": True})._debox_group_respond() == "mentions"


def test_group_respond_wins_over_require_mention():
    a = make_polling_adapter({"require_mention": True, "group_respond": "all"})
    assert a._debox_group_respond() == "all"
    b = make_polling_adapter({"require_mention": False, "group_respond": "none"})
    assert b._debox_group_respond() == "none"


def test_group_listen_knob():
    assert make_polling_adapter({"group_listen": "mentions"})._debox_group_listen() == "mentions"
    assert make_polling_adapter({"group_listen": "all"})._debox_group_listen() == "all"
    assert make_polling_adapter({"group_listen": "off"})._debox_group_listen() == "off"


# ─── group_respond gating: polling transport ─────────────────────────────────


async def test_polling_respond_none_drops_group_even_mentions():
    adapter = make_polling_adapter({"group_respond": "none"})
    await adapter._process_update(make_update())
    await adapter._process_update(make_update(mention_users=[{"user_id": BOT_ID}], update_id=2))
    adapter.handle_message.assert_not_awaited()


async def test_polling_respond_none_leaves_dms_alone():
    adapter = make_polling_adapter({"group_respond": "none"})
    await adapter._process_update(make_update(chat_type="private", chat_id="c9"))
    adapter.handle_message.assert_awaited_once()


async def test_polling_default_all_processes_unaddressed():
    adapter = make_polling_adapter({})  # legacy default require_mention=False → "all"
    await adapter._process_update(make_update())
    adapter.handle_message.assert_awaited_once()


async def test_polling_respond_mentions_gates_like_legacy():
    adapter = make_polling_adapter({"group_respond": "mentions"})
    # Mentioning someone else does not count when the bot's own id is known.
    await adapter._process_update(make_update(mention_users=[{"user_id": "other"}]))
    adapter.handle_message.assert_not_awaited()
    await adapter._process_update(make_update(mention_users=[{"user_id": BOT_ID}], update_id=2))
    adapter.handle_message.assert_awaited_once()


# ─── group_respond gating: webhook transport ─────────────────────────────────


async def test_webhook_respond_none_drops_group_even_mentions():
    adapter = make_webhook_adapter({"group_respond": "none"})
    resp = await adapter._handle_webhook(make_webhook_request())
    assert resp.status == 200  # 200 so DeBox doesn't retry, but nothing dispatched
    await adapter._handle_webhook(make_webhook_request(text="hey", mention_users=BOT_ID))
    adapter.handle_message.assert_not_awaited()


async def test_webhook_respond_none_leaves_dms_alone():
    adapter = make_webhook_adapter({"group_respond": "none"})
    await adapter._handle_webhook(make_webhook_request(text="direct hello", group_id=""))
    adapter.handle_message.assert_awaited_once()


async def test_webhook_respond_mentions_gates_like_legacy():
    adapter = make_webhook_adapter({"group_respond": "mentions"})
    await adapter._handle_webhook(make_webhook_request(mention_users="someoneelse"))
    adapter.handle_message.assert_not_awaited()
    await adapter._handle_webhook(make_webhook_request(mention_users=BOT_ID))
    adapter.handle_message.assert_awaited_once()


# ─── group_listen recording of dropped messages ──────────────────────────────


async def test_polling_listen_all_records_dropped_group_message():
    adapter = make_polling_adapter({"require_mention": True, "group_listen": "all"})
    await adapter._process_update(make_update(text="observed only"))
    adapter.handle_message.assert_not_awaited()
    rows = await _wait_for_observed(GROUP_ID)
    assert rows and rows[0]["text"] == "observed only"
    assert rows[0]["sender_id"] == "uvg2p6ho"
    assert rows[0]["sender_name"] == "Kai"
    assert rows[0]["platform"] == "debox"


async def test_webhook_listen_all_records_dropped_group_message():
    adapter = make_webhook_adapter({"require_mention": True, "group_listen": "all"})
    resp = await adapter._handle_webhook(make_webhook_request(text="webhook observed"))
    assert resp.status == 200
    adapter.handle_message.assert_not_awaited()
    rows = await _wait_for_observed(GROUP_ID)
    assert rows and rows[0]["text"] == "webhook observed"
    assert rows[0]["sender_id"] == "uvg2p6ho"


async def test_respond_none_listen_all_records():
    adapter = make_polling_adapter({"group_respond": "none", "group_listen": "all"})
    await adapter._process_update(make_update(text="none-mode observed"))
    adapter.handle_message.assert_not_awaited()
    rows = await _wait_for_observed(GROUP_ID)
    assert rows and rows[0]["text"] == "none-mode observed"


async def test_listen_off_records_nothing():
    adapter = make_polling_adapter({"require_mention": True})
    await adapter._process_update(make_update(text="dropped silently"))
    adapter.handle_message.assert_not_awaited()
    # listen="off" never schedules a store write, so the immediate check holds.
    assert get_chat_directory().recent_observed(AGENT, chat_id=GROUP_ID) == []


def test_maybe_record_observed_mentions_filter_inline():
    adapter = make_polling_adapter({"group_listen": "mentions"})
    # No running loop in a sync test, so record_observed_event writes inline.
    adapter._maybe_record_observed(
        chat_id=GROUP_ID,
        chat_name="DeBox group",
        user_id="uvg2p6ho",
        user_name="Kai",
        text="unaddressed",
        mentioned=False,
    )
    assert get_chat_directory().recent_observed(AGENT, chat_id=GROUP_ID) == []
    adapter._maybe_record_observed(
        chat_id=GROUP_ID,
        chat_name="DeBox group",
        user_id="uvg2p6ho",
        user_name="Kai",
        text="hey bot",
        mentioned=True,
    )
    rows = get_chat_directory().recent_observed(AGENT, chat_id=GROUP_ID)
    assert [r["text"] for r in rows] == ["hey bot"]


def test_maybe_record_observed_skips_empty_text():
    adapter = make_polling_adapter({"group_listen": "all"})
    adapter._maybe_record_observed(
        chat_id=GROUP_ID,
        chat_name="DeBox group",
        user_id="uvg2p6ho",
        user_name="Kai",
        text="   ",
        mentioned=False,
    )
    assert get_chat_directory().recent_observed(AGENT, chat_id=GROUP_ID) == []
