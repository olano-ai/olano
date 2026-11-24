"""Tests for the telegram_business toolkit (secretary/management mode tools).

The tools are double-gated: connector toggle in config.extra AND the right
granted on the business connection. All gates default to OFF/absent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from olano.platforms.config import PlatformConfig
from olano.platforms.telegram import TelegramAdapter
from olano.tools import telegram_business_tools as tbt

CONN_ID = "bconn1"


def _adapter(**extra) -> TelegramAdapter:
    cfg = PlatformConfig(enabled=True, token="bot-token", extra=extra)
    adapter = TelegramAdapter(cfg, agent_name="test-telegram")
    adapter._bot = AsyncMock()
    return adapter


def _entry(**rights) -> dict:
    return {
        "id": CONN_ID,
        "user_id": "100",
        "user_name": "Olga Owner",
        "username": "olga",
        "user_chat_id": "100",
        "is_enabled": True,
        "date": None,
        "rights": rights,
    }


def _patch_adapter(adapter):
    return patch.object(tbt, "_adapter", return_value=adapter)


def _run_inline_patch():
    import asyncio

    return patch.object(tbt, "_run", side_effect=lambda factory: asyncio.run(factory()))


def test_no_adapter_message():
    with patch.object(tbt, "_adapter", return_value=None):
        out = tbt.telegram_business_status.invoke({})
    assert "No running platform:telegram connector" in out


def test_status_lists_connections_and_rights():
    adapter = _adapter(business_mode=True)
    adapter._business_connections[CONN_ID] = _entry(can_reply=True, can_edit_bio=True)
    with _patch_adapter(adapter):
        out = tbt.telegram_business_status.invoke({})
    assert CONN_ID in out
    assert "can_edit_bio" in out and "can_reply" in out


def test_set_name_blocked_without_toggle():
    adapter = _adapter(business_mode=True)  # business_manage_profile absent
    adapter._business_connections[CONN_ID] = _entry(can_edit_name=True)
    with _patch_adapter(adapter):
        out = tbt.telegram_business_set_name.invoke({"first_name": "New"})
    assert "business_manage_profile" in out
    adapter._bot.set_business_account_name.assert_not_awaited()


def test_set_name_blocked_without_right():
    adapter = _adapter(business_mode=True, business_manage_profile=True)
    adapter._business_connections[CONN_ID] = _entry(can_edit_name=False)
    with _patch_adapter(adapter):
        out = tbt.telegram_business_set_name.invoke({"first_name": "New"})
    assert "can_edit_name" in out
    adapter._bot.set_business_account_name.assert_not_awaited()


def test_set_name_happy_path():
    adapter = _adapter(business_mode=True, business_manage_profile=True)
    adapter._business_connections[CONN_ID] = _entry(can_edit_name=True)
    with _patch_adapter(adapter), _run_inline_patch():
        out = tbt.telegram_business_set_name.invoke({"first_name": "New", "last_name": "Name"})
    assert "name set" in out.lower()
    kwargs = adapter._bot.set_business_account_name.await_args.kwargs
    assert kwargs["business_connection_id"] == CONN_ID
    assert kwargs["first_name"] == "New"


def test_set_bio_happy_path():
    adapter = _adapter(business_mode=True, business_manage_profile=True)
    adapter._business_connections[CONN_ID] = _entry(can_edit_bio=True)
    with _patch_adapter(adapter), _run_inline_patch():
        out = tbt.telegram_business_set_bio.invoke({"bio": "We fix things."})
    assert "bio updated" in out.lower()
    kwargs = adapter._bot.set_business_account_bio.await_args.kwargs
    assert kwargs["business_connection_id"] == CONN_ID


def test_star_balance_gated_and_working():
    adapter = _adapter(business_mode=True, business_manage_gifts=True)
    adapter._business_connections[CONN_ID] = _entry(can_view_gifts_and_stars=True)
    adapter._bot.get_business_account_star_balance.return_value = MagicMock(amount=420)
    with _patch_adapter(adapter), _run_inline_patch():
        out = tbt.telegram_business_get_star_balance.invoke({})
    assert "420" in out


def test_star_balance_blocked_without_toggle():
    adapter = _adapter(business_mode=True)
    adapter._business_connections[CONN_ID] = _entry(can_view_gifts_and_stars=True)
    with _patch_adapter(adapter):
        out = tbt.telegram_business_get_star_balance.invoke({})
    assert "business_manage_gifts" in out


def test_managed_bot_access_requires_toggle():
    adapter = _adapter()
    with _patch_adapter(adapter):
        out = tbt.telegram_managed_bot_get_access.invoke({"bot_user_id": 777})
    assert "manage_bots" in out
    adapter._bot.get_managed_bot_access_settings.assert_not_awaited()


def test_managed_bot_set_access_happy_path():
    adapter = _adapter(manage_bots=True)
    adapter._bot.set_managed_bot_access_settings.return_value = True
    with _patch_adapter(adapter), _run_inline_patch():
        out = tbt.telegram_managed_bot_set_access.invoke(
            {"bot_user_id": 777, "is_access_restricted": True, "allowed_user_ids": "1, 2"}
        )
    assert "access updated" in out
    args = adapter._bot.set_managed_bot_access_settings.await_args
    assert args.args == (777, True)
    assert args.kwargs["added_user_ids"] == [1, 2]


def test_toolkit_registered():
    from olano.tools.registry import _TOOLKITS

    assert _TOOLKITS["telegram_business"] == "olano.tools.telegram_business_tools"
    names = {t.name for t in tbt.get_tools()}
    assert "telegram_business_status" in names
    assert "telegram_managed_bot_set_access" in names
