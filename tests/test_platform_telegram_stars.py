"""Tests for Telegram Stars payments on the platform adapter (ported from
the deprecated classic channel)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from olano.platforms.config import PlatformConfig
from olano.platforms.telegram import TelegramAdapter


def _adapter(**extra) -> TelegramAdapter:
    cfg = PlatformConfig(enabled=True, token="bot-token", extra=extra)
    adapter = TelegramAdapter(cfg, agent_name="test-telegram")
    adapter._bot = AsyncMock()
    return adapter


def test_pre_checkout_query_in_default_update_types():
    assert "pre_checkout_query" in _adapter()._allowed_update_types()


async def test_send_subscription_offer_stripe_button():
    adapter = _adapter()
    offer = SimpleNamespace(
        text="Subscribe!", stripe_url="https://checkout.stripe.com/x", stars_invoice=None
    )

    sent = await adapter.send_subscription_offer("123", offer)

    assert sent is True
    kwargs = adapter._bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert kwargs["text"] == "Subscribe!"
    assert kwargs["reply_markup"] is not None
    adapter._bot.send_invoice.assert_not_awaited()


async def test_send_subscription_offer_recurring_stars_uses_invoice_link():
    # Recurring Stars subscriptions only exist on createInvoiceLink —
    # sendInvoice has no subscription_period parameter.
    adapter = _adapter()
    adapter._bot.create_invoice_link.return_value = "https://t.me/invoice/abc"
    offer = SimpleNamespace(
        text="Subscribe!",
        stripe_url="",
        stars_invoice={
            "title": "Pro",
            "description": "Full access",
            "payload": "ref123",
            "currency": "XTR",
            "prices": [{"label": "Pro", "amount": 250}],
            "subscription_period": 2592000,
        },
    )

    sent = await adapter.send_subscription_offer("123", offer)

    assert sent is True
    link_kwargs = adapter._bot.create_invoice_link.await_args.kwargs
    assert link_kwargs["currency"] == "XTR"
    assert link_kwargs["provider_token"] == ""
    assert link_kwargs["subscription_period"] == 2592000
    assert link_kwargs["payload"] == "ref123"
    adapter._bot.send_invoice.assert_not_awaited()
    msg_kwargs = adapter._bot.send_message.await_args.kwargs
    assert msg_kwargs["chat_id"] == 123
    assert msg_kwargs["reply_markup"] is not None


async def test_send_subscription_offer_one_off_stars_uses_native_invoice():
    adapter = _adapter()
    offer = SimpleNamespace(
        text="Buy!",
        stripe_url="",
        stars_invoice={
            "title": "Day pass",
            "description": "24h access",
            "payload": "ref456",
            "currency": "XTR",
            "prices": [{"label": "Day pass", "amount": 50}],
            "subscription_period": None,
        },
    )

    sent = await adapter.send_subscription_offer("123", offer)

    assert sent is True
    kwargs = adapter._bot.send_invoice.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert kwargs["currency"] == "XTR"
    assert kwargs["payload"] == "ref456"
    adapter._bot.create_invoice_link.assert_not_awaited()


async def test_send_subscription_offer_nothing_to_send():
    adapter = _adapter()
    offer = SimpleNamespace(text="", stripe_url="", stars_invoice=None)
    assert await adapter.send_subscription_offer("123", offer) is False


async def test_pre_checkout_answers_ok_for_valid_payload():
    adapter = _adapter()
    query = AsyncMock()
    query.invoice_payload = "valid-ref"
    update = MagicMock(pre_checkout_query=query)

    with patch(
        "olano.monetization.providers.stripe_provider.decode_reference",
        return_value=("agent", "telegram", "42"),
    ):
        await adapter._handle_pre_checkout(update, MagicMock())

    query.answer.assert_awaited_once_with(ok=True, error_message=None)


async def test_pre_checkout_rejects_unknown_payload():
    adapter = _adapter()
    query = AsyncMock()
    query.invoice_payload = "garbage"
    update = MagicMock(pre_checkout_query=query)

    with patch(
        "olano.monetization.providers.stripe_provider.decode_reference",
        return_value=None,
    ):
        await adapter._handle_pre_checkout(update, MagicMock())

    query.answer.assert_awaited_once_with(ok=False, error_message="Invalid subscription.")


async def test_successful_payment_applies_entitlement():
    adapter = _adapter()
    sp = SimpleNamespace(
        invoice_payload="ref",
        telegram_payment_charge_id="chg_1",
        subscription_expiration_date=None,
    )
    msg = MagicMock(successful_payment=sp, chat_id=555)
    update = MagicMock(message=msg)
    fake_update_obj = MagicMock()

    with (
        patch(
            "olano.monetization.providers.stars_provider.TelegramStarsProvider.parse_event",
            return_value=fake_update_obj,
        ),
        patch("olano.monetization.service.apply_entitlement_update") as apply_mock,
        patch.object(adapter, "send", new=AsyncMock()) as send_mock,
    ):
        await adapter._handle_successful_payment(update, MagicMock())

    apply_mock.assert_called_once_with(fake_update_obj)
    send_mock.assert_awaited_once()
    assert "Subscription active" in send_mock.await_args.args[1]


async def test_stars_provider_accepts_both_telegram_channel_names():
    from olano.monetization.providers.stars_provider import TelegramStarsProvider

    plan = SimpleNamespace(stars_amount=100, product_name="Pro", offer_copy="Go pro")
    provider = TelegramStarsProvider(agent="a", plan=plan)

    for channel in ("telegram", "platform:telegram"):
        offer = await provider.create_offer(channel=channel, user_id="42")
        assert offer is not None and offer.stars_invoice is not None, channel

    assert await provider.create_offer(channel="discord", user_id="42") is None
