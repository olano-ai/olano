"""The entitlement gate: may this end-user get a model turn right now?

Pure decision logic — no network, no offer building (that's ``service``), so it
is trivially unit-testable with an injected ``now``. The user identity comes
from the request context (``get_request_context().user_id``), **never** a model
argument — the same discipline as ``memory/customer_memory.active_customer_id``,
which is what makes a prompt-injected "pretend I paid" a no-op.

Coexistence with the allowlist: the caller only invokes the gate for *untrusted*
senders (``sender.is_trusted`` False). The owner/allowlisted users bypass
entirely and are never charged.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from olano.memory.customer_memory import sanitize_identity
from olano.monetization.store import (
    EntitlementRecord,
    EntitlementStatus,
    EntitlementStore,
    get_entitlement_store,
)
from olano.security.request_context import get_request_context

logger = logging.getLogger(__name__)

_DAY_SECONDS = 86_400


@dataclass
class AccessDecision:
    """Outcome of the gate for one inbound message."""

    allowed: bool
    reason: str = ""
    record: EntitlementRecord | None = None
    # Days left in the trial (informational; only meaningful while on trial).
    trial_days_left: int | None = None
    # Paywall text to return to a blocked user (the always-works fallback shown
    # even on channels that can't render an inline offer).
    message: str = ""


def _trial_end(record: EntitlementRecord, trial_days: int) -> float:
    start = record.trial_start or record.updated_at or 0.0
    return start + trial_days * _DAY_SECONDS


def evaluate_access(
    agent: str,
    channel: str,
    config,
    *,
    now: float | None = None,
    store: EntitlementStore | None = None,
) -> AccessDecision:
    """Decide whether the current end-user may proceed, mutating trial state.

    Side effects (intentional, and the only ones): a first-seen user has a trial
    record created; a lapsed trial / ended paid period is persisted as
    ``EXPIRED``. Everything else is a read.
    """
    now = time.time() if now is None else now
    mon = getattr(config, "monetization", None)
    trial_days = int(getattr(mon, "trial_days", 3) or 0)

    ctx = get_request_context()

    # Groups: billing a shared chat is ambiguous. Unless the owner opted in,
    # leave groups open (the offer copy tells users to DM to subscribe).
    if ctx.is_group and not getattr(mon, "gate_groups", False):
        return AccessDecision(allowed=True, reason="group_open")

    user_id = sanitize_identity(ctx.user_id)
    if not user_id:
        # No stable identity to bill → can't gate; fail open and log.
        logger.warning("Monetization gate: no user_id in context for agent=%s; allowing", agent)
        return AccessDecision(allowed=True, reason="no_identity")

    store = store or get_entitlement_store()
    record = store.get(agent, channel, user_id)

    # First-seen → start the trial lazily and allow.
    if record is None:
        record = EntitlementRecord(
            agent=agent,
            channel=channel,
            user_id=user_id,
            status=EntitlementStatus.TRIAL,
            trial_start=now,
        )
        store.upsert(record)
        return AccessDecision(
            allowed=True,
            reason="trial_started",
            record=record,
            trial_days_left=trial_days,
        )

    status = record.status
    period_end = record.current_period_end

    if status == EntitlementStatus.ACTIVE:
        if period_end is None or period_end > now:
            return AccessDecision(allowed=True, reason="active", record=record)
        # Paid period ended with no renewal → expire and block.
        return _expire(store, record, config)

    if status == EntitlementStatus.TRIAL:
        end = _trial_end(record, trial_days)
        if now < end:
            left = max(0, int((end - now) // _DAY_SECONDS) + 1)
            return AccessDecision(allowed=True, reason="trial", record=record, trial_days_left=left)
        return _expire(store, record, config)

    if status in (EntitlementStatus.PAST_DUE, EntitlementStatus.CANCELED):
        # Keep serving until the already-paid period runs out, then block.
        if period_end is not None and period_end > now:
            return AccessDecision(allowed=True, reason=status.value, record=record)
        return _expire(store, record, config)

    # NONE / EXPIRED → blocked.
    return AccessDecision(
        allowed=False, reason=status.value, record=record, message=_paywall_text(config)
    )


def _expire(store: EntitlementStore, record: EntitlementRecord, config) -> AccessDecision:
    if record.status != EntitlementStatus.EXPIRED:
        record.status = EntitlementStatus.EXPIRED
        store.upsert(record)
    return AccessDecision(
        allowed=False, reason="expired", record=record, message=_paywall_text(config)
    )


def _paywall_text(config) -> str:
    from olano.monetization.pricing import format_price

    mon = getattr(config, "monetization", None)
    plan = getattr(mon, "plan", None)
    copy = getattr(plan, "offer_copy", "") or "Subscribe to continue."
    price = format_price(plan, list(getattr(mon, "providers", []) or []))
    price_line = f" ({price})" if price else ""
    return f"🔒 {copy}{price_line}\n\nSend /subscribe to get access."
