"""Self-serve monetization commands: /subscribe, /billing, /trial.

These are handled *before* the entitlement gate so a blocked user can still run
the one command that unblocks them. The handler has the live channel adapter, so
``/subscribe`` presents the real offer (inline Stripe button / Stars invoice)
rather than only returning text.
"""

from __future__ import annotations

import logging
from typing import Any

from olano.monetization import service
from olano.platforms.inbound import command_name

logger = logging.getLogger(__name__)

_COMMANDS = {"subscribe", "billing", "trial"}


async def handle_monetization_command(
    text: str,
    *,
    agent: str,
    channel_name: str,
    channel: Any,
    recipient: str,
    config: Any,
    is_owner: bool,
) -> str | None:
    """Handle a monetization slash command; return a reply, or ``None`` if N/A.

    Returns ``None`` when the text isn't a monetization command or the agent
    isn't monetized, so the caller falls through to normal handling.
    """
    mon = getattr(config, "monetization", None)
    if mon is None or not getattr(mon, "enabled", False):
        return None
    name = command_name(text)
    if name not in _COMMANDS:
        return None

    if is_owner:
        return "You're the owner of this bot — you have full access and are never charged."

    if name in ("billing", "trial"):
        return service.status_summary(agent, channel_name, config)

    # /subscribe → present the offer through the channel if it can render one,
    # and always return a text fallback.
    from olano.security.request_context import get_request_context

    user_id = get_request_context().user_id
    offer = await service.build_subscription_offer(agent, channel_name, user_id, config)
    sent = False
    if hasattr(channel, "send_subscription_offer"):
        try:
            sent = await channel.send_subscription_offer(recipient, offer)
        except Exception as exc:
            logger.warning("send_subscription_offer failed for agent=%s: %s", agent, exc)
    if sent:
        return "👆 Your subscription options are above."
    if offer.stripe_url:
        return f"{offer.text}\n\nSubscribe here: {offer.stripe_url}"
    return offer.text
