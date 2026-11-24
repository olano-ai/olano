"""Welcome/onboarding copy + price-aware paywall and offer."""

from __future__ import annotations

from pathlib import Path

from olano.config.agent_config import AgentConfig
from olano.monetization import gate, service
from olano.monetization.store import (
    EntitlementRecord,
    EntitlementStatus,
    EntitlementStore,
    reset_entitlement_store,
)
from olano.security.request_context import push_request_context


def _cfg() -> AgentConfig:
    return AgentConfig(
        name="sicop",
        monetization={
            "enabled": True,
            "providers": ["stripe", "telegram_stars"],
            "trial_days": 3,
            "plan": {
                "price_cents": 1500,
                "currency": "usd",
                "stripe_price_id": "price_1",
                "stars_amount": 750,
                "offer_copy": "Keep the alerts coming!",
            },
        },
    )


def test_welcome_new_user_mentions_trial_and_price(tmp_path: Path) -> None:
    reset_entitlement_store(EntitlementStore(tmp_path / "e.db"))
    text = service.welcome_text("sicop", "telegram", _cfg(), user_id="42")
    assert "3-day free trial" in text
    assert "$15/month or 750 ⭐/month" in text
    assert "/subscribe" in text


def test_welcome_on_trial_shows_days_left(tmp_path: Path) -> None:
    store = EntitlementStore(tmp_path / "e.db")
    reset_entitlement_store(store)
    store.upsert(
        EntitlementRecord(
            agent="sicop",
            channel="telegram",
            user_id="42",
            status=EntitlementStatus.TRIAL,
            trial_start=1_000_000.0,
        )
    )
    text = service.welcome_text("sicop", "telegram", _cfg(), user_id="42", now=1_000_000.0 + 86_400)
    assert "day(s) left" in text


def test_welcome_active_user_is_welcomed_back(tmp_path: Path) -> None:
    store = EntitlementStore(tmp_path / "e.db")
    reset_entitlement_store(store)
    store.upsert(
        EntitlementRecord(
            agent="sicop",
            channel="telegram",
            user_id="42",
            status=EntitlementStatus.ACTIVE,
            current_period_end=2_000_000_000.0,
        )
    )
    text = service.welcome_text("sicop", "telegram", _cfg(), user_id="42")
    assert "Welcome back" in text and "active" in text


def test_paywall_includes_price(tmp_path: Path) -> None:
    reset_entitlement_store(EntitlementStore(tmp_path / "e.db"))
    msg = gate._paywall_text(_cfg())
    assert "Keep the alerts coming!" in msg
    assert "$15/month or 750 ⭐/month" in msg


async def test_offer_text_includes_price(tmp_path: Path) -> None:
    reset_entitlement_store(EntitlementStore(tmp_path / "e.db"))
    with push_request_context(channel="telegram", user_id="42", is_owner=False):
        offer = await service.build_subscription_offer("sicop", "telegram", "42", _cfg())
    assert "$15/month or 750 ⭐/month" in offer.text
