"""Payment-provider registry: build the rails an agent's config enables."""

from __future__ import annotations

from typing import Any

from olano.monetization.providers.base import (
    EntitlementUpdate,
    PaymentProvider,
    SubscriptionOffer,
)

__all__ = [
    "EntitlementUpdate",
    "PaymentProvider",
    "SubscriptionOffer",
    "build_providers",
]

# Provider name → class. New rails register here.
_REGISTRY: dict[str, type[PaymentProvider]] = {}


def _load_registry() -> dict[str, type[PaymentProvider]]:
    if not _REGISTRY:
        from olano.monetization.providers.stars_provider import TelegramStarsProvider
        from olano.monetization.providers.stripe_provider import StripeProvider

        _REGISTRY["stripe"] = StripeProvider
        _REGISTRY["telegram_stars"] = TelegramStarsProvider
    return _REGISTRY


def build_providers(config: Any, agent: str) -> dict[str, PaymentProvider]:
    """Instantiate the providers named in ``config.monetization.providers``.

    Unknown names are skipped (logged by the caller). Each provider is bound to
    the agent + its plan so credential resolution stays per-agent.
    """
    mon = getattr(config, "monetization", None)
    if mon is None or not getattr(mon, "enabled", False):
        return {}
    registry = _load_registry()
    plan = getattr(mon, "plan", None)
    out: dict[str, PaymentProvider] = {}
    for name in getattr(mon, "providers", []) or []:
        cls = registry.get(name)
        if cls is not None:
            out[name] = cls(agent=agent, plan=plan)
    return out
