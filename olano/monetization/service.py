"""Orchestration between the gate, providers, store, and channels.

- :func:`build_subscription_offer` — assemble a combined offer (Stripe URL +
  Stars invoice) to present to a blocked user; fails soft to a text-only offer.
- :func:`apply_entitlement_update` — apply a *verified* payment event to the
  store (idempotent), resolving identity by subscription id when the event
  carries no chat reference.
- :func:`entitled_user_ids` — who may receive a proactive push.
- :func:`send_confirmation` — best-effort DM after a state change, routed onto
  the channel adapter's own event loop.
"""

from __future__ import annotations

import logging

from olano.monetization.providers import SubscriptionOffer, build_providers
from olano.monetization.providers.base import EntitlementUpdate
from olano.monetization.store import (
    EntitlementRecord,
    EntitlementStatus,
    EntitlementStore,
    get_entitlement_store,
)

logger = logging.getLogger(__name__)


async def build_subscription_offer(
    agent: str, channel: str, user_id: str, config
) -> SubscriptionOffer:
    """Combine every configured rail into one offer for a blocked user.

    Both callers (``/subscribe`` and the paywall) run before the turn binds the
    agent, so the providers' credential reads are bound here: without it a
    STRIPE_API_KEY stored as a per-agent vault override resolves to nothing and
    the Stripe rail silently drops out of the offer.
    """
    from olano.monetization.pricing import format_price
    from olano.tools._credentials import agent_runtime_context

    providers = build_providers(config, agent)
    mon = getattr(config, "monetization", None)
    plan = getattr(mon, "plan", None)
    copy = getattr(plan, "offer_copy", "") or "Subscribe to continue."
    price = format_price(plan, list(getattr(mon, "providers", []) or []))
    offer = SubscriptionOffer(text=f"{copy} ({price})" if price else copy)
    for name, provider in providers.items():
        try:
            with agent_runtime_context(
                agent,
                getattr(config, "env", None),
                credential_scope=getattr(config, "credential_scope", None),
            ):
                sub = await provider.create_offer(channel=channel, user_id=user_id)
        except Exception as exc:  # defensive — never let a rail break the paywall
            logger.warning("Provider %s create_offer failed for agent=%s: %s", name, agent, exc)
            sub = None
        if sub is None:
            continue
        if sub.stripe_url:
            offer.stripe_url = sub.stripe_url
        if sub.stars_invoice:
            offer.stars_invoice = sub.stars_invoice
    if offer.is_empty():
        # No usable rail (e.g. owner's Stripe key missing and no Stars) — say so
        # rather than silently blocking with a dead paywall.
        offer.text = (
            f"{offer.text}\n\n(Subscriptions aren't set up yet — please contact the operator.)"
        )
    return offer


def apply_entitlement_update(
    update: EntitlementUpdate, *, store: EntitlementStore | None = None
) -> EntitlementRecord | None:
    """Apply a verified payment event to the store, idempotently.

    Returns the updated record, or ``None`` if the event was a duplicate or its
    target couldn't be resolved. Pure store logic (no I/O to channels) so it is
    easy to unit-test; callers send any confirmation DM separately.
    """
    store = store or get_entitlement_store()

    # Idempotency first — a retried delivery must not double-apply.
    if not store.claim_event(update.provider, update.event_id):
        logger.info("Duplicate %s event %s ignored", update.provider, update.event_id)
        return None

    agent, channel, user_id = update.agent, update.channel, update.user_id

    # Events that carry only a subscription id → resolve the record by it.
    if not user_id:
        existing = store.find_by_subscription(update.provider, update.external_sub_id)
        if existing is None:
            logger.warning(
                "No entitlement record for %s subscription %s (event %s); skipping",
                update.provider,
                update.external_sub_id,
                update.event_id,
            )
            return None
        agent, channel, user_id = existing.agent, existing.channel, existing.user_id

    record = store.get(agent, channel, user_id) or EntitlementRecord(
        agent=agent, channel=channel, user_id=user_id
    )
    record.status = update.status
    record.provider = update.provider or record.provider
    if update.current_period_end is not None:
        record.current_period_end = update.current_period_end
    if update.external_customer_id:
        record.external_customer_id = update.external_customer_id
    if update.external_sub_id:
        record.external_sub_id = update.external_sub_id
    store.upsert(record)
    logger.info(
        "Entitlement %s → %s [agent=%s channel=%s user=%s]",
        update.provider,
        record.status.value,
        agent,
        channel,
        user_id,
    )
    return record


