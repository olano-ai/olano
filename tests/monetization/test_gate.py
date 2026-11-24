"""Entitlement gate: trial start, trial expiry, active/canceled, group + owner."""

from __future__ import annotations

from pathlib import Path

from olano.config.agent_config import AgentConfig
from olano.monetization import gate
from olano.monetization.store import (
    EntitlementRecord,
    EntitlementStatus,
    EntitlementStore,
)
from olano.security.request_context import push_request_context

NOW = 1_000_000.0
DAY = 86_400


def _cfg() -> AgentConfig:
    return AgentConfig(
        name="sicop",
        monetization={
            "enabled": True,
            "trial_days": 3,
            "providers": ["stripe"],
            "plan": {"stripe_price_id": "price_1", "offer_copy": "Subscribe!"},
        },
    )


def _store(tmp_path: Path) -> EntitlementStore:
    return EntitlementStore(tmp_path / "e.db")


def test_first_seen_starts_trial_and_allows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with push_request_context(channel="telegram", user_id="42", is_owner=False):
        d = gate.evaluate_access("sicop", "telegram", _cfg(), now=NOW, store=store)
    assert d.allowed and d.reason == "trial_started"
    # A record now exists in trial.
    rec = store.get("sicop", "telegram", "42")
    assert rec is not None and rec.status is EntitlementStatus.TRIAL


def test_trial_expires_and_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cfg = _cfg()
    with push_request_context(channel="telegram", user_id="42", is_owner=False):
        gate.evaluate_access("sicop", "telegram", cfg, now=NOW, store=store)
        inside = gate.evaluate_access("sicop", "telegram", cfg, now=NOW + 2 * DAY, store=store)
        assert inside.allowed and inside.reason == "trial"
        after = gate.evaluate_access("sicop", "telegram", cfg, now=NOW + 4 * DAY, store=store)
    assert not after.allowed and after.reason == "expired"
    assert "Subscribe!" in after.message
    assert store.get("sicop", "telegram", "42").status is EntitlementStatus.EXPIRED


def test_active_subscription_allows_until_period_end(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(
        EntitlementRecord(
            agent="sicop",
            channel="telegram",
            user_id="42",
            status=EntitlementStatus.ACTIVE,
            current_period_end=NOW + 10 * DAY,
        )
    )
    with push_request_context(channel="telegram", user_id="42", is_owner=False):
        good = gate.evaluate_access("sicop", "telegram", _cfg(), now=NOW + DAY, store=store)
        lapsed = gate.evaluate_access("sicop", "telegram", _cfg(), now=NOW + 11 * DAY, store=store)
    assert good.allowed and good.reason == "active"
    assert not lapsed.allowed  # period ended, no renewal → expired
    assert store.get("sicop", "telegram", "42").status is EntitlementStatus.EXPIRED


def test_canceled_keeps_access_until_period_end(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(
        EntitlementRecord(
            agent="sicop",
            channel="telegram",
            user_id="9",
            status=EntitlementStatus.CANCELED,
            current_period_end=NOW + 5 * DAY,
        )
    )
    with push_request_context(channel="telegram", user_id="9", is_owner=False):
        d = gate.evaluate_access("sicop", "telegram", _cfg(), now=NOW + DAY, store=store)
    assert d.allowed and d.reason == "canceled"


def test_group_chat_is_open_by_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with push_request_context(channel="telegram", user_id="42", is_owner=False, is_group=True):
        d = gate.evaluate_access("sicop", "telegram", _cfg(), now=NOW, store=store)
    assert d.allowed and d.reason == "group_open"


def test_no_identity_fails_open(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with push_request_context(channel="telegram", user_id="", is_owner=False):
        d = gate.evaluate_access("sicop", "telegram", _cfg(), now=NOW, store=store)
    assert d.allowed and d.reason == "no_identity"
