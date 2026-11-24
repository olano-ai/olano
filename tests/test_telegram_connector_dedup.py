"""Tests for the Telegram connector dedup: explicit allowed-updates lists
and the deprecation of the classic ``telegram`` channel in favor of
``platform:telegram``."""

from __future__ import annotations

from olano.channels import telegram_channel as legacy_telegram
from olano.platforms.catalog import build_platform_catalog
from olano.platforms.config import PlatformConfig
from olano.platforms.telegram import TelegramAdapter


def _adapter(**extra) -> TelegramAdapter:
    cfg = PlatformConfig(enabled=True, token="bot-token", extra=extra)
    return TelegramAdapter(cfg, agent_name="test-telegram")


# ── allowed_updates containment ──────────────────────────────────────────


def test_platform_adapter_default_update_types_exclude_business():
    adapter = _adapter()
    allowed = adapter._allowed_update_types()
    assert "message" in allowed
    assert "callback_query" in allowed
    for biz in (
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
    ):
        assert biz not in allowed


def test_platform_adapter_business_mode_subscribes_business_updates():
    adapter = _adapter(business_mode=True)
    allowed = adapter._allowed_update_types()
    for biz in (
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
    ):
        assert biz in allowed


def test_legacy_channel_update_types_exclude_business():
    allowed = legacy_telegram._ALLOWED_UPDATE_TYPES
    assert "message" in allowed
    assert "pre_checkout_query" in allowed  # Stars payments still work
    assert not any("business" in t for t in allowed)


# ── catalog deprecation ─────────────────────────────────────────────────


def test_catalog_marks_legacy_telegram_deprecated():
    rows = {row["type"]: row for row in build_platform_catalog()}
    legacy = rows["telegram"]
    assert legacy["deprecated"] is True
    assert "platform:telegram" in legacy["note"]


def test_catalog_platform_telegram_not_deprecated():
    rows = {row["type"]: row for row in build_platform_catalog()}
    assert "deprecated" not in rows["platform:telegram"]


def test_catalog_deprecates_every_classic_channel():
    """Telegram was the first; every classic adapter is superseded now."""
    rows = {row["type"]: row for row in build_platform_catalog()}
    for ch in ("discord", "slack", "whatsapp", "email", "imap"):
        assert rows[ch]["deprecated"] is True, ch
