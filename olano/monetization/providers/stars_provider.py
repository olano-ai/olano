"""Telegram Stars rail: native in-chat recurring subscriptions.

Telegram *requires* digital goods/services to be sold with Stars (``XTR``), not
a fiat card, and supports recurring Stars subscriptions via ``send_invoice``
with a ``subscription_period``. Unlike Stripe there is no HTTP webhook — payment
confirmation arrives as a Telegram ``successful_payment`` bot update, so this
provider only builds the invoice params and parses that update; the HTTP
``verify_webhook`` default (reject) is left untouched.

The chat user is linked via the invoice ``payload`` (same triple encoding as the
Stripe ``client_reference_id``).
"""

from __future__ import annotations

import logging
from typing import Any

from olano.monetization.providers.base import (
    EntitlementUpdate,
    PaymentProvider,
    SubscriptionOffer,
)
from olano.monetization.providers.stripe_provider import decode_reference, encode_reference
from olano.monetization.store import EntitlementStatus

logger = logging.getLogger(__name__)

# Telegram Stars subscriptions only accept a 30-day (monthly) period today.
_STARS_PERIOD_SECONDS = 2592000


class TelegramStarsProvider(PaymentProvider):
    name = "telegram_stars"

    async def create_offer(self, *, channel: str, user_id: str) -> SubscriptionOffer | None:
        amount = int(getattr(self.plan, "stars_amount", 0) or 0)
        # Both Telegram integration paths sell Stars: the platform adapter
        # reports "telegram" (Platform.TELEGRAM.value) but accept the raw
        # config type "platform:telegram" too in case a caller passes it.
        if channel not in ("telegram", "platform:telegram") or amount <= 0:
            return None
        invoice = {
            "title": getattr(self.plan, "product_name", "Subscription"),
            "description": getattr(self.plan, "offer_copy", "") or "Subscribe for full access.",
            "payload": encode_reference(self.agent, channel, user_id),
            "currency": "XTR",
            # LabeledPrice built in the channel adapter (needs the telegram class);
            # we pass the primitives so this module stays telegram-SDK-free.
            "prices": [
                {"label": getattr(self.plan, "product_name", "Subscription"), "amount": amount}
            ],
            "subscription_period": _STARS_PERIOD_SECONDS,
        }
        return SubscriptionOffer(text=getattr(self.plan, "offer_copy", ""), stars_invoice=invoice)

    def parse_event(self, payload: dict[str, Any]) -> EntitlementUpdate | None:
        """Parse a Telegram ``successful_payment`` mapping into an update.

        Expected keys: ``invoice_payload`` (our triple), ``telegram_payment_charge_id``,
        and optionally ``subscription_expiration_date`` (Unix seconds).
        """
        ref = decode_reference(str(payload.get("invoice_payload") or ""))
        if not ref:
            return None
        agent, channel, user_id = ref
        expiration = payload.get("subscription_expiration_date")
        try:
            period_end = float(expiration) if expiration is not None else None
        except (TypeError, ValueError):
            period_end = None
        charge_id = str(payload.get("telegram_payment_charge_id") or "")
        return EntitlementUpdate(
            status=EntitlementStatus.ACTIVE,
            provider=self.name,
            agent=agent,
            channel=channel,
            user_id=user_id,
            current_period_end=period_end,
            external_sub_id=charge_id,
            external_customer_id=user_id,
            # Stars charges have no separate event id; dedupe on the charge id.
            event_id=charge_id,
        )
