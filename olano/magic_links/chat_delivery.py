"""Deliver a finished magic link's outcome to the **dashboard chat**.

The messenger case has always worked: ``watch.finalize`` builds a ✅/⚠️ notice
and hands it to the channel registry, which puts it in the Telegram/WhatsApp
conversation the link was minted from. The dashboard had no equivalent, and the
failure was silent in a way that made the product look broken:

* the chat's own Connect card promises *"the connection completes here on its
  own after you sign in"*, and ``magic_link_tools._mint`` tells the model *"I
  will post the result to this chat myself when they finish"*;
* ``watch._NON_CHANNEL_SOURCES`` dropped ``api`` — the dashboard's channel — so
  the notice was built and discarded;
* and even without that guard there was nothing to deliver *through*:
  ``APIServerPlatform.send()`` is a stub that always fails, because a dashboard
  chat is an HTTP request/response surface with no outbound socket.

So the dashboard needs its own delivery, and it is two writes rather than a
send. **Persist** the notice as a marked row in the thread's JSONL, so the chat
shows it after a reload and in a second tab; and **push** an SSE frame, so a
chat that is open right now shows it without one. Neither is a message *from*
the agent — the model never said it, and it is deliberately not in the
checkpointer — it is an event about the conversation, which is exactly what the
chat's ``system`` row renders.

Both halves are best-effort and neither raises. ``finalize``'s contract is that
a dead surface can never turn a successful sign-in into a failure: the tokens
are on disk by the time we get here.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Marks a session row as a magic-link outcome. The dashboard's thread endpoint
#: hides ``system`` rows on purpose (prompt-update notices, context notes), and
#: this key is the one narrow exception it lets through — so the marker has to
#: be stable and is asserted by ``tests/test_magic_link_chat_delivery.py``.
EVENT_KIND = "magic_link"

#: Channels that are the dashboard chat rather than a messenger. ``api`` is what
#: ``runtime/api.py`` binds for a chat-stream turn.
DASHBOARD_CHANNELS = frozenset({"api"})

#: Both mint paths fall back to ``"owner"`` when they cannot name a chat
#: (``magic_link_tools._request_origin``, ``olano_admin._spawn_oauth_watcher``).
#: A messenger resolves that to the owner's DM; here it is not a thread id, and
#: taking it literally would create an ``owner.jsonl`` transcript nothing reads.
_NO_THREAD = frozenset({"", "owner"})


def deliver(
    *,
    agent: str,
    thread_id: str,
    text: str,
    link_id: str,
    status: str,
    flow: str,
    label: str,
    target_agent: str,
    apply: str,
) -> bool:
    """Put a completed link's outcome in the dashboard thread it came from.

    ``agent`` is the agent whose chat asked (the thread's owner);
    ``target_agent`` is the agent the link configured, which is often a
    different one — an operator asks AgentFather to connect WeChat for Curtis,
    so the notice belongs in AgentFather's thread and the Restart button has to
    name Curtis. Returns whether anything was delivered.
    """
    if not agent or thread_id in _NO_THREAD:
        # No thread means the link was minted before the turn context carried
        # one (or from a non-chat surface); there is no conversation to answer.
        return False
    payload: dict[str, Any] = {
        "link_id": link_id,
        "status": status,
        "flow": flow,
        "label": label,
        "target_agent": target_agent,
        "apply": apply,
    }
    persisted = _persist(agent, thread_id, text, payload)
    _push(agent, thread_id, text, payload)
    return persisted


def _persist(agent: str, thread_id: str, text: str, payload: dict[str, Any]) -> bool:
    """Append the marked row to the thread's JSONL via the agent's own runner.

    Through the runner, not a hand-rolled path join, because the runner is what
    knows where an agent's session dir is (``workspace.agent_session_dir``) and
    it already writes ``system`` rows this way for prompt-update notices.
    """
    try:
        from olano.tools.talk_to import get_bound_runtime

        runtime = get_bound_runtime()
        runner = runtime.get_runner(agent) if runtime is not None else None
        if runner is None:
            return False
        runner.append_session_entry(
            thread_id,
            {"role": "system", "event": EVENT_KIND, "content": text, **payload},
        )
        return True
    except Exception:  # a transcript write must never fail a completed sign-in
        logger.debug("magic-link: could not persist the chat notice", exc_info=True)
        return False


def _push(agent: str, thread_id: str, text: str, payload: dict[str, Any]) -> None:
    """Broadcast the outcome so an open chat renders it with no reload.

    ``agent`` rides in the frame because the per-subscriber SSE filter
    (``runtime/auth.event_visible_to_agents``) scopes a frame by ``data.agent``
    — an unattributed frame reaches members with no grant on this agent.
    """
    try:
        from olano.runtime.api import _push_event

        _push_event(
            "magic_link_updated",
            {"agent": agent, "thread_id": thread_id, "text": text, **payload},
        )
    except Exception:  # no runtime (CLI/tests) — the persisted row still stands
        logger.debug("magic-link: could not push the chat notice", exc_info=True)
