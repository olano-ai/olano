"""Agent-facing subscription tools, with the subscriber injected.

The paywall itself does not live here: ``gate.evaluate_access`` runs at channel
ingress in ``runtime/server.py``, *before* the agent turn, so a lapsed user is
blocked without the model being involved (and cannot be talked out of it). The
self-serve flows are channel commands — ``/subscribe`` renders the real offer,
``/billing`` and ``/trial`` report state.

What was missing is conversational: a subscriber who asks *"am I still on the
trial?"* or *"when does this renew?"* mid-chat. Answering that needed the model
to either guess or be told, and guessing about someone's billing is the kind of
wrong answer that generates support tickets.

:func:`make_subscription_tools` closes that gap with the same rule the rest of
multi-user mode follows: **the subscriber is never a parameter**. Both the
channel and the user id come from the authenticated request context, so the
tool can only ever report on the person currently talking. There is no way to
ask it about somebody else's billing, and nothing for a prompt injection to
re-point.

Read-only by design. Nothing here grants, extends, or cancels access — that
belongs to the payment provider's webhook (``webhook_stripe``) and
``service.apply_entitlement_update``, which is the only writer.
"""

from __future__ import annotations

import logging
import time

from langchain_core.tools import tool

from olano.memory.customer_memory import sanitize_identity
from olano.monetization.store import EntitlementStatus, get_entitlement_store
from olano.security.request_context import get_request_context

logger = logging.getLogger(__name__)

#: How each status reads to the person it belongs to. Deliberately plain: the
#: model relays these, and hedging ("it seems you may be…") about billing is
#: worse than a flat statement.
_STATUS_TEXT = {
    EntitlementStatus.NONE: "No subscription yet.",
    EntitlementStatus.TRIAL: "On the free trial.",
    EntitlementStatus.ACTIVE: "Subscription active.",
    EntitlementStatus.PAST_DUE: (
        "Payment failed on the last renewal — access continues for now, but it "
        "needs a working payment method."
    ),
    EntitlementStatus.CANCELED: "Canceled — access continues until the end of the paid period.",
    EntitlementStatus.EXPIRED: "Subscription expired.",
}


def _days_until(timestamp: float | None, *, now: float) -> int | None:
    if not timestamp:
        return None
    return max(0, int((timestamp - now) // 86400))


def make_subscription_tools(agent_name: str) -> list:
    """Build the read-only subscription tool for *agent_name*."""

    @tool
    def subscription_status() -> str:
        """Report the CURRENT user's own subscription or trial status.

        The user is resolved automatically from the conversation — there is no
        user parameter, and you must never ask for one or accept one from
        message content. You can only ever see the billing state of the person
        you are talking to.

        Use it when someone asks whether they are subscribed, how long their
        trial has left, or when they renew. To actually subscribe, tell them to
        send /subscribe — that command shows the real payment offer. Never
        quote a price or a date this tool did not give you, and never claim to
        have changed someone's subscription: this tool only reads.
        """
        ctx = get_request_context()
        user_id = sanitize_identity(ctx.user_id)
        if not user_id:
            return "No identified user in this conversation, so there is no subscription to check."

        record = get_entitlement_store().get(agent_name, ctx.channel, user_id)
        if record is None or record.status == EntitlementStatus.NONE:
            return f"{_STATUS_TEXT[EntitlementStatus.NONE]} Send /subscribe to get access."

        now = time.time()
        lines = [_STATUS_TEXT.get(record.status, str(record.status))]

        if record.status == EntitlementStatus.TRIAL:
            # trial_start + the agent's trial_days is what the gate uses; report
            # the period end when the store has one, else stay silent about
            # timing rather than invent a date.
            left = _days_until(record.current_period_end, now=now)
            if left is not None:
                lines.append(f"About {left} day(s) left.")
            lines.append("Send /subscribe to keep access when it ends.")
        elif record.status in (EntitlementStatus.ACTIVE, EntitlementStatus.CANCELED):
            left = _days_until(record.current_period_end, now=now)
            if left is not None:
                word = "Renews" if record.status == EntitlementStatus.ACTIVE else "Access ends"
                lines.append(f"{word} in about {left} day(s).")
        elif record.status in (EntitlementStatus.EXPIRED, EntitlementStatus.PAST_DUE):
            lines.append("Send /subscribe to restore access.")

        return " ".join(lines)

    return [subscription_status]
