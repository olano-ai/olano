"""Provider parsing/verification + offer assembly (no network)."""

from __future__ import annotations

import hmac
import time
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from olano.config.agent_config import AgentConfig
from olano.monetization import service
from olano.monetization.providers.base import (
    PaymentProvider,
    SubscriptionOffer,
)
from olano.monetization.providers.stars_provider import TelegramStarsProvider
from olano.monetization.providers.stripe_provider import (
    StripeProvider,
    decode_reference,
    encode_reference,
)
from olano.monetization.store import EntitlementStatus


def _signed(secret: str, body: bytes, ts: str | None = None) -> str:
    ts = ts or str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_reference_round_trip() -> None:
    assert decode_reference(encode_reference("a", "telegram", "42")) == ("a", "telegram", "42")
    # user_id containing a colon survives.
    assert decode_reference("a:telegram:4:2") == ("a", "telegram", "4:2")
    assert decode_reference("garbage") is None


def test_stripe_verify_good_and_bad() -> None:
    prov = StripeProvider(agent="sicop", plan=None)
    body = b'{"id":"evt_1","type":"x"}'
    secret = "whsec_abc"
    with patch("olano.monetization.providers.stripe_provider.get_credential", return_value=secret):
        assert prov.verify_webhook(headers={"stripe-signature": _signed(secret, body)}, body=body)
        assert not prov.verify_webhook(
            headers={"stripe-signature": f"t={int(time.time())},v1=bad"}, body=body
        )
        # Stale timestamp is rejected.
        old = str(int(time.time()) - 10_000)
        assert not prov.verify_webhook(
            headers={"stripe-signature": _signed(secret, body, ts=old)}, body=body
        )
    # Missing secret → reject.
    with patch("olano.monetization.providers.stripe_provider.get_credential", return_value=""):
        assert not prov.verify_webhook(
            headers={"stripe-signature": _signed(secret, body)}, body=body
        )


def test_stripe_parse_event_types() -> None:
    prov = StripeProvider(agent="sicop", plan=None)

    def ev(t: str, obj: dict) -> dict:
        return {"id": "evt", "type": t, "data": {"object": obj}}

    checkout = prov.parse_event(
        ev(
            "checkout.session.completed",
            {
                "client_reference_id": "sicop:telegram:42",
                "subscription": "sub_1",
                "customer": "cus_1",
                "current_period_end": 111,
            },
        )
    )
    assert checkout.status is EntitlementStatus.ACTIVE and checkout.user_id == "42"
    assert checkout.external_sub_id == "sub_1"

    failed = prov.parse_event(ev("invoice.payment_failed", {"subscription": "sub_1"}))
    assert failed.status is EntitlementStatus.PAST_DUE

    deleted = prov.parse_event(ev("customer.subscription.deleted", {"id": "sub_1"}))
    assert deleted.status is EntitlementStatus.CANCELED

    refunded = prov.parse_event(ev("charge.refunded", {"customer": "cus_1"}))
    assert refunded.status is EntitlementStatus.CANCELED and refunded.current_period_end == 0.0

    assert prov.parse_event(ev("some.unrelated.event", {})) is None
    # checkout with no client_reference_id can't be linked → skipped.
    assert prov.parse_event(ev("checkout.session.completed", {})) is None


def test_stars_parse_event() -> None:
    prov = TelegramStarsProvider(agent="sicop", plan=None)
    upd = prov.parse_event(
        {
            "invoice_payload": "sicop:telegram:42",
            "telegram_payment_charge_id": "charge_9",
            "subscription_expiration_date": 1_700_000_000,
        }
    )
    assert upd.status is EntitlementStatus.ACTIVE
    assert upd.user_id == "42" and upd.external_sub_id == "charge_9"
    assert upd.current_period_end == 1_700_000_000.0
    # Unrecognized payload → None.
    assert prov.parse_event({"invoice_payload": "nope"}) is None


async def test_stars_create_offer_offline() -> None:
    cfg = AgentConfig(
        name="sicop",
        monetization={
            "enabled": True,
            "providers": ["telegram_stars"],
            "plan": {"stars_amount": 500, "product_name": "Alerts"},
        },
    )
    prov = TelegramStarsProvider(agent="sicop", plan=cfg.monetization.plan)
    offer = await prov.create_offer(channel="telegram", user_id="42")
    assert offer is not None and offer.stars_invoice is not None
    assert offer.stars_invoice["currency"] == "XTR"
    assert offer.stars_invoice["payload"] == "sicop:telegram:42"
    # Stars only applies to telegram / positive amount.
    assert await prov.create_offer(channel="discord", user_id="42") is None


async def test_build_subscription_offer_merges_rails() -> None:
    """A blocked user's offer combines every provider that returns something."""

    class FakeStripe(PaymentProvider):
        name = "stripe"

        async def create_offer(self, *, channel, user_id):
            return SubscriptionOffer(text="pay", stripe_url="https://pay/x")

    cfg = AgentConfig(
        name="sicop",
        monetization={
            "enabled": True,
            "providers": ["stripe", "telegram_stars"],
            "plan": {"stars_amount": 500, "offer_copy": "join"},
        },
    )
    real = {
        "stripe": FakeStripe(agent="sicop", plan=cfg.monetization.plan),
        "telegram_stars": TelegramStarsProvider(agent="sicop", plan=cfg.monetization.plan),
    }
    with patch("olano.monetization.service.build_providers", return_value=real):
        offer = await service.build_subscription_offer("sicop", "telegram", "42", cfg)
    assert offer.stripe_url == "https://pay/x"
    assert offer.stars_invoice is not None
    assert not offer.is_empty()


async def test_stripe_offer_resolves_the_agents_own_vault_key() -> None:
    """/subscribe and the paywall run before the turn binds the agent, so the
    offer has to bind it: a STRIPE_API_KEY stored as a per-agent vault override
    is otherwise invisible and the Stripe button silently disappears."""

    class ScopedVault:
        def get(self, name, agent=None, touch=True):
            return "rk_live_sicop" if (name, agent) == ("STRIPE_API_KEY", "sicop") else None

    sent: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"url": "https://checkout.stripe.com/c/pay/cs_x"}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            pass

        async def post(self, url, data=None, auth=None):
            sent.update(url=url, data=data, auth=auth)
            return FakeResponse()

    cfg = AgentConfig(
        name="sicop",
        monetization={
            "enabled": True,
            "providers": ["stripe"],
            "plan": {"stripe_price_id": "price_x", "offer_copy": "join"},
        },
    )
    with (
        patch("olano.vault.resolver.get_vault", return_value=ScopedVault()),
        patch("olano.monetization.providers.stripe_provider.httpx.AsyncClient", FakeClient),
    ):
        offer = await service.build_subscription_offer("sicop", "telegram", "42", cfg)
    assert offer.stripe_url == "https://checkout.stripe.com/c/pay/cs_x"
    assert sent["auth"] == ("rk_live_sicop", "")
    assert sent["data"]["client_reference_id"] == "sicop:telegram:42"


async def test_offer_empty_when_no_rail_usable(tmp_path: Path) -> None:
    cfg = AgentConfig(
        name="sicop",
        monetization={
            "enabled": True,
            "providers": ["stripe"],
            "plan": {"stripe_price_id": "", "offer_copy": "join"},
        },
    )
    # No Stripe key/price and no Stars → offer falls back to a contact-owner note.
    with patch("olano.monetization.providers.stripe_provider.get_credential", return_value=""):
        offer = await service.build_subscription_offer("sicop", "telegram", "42", cfg)
    assert offer.is_empty()
    assert "contact the operator" in offer.text
