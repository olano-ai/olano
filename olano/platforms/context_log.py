"""Fold proactive outbound deliveries into the target conversation's history.

When an agent pushes a message into a chat from *outside* that chat's own
conversation — a notification tool called during a scheduled task, a PULSE
digest, an MCP OAuth notice — the delivered content used to exist only in the
thread that produced it. The recipient then replies in their chat ("tell me
more about #2") and the agent serving that conversation has never seen the
notification it is being asked about.

:func:`record_outbound_delivery` closes that gap: after
:meth:`~olano.platforms.delivery.ChannelRegistry.send` delivers an
envelope, the content is recorded as a context note on the conversation the
reply will arrive in (``AgentRunner.record_context_note``), keyed by the
adapter's :meth:`outbound_thread_key` — the same base thread id the inbound
handler derives for that chat. A send into the very conversation the agent is
currently serving is skipped: the tool call and its result are already in that
thread's history.

Everything here is best-effort by contract: recording context must never fail
a delivery that already succeeded.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Notification texts can be long digests; keep the recorded note bounded so
# thread-state files and the injected system message stay small. The JSONL
# transcript row is capped identically (it stores the same note).
_NOTE_TEXT_LIMIT = 4000


def _active_runner(agent_name: str) -> Any | None:
    """The booted AgentRunner for *agent_name*, or ``None`` outside a runtime.

    Resolved through the runtime API's ``_ACTIVE_RUNTIME`` — via
    ``sys.modules`` on purpose: if ``olano.runtime.api`` was never imported
    there is no runtime in this process (CLI one-shot, unit test), so there is
    nothing to record into and nothing worth importing either.
    """
    api = sys.modules.get("olano.runtime.api")
    runtime = getattr(api, "_ACTIVE_RUNTIME", None) if api else None
    get = getattr(runtime, "get_runner", None)
    try:
        return get(agent_name) if callable(get) else None
    except Exception:  # pragma: no cover - defensive
        return None


def _describe(envelope: Any) -> str:
    """A self-describing context note for one delivered envelope."""
    channel = str(getattr(envelope, "channel_type", "") or "channel")
    text = str(getattr(envelope, "text", "") or "").strip()
    if len(text) > _NOTE_TEXT_LIMIT:
        text = text[:_NOTE_TEXT_LIMIT] + " … [truncated]"
    media_url = str(getattr(envelope, "media_url", "") or "")
    if media_url:
        kind = str(getattr(envelope, "media_type", "") or "file")
        base = f"You delivered a {kind} attachment ({media_url}) to this chat via {channel}"
        return f'{base} with the caption: "{text}"' if text else f"{base}."
    return f'You sent this message to this chat via {channel}: "{text}"'


def record_outbound_delivery(envelope: Any, adapter: Any, resolved_target: str) -> None:
    """Record a successfully delivered envelope into the target conversation.

    ``resolved_target`` is the real address the send went to (post
    ``resolve_target`` — never the ``"owner"`` sentinel). Adapters without an
    ``outbound_thread_key`` (the deprecated classic channels, whose inbound
    thread ids are process-random) are skipped: a note recorded under a key no
    inbound message ever resolves to would simply strand.
    """
    agent = str(getattr(envelope, "agent_name", "") or "")
    if not agent:
        return
    thread_key_fn = getattr(adapter, "outbound_thread_key", None)
    if not callable(thread_key_fn):
        return
    runner = _active_runner(agent)
    if runner is None:
        return
    base_thread = str(
        thread_key_fn(str(resolved_target), getattr(envelope, "metadata", None) or None) or ""
    )
    if not base_thread:
        return
    # The conversation currently executing (runner.astream_events/ainvoke bind
    # session_id to the streaming thread). A send back into it is already in
    # its history as the tool call + result — record_context_note skips it.
    from olano.security.request_context import get_request_context

    current_thread = str(getattr(get_request_context(), "session_id", "") or "")
    recorded = runner.record_context_note(
        base_thread, _describe(envelope), skip_thread_id=current_thread or None
    )
    if recorded:
        logger.debug(
            "Recorded outbound %s delivery for agent '%s' into thread %s",
            getattr(envelope, "channel_type", "?"),
            agent,
            recorded,
        )
