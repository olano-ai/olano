"""Human-in-the-loop approval queue for platform adapters.

Platform adapters render approval prompts as native UI — inline buttons on
Telegram/Discord, Block Kit actions on Slack, interactive cards on Feishu.
The agent turn that triggered the approval blocks on a per-session future
created by :func:`register_gateway_approval`; the platform's button/callback
handler then calls :func:`resolve_gateway_approval` to deliver the human's
choice and unblock the waiting turn.

Approvals are keyed by *session_key* (the platform conversation key produced
by :func:`olano.platforms.session.build_session_key`, e.g.
``"telegram:<chat_id>"``) so a click in a conversation resolves whatever that
conversation's agent turn is currently waiting on, without the UI layer
having to thread an opaque approval id back through the platform's callback
payload.

This is the shared, platform-agnostic half of HITL-on-platforms. The
per-adapter delivery side (rendering the prompt and awaiting the future)
mirrors the proven pattern in
``olano/channels/telegram_channel.py:_request_approval_button`` and is
wired up per platform as those adapters gain approval prompts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

logger = logging.getLogger(__name__)

# session_key -> FIFO list of futures awaiting a human decision. A session
# normally has at most one approval in flight (the turn blocks on it before
# continuing), but the list keeps concurrent prompts well-defined.
_pending: dict[str, list[asyncio.Future[str]]] = {}

# The choices a platform button can deliver. "once"/"always" approve;
# "deny" rejects; "timeout" is set by the awaiting side when no one clicks.
ApprovalChoice = Literal["once", "always", "deny", "timeout"]


def register_gateway_approval(session_key: str) -> asyncio.Future[str]:
    """Register and return a future that resolves to the human's choice.

    Called by the approval-request (blocking) side *before* it renders the
    platform prompt. The caller should ``await asyncio.wait_for(future, ...)``
    and, on timeout or cancellation, drop it via
    :func:`discard_gateway_approval` so a later click can't resolve a stale
    future.
    """
    loop = asyncio.get_event_loop()
    future: asyncio.Future[str] = loop.create_future()
    _pending.setdefault(session_key, []).append(future)
    return future


def discard_gateway_approval(session_key: str, future: asyncio.Future[str]) -> None:
    """Remove *future* from the queue (e.g. after timeout/cancellation).

    Safe to call more than once; prunes the session's entry when its last
    pending future is gone.
    """
    futures = _pending.get(session_key)
    if not futures:
        return
    try:
        futures.remove(future)
    except ValueError:
        pass
    if not futures:
        _pending.pop(session_key, None)


def pending_gateway_approvals(session_key: str) -> int:
    """Number of approvals for *session_key* still awaiting a decision."""
    return len(_pending.get(session_key, ()))


def resolve_gateway_approval(session_key: str, choice: str) -> int:
    """Deliver *choice* to every approval awaiting on *session_key*.

    Returns the number of pending approvals unblocked. A click for a session
    with nothing pending is a harmless no-op that returns ``0`` — the platform
    callback handlers treat the count as informational only.
    """
    futures = _pending.pop(session_key, [])
    resolved = 0
    for future in futures:
        if not future.done():
            future.set_result(choice)
            resolved += 1
    if resolved:
        logger.debug(
            "Gateway approval resolved: session=%s choice=%s count=%d",
            session_key,
            choice,
            resolved,
        )
    return resolved
