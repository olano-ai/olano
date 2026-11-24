"""Per-thread live events — the one place a conversation announces it moved.

A turn can arrive on any surface: the dashboard chat, Telegram, a scheduled
job, the OpenAI-compatible connector, a peer agent, a webhook. Until this
module existed, only the dashboard's own turn was visible to the dashboard —
every other surface appended to the JSONL transcript and returned, so a chat
open on a laptop or a phone learned nothing until a page reload.

Two frames, both published from :class:`olano.core.runner.AgentRunner` so no
caller has to remember to do it:

``thread_updated`` — one per transcript row, carrying ``{agent, thread_id,
  entry}`` where ``entry`` is the raw JSONL row exactly as written.
  ``runtime/api._push_event`` projects it into the same message shape
  ``GET /api/agents/{name}/threads/{id}`` serves (and drops rows that view
  hides), so what a live tab draws is, by construction, what a reload draws.

``thread_activity`` — ``{agent, thread_id, status}`` at turn start
  (``thinking``), throttled while text streams (``streaming``, with the text
  so far), and at turn end (``idle``). Also stamps the surface (``channel``)
  so a chat can say *where* the turn is happening.

Publishing goes through the publisher :func:`olano.tools.talk_to.bind_runtime`
installs (``runtime/api._push_event`` in the server) — the same seam the A2A
mirror and the delegation middleware already use — so a CLI run, a test, or a
cognition worker with no dashboard is a no-op, never an import of the runtime
package. Every helper here swallows its own failures: a broadcast must never
cost a turn.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

THREAD_UPDATED = "thread_updated"
THREAD_ACTIVITY = "thread_activity"

# Minimum spacing between two ``streaming`` frames for one turn. A frame per
# token would flood every /api/stream subscriber (their queues drop at 500)
# for a cross-device preview nobody can read faster than this anyway.
STREAM_PREVIEW_INTERVAL_S = 0.75


def _publisher() -> Any | None:
    try:
        from olano.tools import talk_to as tt

        return getattr(tt, "_PUBLISHER", None)
    except Exception:  # pragma: no cover - import guard
        return None


def publish(event_type: str, payload: dict[str, Any]) -> None:
    """Broadcast ``payload`` under ``event_type``; a no-op outside the server."""
    publisher = _publisher()
    if publisher is None:
        return
    try:
        publisher(event_type, payload)
    except Exception:
        logger.debug("thread event %s dropped", event_type, exc_info=True)


def publish_thread_updated(agent: str, thread_id: str, entry: dict[str, Any]) -> None:
    """Announce a row appended to ``<agent>/<thread_id>.jsonl``."""
    if not agent or not thread_id:
        return
    publish(THREAD_UPDATED, {"agent": agent, "thread_id": thread_id, "entry": dict(entry)})


def publish_thread_activity(
    agent: str,
    thread_id: str,
    status: str,
    *,
    channel: str = "",
    text: str | None = None,
) -> None:
    """Announce the turn state of a thread: ``thinking`` / ``streaming`` / ``idle``."""
    if not agent or not thread_id:
        return
    payload: dict[str, Any] = {"agent": agent, "thread_id": thread_id, "status": status}
    if channel:
        payload["channel"] = channel
    if text is not None:
        payload["text"] = text
    publish(THREAD_ACTIVITY, payload)


class StreamPreview:
    """Throttle for the ``streaming`` activity frames of one turn.

    ``feed(chunk)`` accumulates the reply so far and publishes at most one frame
    per :data:`STREAM_PREVIEW_INTERVAL_S`. ``flush()`` sends whatever is pending
    at turn end so the last words are never dropped by the throttle.
    """

    def __init__(self, agent: str, thread_id: str, *, channel: str = "") -> None:
        self.agent = agent
        self.thread_id = thread_id
        self.channel = channel
        self._parts: list[str] = []
        self._last_sent = 0.0
        self._dirty = False

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        self._parts.append(chunk)
        self._dirty = True
        now = time.monotonic()
        if now - self._last_sent >= STREAM_PREVIEW_INTERVAL_S:
            self._send(now)

    def flush(self) -> None:
        if self._dirty:
            self._send(time.monotonic())

    def _send(self, now: float) -> None:
        self._last_sent = now
        self._dirty = False
        publish_thread_activity(
            self.agent,
            self.thread_id,
            "streaming",
            channel=self.channel,
            text="".join(self._parts),
        )
