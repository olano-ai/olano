"""Human-readable price formatting from a MonetizationPlan.

Standalone (no gate/service imports) so both can use it without a cycle. Turns
the numbers in ``plan`` into copy like ``"$15/month"`` or ``"$15/month (or 750
⭐/month)"`` so the welcome, paywall, and offer show the price automatically —
the owner no longer has to hand-write it into ``offer_copy``.
"""

from __future__ import annotations

from typing import Any

# Common currency → symbol. Anything else renders as the uppercase code + space
# (e.g. "CRC 8000/month"), which is unambiguous if less pretty.
_CURRENCY_SYMBOLS = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥"}


def _interval_word(interval: str) -> str:
    return {"month": "month", "year": "year"}.get(interval, interval or "month")


def format_fiat(plan: Any) -> str:
    """Render the fiat price, e.g. ``"$15/month"`` — empty if not priced."""
    cents = int(getattr(plan, "price_cents", 0) or 0)
    if cents <= 0:
        return ""
    currency = str(getattr(plan, "currency", "usd") or "usd").lower()
    amount = cents / 100
    # Whole amounts drop the ".00"; otherwise show two decimals.
    amount_str = f"{amount:.0f}" if amount == int(amount) else f"{amount:.2f}"
    interval = _interval_word(str(getattr(plan, "interval", "month")))
    symbol = _CURRENCY_SYMBOLS.get(currency)
    if symbol:
        return f"{symbol}{amount_str}/{interval}"
    return f"{currency.upper()} {amount_str}/{interval}"


def format_stars(plan: Any) -> str:
    """Render the Telegram Stars price, e.g. ``"750 ⭐/month"`` — empty if off."""
    amount = int(getattr(plan, "stars_amount", 0) or 0)
    if amount <= 0:
        return ""
    interval = _interval_word(str(getattr(plan, "interval", "month")))
    return f"{amount} ⭐/{interval}"


def format_price(plan: Any, providers: list[str] | None = None) -> str:
    """Combine the enabled rails into one price string.

    ``providers`` is the agent's configured rail list; a rail's price is only
    shown when that rail is actually enabled. Returns ``""`` when nothing is
    priced (caller then omits the price line entirely).
    """
    providers = providers or []
    fiat = format_fiat(plan) if "stripe" in providers else ""
    stars = format_stars(plan) if "telegram_stars" in providers else ""
    if fiat and stars:
        # No inner parens — callers may wrap the whole thing in parens.
        return f"{fiat} or {stars}"
    return fiat or stars
