"""Payment-provider abstraction shared by every rail (Stripe, Telegram Stars).

A provider does three things: mint a *checkout offer* for a blocked user, verify
an inbound webhook/callback is authentic, and translate a verified payment event
into an :class:`EntitlementUpdate` the store can apply. Keeping this interface
channel- and rail-agnostic is what lets one gate serve Telegram today and
Discord/Slack/web later, and lets an owner pick Stripe, Stars, or both.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from olano.monetization.store import EntitlementStatus


@dataclass
class SubscriptionOffer:
    """What to present to a blocked user so they can start paying.

    A single offer can carry more than one rail — e.g. a Stripe Checkout URL
    (button) *and* a Telegram Stars invoice (native in-chat) — and the channel
    adapter renders whatever it supports. ``text`` is the always-works fallback.
    """

    text: str = ""
    stripe_url: str = ""
    # Params for ``bot.send_invoice`` (Telegram Stars). ``None`` ⇒ no Stars rail.
    stars_invoice: dict[str, Any] | None = None

    def is_empty(self) -> bool:
        return not self.stripe_url and not self.stars_invoice


@dataclass
class EntitlementUpdate:
    """A verified payment event, normalized into a state change for the store.

    ``agent``/``channel``/``user_id`` identify the record. Provider webhooks that
    carry only a subscription id (e.g. ``invoice.paid``) leave ``user_id`` empty;
    :func:`olano.monetization.service.apply_entitlement_update` then resolves
    the record via ``external_sub_id``.
    """

    status: EntitlementStatus
    provider: str
    agent: str = ""
    channel: str = ""
    user_id: str = ""
    current_period_end: float | None = None
    external_customer_id: str = ""
    external_sub_id: str = ""
    event_id: str = ""


class PaymentProvider(ABC):
    """One payment rail for one agent (its credentials resolved per-agent)."""

    name: str = "base"

    def __init__(self, agent: str, plan: Any) -> None:
        # ``plan`` is a duck-typed MonetizationPlan (price/currency/ids/copy).
        self.agent = agent
        self.plan = plan

    @abstractmethod
    async def create_offer(self, *, channel: str, user_id: str) -> SubscriptionOffer | None:
        """Build a checkout offer for one blocked user, or ``None`` on failure.

        Must **fail soft**: a missing key / API error returns ``None`` (the gate
        falls back to another rail or a contact-owner message) — never raises
        into the message handler.
        """

    def verify_webhook(self, *, headers: dict[str, str], body: bytes) -> bool:
        """Return True iff the raw webhook body is authentic. Default: reject.

        Rails without an HTTP webhook (Telegram Stars arrives as a bot update,
        not an HTTP POST) keep the default and never see the webhook route.
        """
        return False

    def parse_event(self, payload: dict[str, Any]) -> EntitlementUpdate | None:
        """Translate a verified event payload into an update, or ``None`` to skip."""
        return None
