"""Price formatting from a MonetizationPlan."""

from __future__ import annotations

from olano.config.agent_config import MonetizationPlan
from olano.monetization.pricing import format_fiat, format_price, format_stars


def _plan(**kw) -> MonetizationPlan:
    return MonetizationPlan(**kw)


def test_fiat_usd_whole_amount() -> None:
    assert format_fiat(_plan(price_cents=1500, currency="usd", interval="month")) == "$15/month"


def test_fiat_non_whole_and_eur_yearly() -> None:
    assert format_fiat(_plan(price_cents=999, currency="eur", interval="year")) == "€9.99/year"


def test_fiat_unknown_currency_uses_code() -> None:
    # Costa Rican colón has no symbol in the map → "CRC 8000/month".
    assert format_fiat(_plan(price_cents=800000, currency="crc")) == "CRC 8000/month"


def test_fiat_zero_is_empty() -> None:
    assert format_fiat(_plan(price_cents=0)) == ""


def test_stars() -> None:
    assert format_stars(_plan(stars_amount=750)) == "750 ⭐/month"
    assert format_stars(_plan(stars_amount=0)) == ""


def test_format_price_respects_enabled_rails() -> None:
    plan = _plan(price_cents=1500, currency="usd", stars_amount=750)
    assert format_price(plan, ["stripe"]) == "$15/month"
    assert format_price(plan, ["telegram_stars"]) == "750 ⭐/month"
    assert format_price(plan, ["stripe", "telegram_stars"]) == "$15/month or 750 ⭐/month"
    # A rail priced in config but not enabled is not shown.
    assert format_price(plan, []) == ""
