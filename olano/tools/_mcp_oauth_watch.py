"""Background completion-watcher for chat-initiated MCP OAuth flows.

When ``olano_authorize_mcp_server`` starts an OAuth flow from a messenger
(Telegram, WhatsApp, Slack, …) it returns the magic link and the agent turn
ends. The user then authorizes in a browser out-of-band; the callback is
captured by the relay (or public-origin callback), the code is fed back in, and
the flow reaches a terminal state on the runtime loop — with **nothing left
watching it**. Unlike the dashboard, a chat turn can't poll, so on that path:

* the just-unlocked tools never go live until a manual reload/restart, and
* the user is never told the sign-in actually worked (or failed).

This module closes both gaps. :func:`spawn_watcher` schedules a background task
that waits for the flow to finish and then, on success:

* hot-reloads the target agent's MCP client so the new tools appear without a
  restart (the runtime equivalent of the dashboard's poll-triggered reload), and
* sends a completion notice back to the exact chat the request came from.

On failure it sends a short "didn't complete" notice instead. Both effects are
best-effort: a missing runtime (CLI/tests) or an undeliverable channel is
logged, never raised — the OAuth tokens are already persisted on disk regardless
of whether the reload or the notification succeeds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from olano.magic_links import chat_delivery
from olano.magic_links import store as link_store
from olano.magic_links.flows import APPLY_NONE, APPLY_RELOADED, APPLY_RESTART_NEEDED

logger = logging.getLogger(__name__)

# Strong references to in-flight watcher tasks. asyncio keeps only a weak
# reference to a bare ``create_task`` result, so without this a fire-and-forget
# watcher can be garbage-collected mid-await. Each task discards itself on done.
_WATCHER_TASKS: set[asyncio.Task[None]] = set()

# session ids already hot-reloaded, so the watcher (and any later manual poll)
# never rebuild the same agent twice for one authorization.
_RELOADED_SESSIONS: set[str] = set()

# Flow statuses that mean the OAuth handshake is over. Mirrors
# ``mcp_setup._TERMINAL_OAUTH_STATUSES`` (kept local so the watcher never depends
# on a private symbol and tests need not patch it).
_TERMINAL_STATUSES = frozenset({"authorized", "error", "expired"})

# Sources with no chat to answer at all: a CLI one-shot, or a context nobody
# bound. The dashboard used to be on this list — see ``_notify``.
_NON_CHANNEL_SOURCES = frozenset({"", "local"})

_POLL_INTERVAL = 1.0
# Bound the watcher's lifetime. The flow itself is capped by
# ``OAUTH_FLOW_TIMEOUT`` (300s), so a little beyond that guarantees the task
# exits even if the session is pruned out from under it.
_WATCH_DEADLINE = 360.0


def _server_label(url: str) -> str:
    """A short human name for an MCP server URL (its host, else the raw URL)."""
    return urlparse(url).hostname or url or "the MCP server"


async def _reload_agent_mcp(session_id: str, agent: str) -> dict[str, Any] | None:
    """Hot-reload ``agent``'s MCP tools once per authorization (idempotent).

    Returns the reload summary, or ``None`` when it was skipped (already done for
    this session, no bound runtime, or the reload raised).
    """
    if session_id in _RELOADED_SESSIONS:
        return None
    _RELOADED_SESSIONS.add(session_id)

    from olano.tools.talk_to import get_bound_runtime

    runtime = get_bound_runtime()
    if runtime is None or not hasattr(runtime, "reload_agent_mcp"):
        logger.debug("MCP OAuth watcher: no bound runtime — skipping reload for %s", agent)
        return None
    try:
        result: dict[str, Any] = await runtime.reload_agent_mcp(agent)
        return result
    except Exception as exc:  # a reload hiccup must never break the watcher
        logger.warning("MCP OAuth watcher: reload failed for %s: %s", agent, exc)
        return None


async def _notify(
    agent_name: str,
    channel: str,
    target: str,
    text: str,
    outcome: dict[str, Any],
) -> None:
    """Best-effort push of a completion notice to the originating chat.

    The same fork as ``magic_links.watch._notify``, and here for the same
    reason: the dashboard chat has no outbound socket to send a message on, so
    its notice is persisted into the thread and pushed over SSE instead. Until
    that second mechanism existed, ``api`` sat in ``_NON_CHANNEL_SOURCES`` and
    an MCP sign-in started from the dashboard reported to nobody.
    """
    if not agent_name or not channel or channel in _NON_CHANNEL_SOURCES:
        return
    if channel in chat_delivery.DASHBOARD_CHANNELS:
        # Required, not defaulted: `deliver` takes the whole payload as keyword
        # arguments, so an absent outcome would be a TypeError inside a
        # best-effort notice rather than a degraded card.
        chat_delivery.deliver(agent=agent_name, thread_id=target, text=text, **outcome)
        return
    try:
        from olano.platforms import MessageEnvelope, get_channel_registry

        registry = get_channel_registry()
        await registry.send(
            MessageEnvelope(
                text=text,
                target=target or "owner",
                channel_type=channel,
                agent_name=agent_name,
            )
        )
    except Exception as exc:  # delivery is best-effort; log and move on
        logger.debug("MCP OAuth watcher: completion notice not delivered: %s", exc)


def _success_message(agent: str, session: Any, reload_result: dict[str, Any] | None) -> str:
    host = _server_label(session.url)
    count = int(getattr(session, "tool_count", 0) or 0)
    tools = f"{count} tool{'s' if count != 1 else ''}"
    msg = f"✅ Sign-in complete — **{agent}** is now connected to {host} ({tools})."
    if reload_result is not None and not reload_result.get("ok", True):
        msg += " (A restart may be needed before the tools are usable.)"
    return msg


def _failure_message(agent: str, session: Any) -> str:
    host = _server_label(session.url)
    reason = (getattr(session, "error", None) or getattr(session, "message", None) or "").strip()
    if reason and not reason.endswith((".", "!", "?")):
        reason += "."
    tail = f" {reason}" if reason else ""
    return (
        f"⚠️ Sign-in for **{agent}** → {host} didn't complete.{tail} "
        "Ask me to try authorizing it again."
    )


def _outcome(agent: str, session: Any, apply: str) -> dict[str, Any]:
    """The structured half of the notice, for the dashboard card.

    ``link_id`` is the OAuth session id here rather than an ``hs_`` link — this
    path predates magic links and never mints one — but it plays the same role:
    the key the chat dedupes a repeated delivery on.

    ``status`` speaks the magic-link store's vocabulary, not the OAuth
    session's: the card reads ``"done"`` as success and anything else as a
    failure, so passing the session's own ``"authorized"`` through drew every
    successful sign-in under a "Didn't complete" banner.
    """
    status = str(getattr(session, "status", "") or "")
    return {
        "link_id": str(getattr(session, "session_id", "") or ""),
        "status": link_store.STATUS_DONE if status == "authorized" else status,
        "flow": "mcp-oauth",
        "label": _server_label(getattr(session, "url", "")),
        "target_agent": agent,
        "apply": apply,
    }


async def _watch(
    session_id: str,
    agent: str,
    *,
    notify_agent: str,
    notify_channel: str,
    notify_target: str,
) -> None:
    """Await terminal status for ``session_id``, then reload + notify."""
    from olano.core import mcp_setup

    session = mcp_setup.poll_oauth_flow(session_id)
    waited = 0.0
    while waited < _WATCH_DEADLINE:
        session = mcp_setup.poll_oauth_flow(session_id)
        if session is None or session.status in _TERMINAL_STATUSES:
            break
        await asyncio.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL

    if session is None:
        logger.debug("MCP OAuth watcher: session %s vanished before completion", session_id)
        return

    if session.status == "authorized":
        result = await _reload_agent_mcp(session_id, agent)
        # A reload that ran means the tools are live; one that was skipped or
        # failed leaves the agent signed in and still blind to them, which is
        # the case worth offering a restart for.
        reloaded = result is not None and result.get("ok", True)
        await _notify(
            notify_agent,
            notify_channel,
            notify_target,
            _success_message(agent, session, result),
            _outcome(agent, session, APPLY_RELOADED if reloaded else APPLY_RESTART_NEEDED),
        )
    elif session.status in _TERMINAL_STATUSES:  # error | expired
        await _notify(
            notify_agent,
            notify_channel,
            notify_target,
            _failure_message(agent, session),
            # Nothing was applied by a sign-in that did not complete.
            _outcome(agent, session, APPLY_NONE),
        )


def spawn_watcher(
    session_id: str,
    agent: str,
    *,
    notify_agent: str,
    notify_channel: str,
    notify_target: str,
) -> bool:
    """Schedule the completion watcher on the running loop.

    Returns ``True`` if a watcher was scheduled, ``False`` when there is no
    running event loop to attach it to (e.g. a sync CLI context) — the caller
    can ignore the result; a skipped watcher just means no auto-reload/notice.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("MCP OAuth watcher: no running loop — not spawning for %s", session_id)
        return False
    task = loop.create_task(
        _watch(
            session_id,
            agent,
            notify_agent=notify_agent,
            notify_channel=notify_channel,
            notify_target=notify_target,
        )
    )
    _WATCHER_TASKS.add(task)
    task.add_done_callback(_WATCHER_TASKS.discard)
    return True
