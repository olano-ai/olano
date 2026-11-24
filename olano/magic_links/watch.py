"""Completion watcher — drives a magic link to its end and reports back to chat.

Two jobs, both of which exist because a chat turn cannot poll:

* :func:`watch` — a background task per minted link that polls until the
  magic link reaches a terminal state, so a user who signs in ten minutes after
  the agent's turn ended still gets an answer. Without it, nobody would be
  observing a link opened from Telegram (the page's own poll loop only runs
  while the tab is open, and the tab is usually closed the moment the provider
  redirects).
* :func:`finalize` — the one-time side effects of a finished link: the
  adapter's ``post_complete`` hook (e.g. hot-reloading an agent's MCP tools)
  and the ✅/⚠️ notice to the exact chat the request came from.

Both are best-effort: tokens are already persisted by the time we get here, so
a dead channel or a missing runtime must never turn a successful sign-in into a
failure. Generalizes :mod:`olano.tools._mcp_oauth_watch`, which remains the
MCP-specific path for flows started directly by ``olano_authorize_mcp_server``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from olano.magic_links import chat_delivery, service, store
from olano.magic_links import flows as flow_registry

logger = logging.getLogger(__name__)

# Strong references to in-flight watchers (asyncio only weakly references a bare
# create_task result, so a fire-and-forget task can be collected mid-await).
_WATCHERS: set[asyncio.Task[None]] = set()

# Sources with no chat to answer at all: a CLI one-shot, or a context nobody
# bound. The dashboard used to be on this list — see ``_notify``.
_NON_CHANNEL_SOURCES = frozenset({"", "local", "cli"})

_POLL_INTERVAL = 2.0


async def _notify(notify: dict[str, Any], text: str, outcome: dict[str, Any]) -> None:
    """Best-effort push of a completion notice to the originating chat.

    Two surfaces, two mechanisms, one decision — which is why this is a fork
    and not a send. A messenger has an outbound socket, so the notice goes out
    as a message. The dashboard chat is HTTP request/response with no socket
    (``APIServerPlatform.send`` is a stub that always fails), so its notice is
    persisted into the thread and pushed over SSE instead; see
    :mod:`olano.magic_links.chat_delivery`.

    ``api`` sat in ``_NON_CHANNEL_SOURCES`` for exactly as long as there was no
    second mechanism — which meant a link minted from the dashboard chat built
    this notice and dropped it, while the card on screen promised the
    connection would "complete here on its own".
    """
    channel = str(notify.get("channel_type") or "")
    agent_name = str(notify.get("agent_name") or "")
    if not agent_name or channel in _NON_CHANNEL_SOURCES:
        return
    if channel in chat_delivery.DASHBOARD_CHANNELS:
        # The bare fact — the card renders the consequence as its own line, with
        # a Restart button where one would help.
        chat_delivery.deliver(
            agent=agent_name,
            thread_id=str(notify.get("target") or ""),
            text=text,
            **outcome,
        )
        return
    # A messenger has one line of prose and no buttons, so the consequence has
    # to ride in the sentence.
    tail = _apply_sentence(str(outcome.get("apply") or ""), str(outcome.get("target_agent") or ""))
    if tail:
        text = f"{text} {tail}"
    try:
        from olano.platforms import MessageEnvelope, get_channel_registry

        await get_channel_registry().send(
            MessageEnvelope(
                text=text,
                target=str(notify.get("target") or "owner"),
                channel_type=channel,
                agent_name=agent_name,
            )
        )
    except Exception as exc:  # delivery is best-effort
        logger.debug("magic-link: completion notice not delivered: %s", exc)


def _success_text(record: store.LinkRecord, label: str) -> str:
    detail = record.result or f"{label} is connected."
    return f"✅ {detail}"


def _apply_sentence(apply: str, agent: str) -> str:
    """What the outcome means for the agent, in one sentence.

    Only for surfaces that cannot render the dashboard's card. Kept in step
    with ``ConnectResultCard``'s notes — same three states, same claims — so a
    Telegram user and a dashboard user are told the same thing.
    """
    who = agent or "The agent"
    if apply == flow_registry.APPLY_RESTARTED:
        return f"{who} has been restarted, so it is live now."
    if apply == flow_registry.APPLY_RELOADED:
        return f"{who}'s tools were reloaded, so they are usable now."
    if apply == flow_registry.APPLY_RESTART_NEEDED:
        return f"Restart {who} for it to pick this up."
    return ""


def _failure_text(record: store.LinkRecord, label: str) -> str:
    reason = (record.error or "").strip()
    if reason and not reason.endswith((".", "!", "?")):
        reason += "."
    tail = f" {reason}" if reason else ""
    if record.status == store.STATUS_EXPIRED:
        return f"⚠️ The {label} magic link expired before it was used.{tail} Ask me for a new one."
    return f"⚠️ {label} didn't complete.{tail} Ask me to try again."


async def finalize(link_id: str) -> None:
    """Run a finished magic link's side effects **once** — notice + post_complete.

    ``store.claim_finalize`` is the atomic, persisted guard: it returns True for
    exactly one caller, so the page poll, the background watcher, and the
    restart resume can all call this and only one runs. Persisted, so a restart
    never re-notifies a link that already finished.
    """
    if not store.claim_finalize(link_id):
        return
    record = store.get(link_id)
    if record is None or not record.is_terminal:
        return
    flow = flow_registry.get_flow(record.flow)
    label = getattr(flow, "label", record.flow) if flow else record.flow

    # What post_complete actually managed — a restart it could not run, a
    # hot-reload that failed — outranks whatever the flow declares, so the
    # notice can offer the restart the user still needs instead of claiming a
    # success that only happened on paper. A hook that raised has, by
    # definition, applied nothing.
    reported: str | None = None
    if record.status == store.STATUS_DONE and flow is not None:
        try:
            reported = await flow.post_complete(record)
        except Exception as exc:  # a hook failure never invalidates the token
            logger.warning("magic-link: post_complete failed for %s: %s", link_id, exc)
            reported = flow_registry.APPLY_RESTART_NEEDED

    text = (
        _success_text(record, label)
        if record.status == store.STATUS_DONE
        else _failure_text(record, label)
    )
    outcome = {
        "link_id": record.id,
        "status": record.status,
        "flow": record.flow,
        "label": label,
        "target_agent": str(record.params.get("agent") or ""),
        # A link that did not finish cleanly applied nothing, whatever its flow
        # declares — never offer to restart an agent for a sign-in that failed.
        "apply": (
            flow_registry.resolve_apply(flow, record, reported)
            if record.status == store.STATUS_DONE and flow is not None
            else flow_registry.APPLY_NONE
        ),
    }
    await _notify(record.notify, text, outcome)


async def watch(link_id: str, *, deadline_seconds: float | None = None) -> None:
    """Poll ``link_id`` until it finishes, then let the service finalize it.

    The polling itself is what advances the link: :func:`service.link_state`
    reads the adapter, persists any status change, and schedules
    :func:`finalize` on the transition to terminal — so this loop only has to
    keep asking until that happens (or the link's own TTL runs out).
    """
    record = store.get(link_id)
    if record is None:
        return
    if deadline_seconds is None:
        # Outlive the link itself by a couple of polls so an expiry is observed,
        # persisted, and reported rather than silently abandoned.
        from olano.config.system_settings import load_magic_link_settings

        deadline_seconds = load_magic_link_settings().ttl_minutes * 60.0 + _POLL_INTERVAL * 2

    waited = 0.0
    while waited < deadline_seconds:
        record = store.get(link_id)
        if record is None:
            return
        if record.is_terminal:
            return  # link_state already scheduled finalize
        try:
            service.link_state(record)
        except Exception as exc:  # never let one bad poll kill the watcher
            logger.debug("magic-link: poll failed for %s: %s", link_id, exc)
        record = store.get(link_id)
        if record is not None and record.is_terminal:
            return
        await asyncio.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL


def spawn(link_id: str) -> bool:
    """Schedule :func:`watch` on the running loop. False when there is none."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("magic-link: no running loop — not watching %s", link_id)
        return False
    task = loop.create_task(watch(link_id))
    _WATCHERS.add(task)
    task.add_done_callback(_WATCHERS.discard)
    return True


async def resume_pending() -> int:
    """Re-arm watchers after a restart; expire links whose flow died with us.

    In-flight *flow* sessions (the OAuth/device-code state machines) live in
    memory, so a restart orphans them: the link record survives but nothing can
    ever complete it. Rather than leave the user staring at a page that will
    never advance, mark those expired and tell the chat to ask for a new link.
    A link that had not been started yet is still perfectly good — re-arm it.
    """
    resumed = 0
    for record in store.list_all(include_terminal=False):
        if record.session_id:
            store.finish(
                record.id,
                status=store.STATUS_EXPIRED,
                error="The server restarted while this link was open.",
            )
            await finalize(record.id)
            continue
        if spawn(record.id):
            resumed += 1
    if resumed:
        logger.info("magic-link: resumed %d pending link watcher(s)", resumed)
    return resumed