def _price_line(config, *, prefix: str = "Price: ") -> str:
    """Formatted price line for the current agent, or '' when unpriced."""
    from olano.monetization.pricing import format_price

    mon = getattr(config, "monetization", None)
    plan = getattr(mon, "plan", None)
    providers = list(getattr(mon, "providers", []) or [])
    price = format_price(plan, providers)
    return f"{prefix}{price}" if price else ""


def welcome_text(
    agent: str, channel: str, config, *, user_id: str, now: float | None = None
) -> str:
    """Onboarding message shown on first contact / ``/start``.

    Announces the free trial, the price (auto-formatted from config), and how to
    pay — so a brand-new user immediately understands the deal. Reports days-left
    when a trial record already exists, else the full ``trial_days``. Read-only.
    """
    import time

    from olano.monetization.gate import _trial_end

    now = time.time() if now is None else now
    mon = getattr(config, "monetization", None)
    trial_days = int(getattr(mon, "trial_days", 3) or 0)
    record = get_entitlement_store().get(agent, channel, user_id)

    if record is not None and record.status is EntitlementStatus.ACTIVE:
        # Already subscribed — welcome them back, don't pitch the trial.
        return "👋 Welcome back! Your subscription is active. Send /billing for details."

    if record is not None and record.status is EntitlementStatus.TRIAL:
        end = _trial_end(record, trial_days)
        left = max(0, int((end - now) // 86_400) + 1)
        trial_line = f"You're on a free trial — about {left} day(s) left."
    else:
        trial_line = f"You have a {trial_days}-day free trial."

    lines = ["👋 Welcome!", trial_line]
    price = _price_line(config, prefix="After that it's ")
    if price:
        lines.append(price + ".")
    lines.append("Tap /subscribe when you're ready · /billing to check your status.")
    return "\n".join(lines)


def entitled_user_ids(agent: str, channel: str) -> list[str]:
    """User ids that should receive a proactive push (active / valid trial)."""
    return get_entitlement_store().entitled_user_ids(agent, channel)


async def send_confirmation(agent: str, channel: str, user_id: str, text: str) -> None:
    """Best-effort DM to a user after a state change (never raises)."""
    try:
        from olano.platforms import MessageEnvelope, get_channel_registry

        registry = get_channel_registry()
        await registry.send(
            MessageEnvelope(text=text, target=str(user_id), channel_type=channel, agent_name=agent)
        )
    except Exception as exc:
        logger.debug("Confirmation DM to %s failed: %s", user_id, exc)


def status_summary(agent: str, channel: str, config, *, now: float | None = None) -> str:
    """Human-readable subscription status for the /billing and /trial commands."""
    import time

    from olano.monetization.gate import _trial_end
    from olano.security.request_context import get_request_context

    now = time.time() if now is None else now
    mon = getattr(config, "monetization", None)
    trial_days = int(getattr(mon, "trial_days", 3) or 0)
    store = get_entitlement_store()
    record = store.get(agent, channel, get_request_context().user_id)
    price = _price_line(config, prefix=" · ")
    if record is None:
        return (
            f"You're on a free {trial_days}-day trial{price}. Send /subscribe to continue after it."
        )
    if record.status == EntitlementStatus.ACTIVE:
        if record.current_period_end:
            when = time.strftime("%Y-%m-%d", time.gmtime(record.current_period_end))
            return f"✅ Subscription active{price} — renews {when}."
        return f"✅ Subscription active{price}."
    if record.status == EntitlementStatus.TRIAL:
        end = _trial_end(record, trial_days)
        left = max(0, int((end - now) // 86_400) + 1)
        return f"⏳ Free trial: about {left} day(s) left{price}. Send /subscribe to keep access."
    if record.status == EntitlementStatus.PAST_DUE:
        return "⚠️ Your last payment failed. Send /subscribe to restore access."
    if record.status == EntitlementStatus.CANCELED:
        return "Your subscription is canceled and won't renew. Send /subscribe to resubscribe."
    return "🔒 Your access has ended. Send /subscribe to subscribe."
