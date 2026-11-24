"""Mid-turn steering — hand a running agent turn a message at its next step.

A message typed while the agent is still working used to have two fates in the
dashboard: wait in a client-side queue until the reply ends and then run as a
brand-new turn, or abort the running turn and resend. Both throw work away —
the agent finishes (and bills) a version the user has already asked it to
change, or loses the tool chain it was in the middle of. **Steering** is the
third option, the one Claude Code, Codex (``turn/steer``), the OpenAI Responses
API (``response.steer``) and OpenClaw converged on: deliver the message *inside
the current turn*, at the next model call — after the tool calls already in
flight finish, before the model is asked what to do next — without cancelling
anything. The model reads it as more user context and adjusts course.

This module is the registry half: a process-wide map of the turns that are
running right now, keyed by ``(agent, thread_id)`` (the *unscoped* dashboard
thread id — the one the request context carries, not the checkpointer's
agent-scoped key), each holding the steers accepted for it. The graph half is
:mod:`olano.core.steer_middleware`, whose ``before_model`` hook drains the
pending steers into the LangGraph state right before each model call; the
runner (``AgentRunner.astream_events``) opens and closes the turn and reports
delivered steers as ``("steer", …)`` events + transcript rows.

The registry is deliberately free of langchain imports so the API layer and the
runner can consult it without touching the graph stack.

Lifecycle of one steer, in the order the pieces see it::

    POST /api/chat/steer ──► runner.steer() ──► REGISTRY.steer()   (pending)
    … tools finish …
    SteerMiddleware.before_model ──► REGISTRY.drain()             (delivered)
    runner stream loop ──► REGISTRY.pop_delivered()  ──► ("steer", …) frame
                                                     └─► JSONL user row {steer: true}
    turn ends ──► REGISTRY.end_turn()  (undelivered steers are dropped here;
                                        the dashboard re-sends them as the
                                        next turn — it never lost them)

Codex's ``expectedTurnId`` rule applies: a steer names the turn it was typed
against, and a turn that already ended (or a different turn on the same
thread) refuses it, so the client can fall back to queueing instead of
handing an aside to a conversation that has moved on.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# One steer is a short aside, not a document. The dashboard composer holds far
# more than this, but a mid-turn note that long belongs in the next turn.
MAX_STEER_CHARS = 8000
# Pending + delivered steers a single turn will accept. Past this the client is
# told to queue; ten mid-turn corrections is already a turn worth restarting.
MAX_PER_TURN = 10

# Prompt-only framing prepended to the steer text in the model's context (the
# transcript and the UI keep the raw text). Same family as the runner's
# context-note header: one line that tells the model *when* this arrived and
# what to do with it, because a bare "make the bars blue" between two tool
# results reads ambiguously — a new request, or a change to the one in flight?
STEER_HEADER = (
    "[The user sent this while you were working. Take it into account for the "
    "task in progress and continue; do not start over unless asked.]"
)


def steer_message_content(text: str) -> str:
    """The exact string the model sees for one steer."""
    return f"{STEER_HEADER}\n\n{text}"


@dataclass
class SteerMessage:
    """One accepted steer. ``delivered_at`` is set when the model read it."""

    id: str
    text: str
    turn_id: str
    sender: str = ""
    channel: str = ""
    created_at: float = field(default_factory=time.time)
    delivered_at: float | None = None

    @property
    def delivered(self) -> bool:
        return self.delivered_at is not None


class TurnHandle:
    """The registry's record of one running turn.

    ``has_delivered`` is a plain attribute the runner polls once per streamed
    chunk (``pop_delivered``) without taking the lock — it flips under the lock
    and is only ever read as a hint, so a stale ``False`` costs one extra chunk
    of latency and a stale ``True`` costs one lock acquire.
    """

    __slots__ = ("agent", "delivered", "has_delivered", "pending", "thread_id", "turn_id")

    def __init__(self, agent: str, thread_id: str, turn_id: str) -> None:
        self.agent = agent
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.pending: list[SteerMessage] = []
        self.delivered: list[SteerMessage] = []
        self.has_delivered = False


class SteerRegistry:
    """Process-wide map of running turns and the steers accepted for them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[tuple[str, str], TurnHandle] = {}

    # ── turn lifecycle (runner) ──────────────────────────────────────────
    def begin_turn(self, agent: str, thread_id: str, turn_id: str | None = None) -> TurnHandle:
        """Register a turn as running. A newer turn on the same key supersedes
        an older one (its handle's ``end_turn`` then becomes a no-op)."""
        handle = TurnHandle(agent, thread_id, turn_id or uuid.uuid4().hex)
        with self._lock:
            self._turns[(agent, thread_id)] = handle
        return handle

    def end_turn(self, handle: TurnHandle) -> list[SteerMessage]:
        """Close a turn and return the steers it never delivered.

        Clears the key only when this handle is still the registered one, so a
        superseded turn finishing late cannot evict its successor.
        """
        key = (handle.agent, handle.thread_id)
        with self._lock:
            if self._turns.get(key) is handle:
                del self._turns[key]
            leftovers = list(handle.pending)
            handle.pending = []
        return leftovers

    def active_turn(self, agent: str, thread_id: str) -> str | None:
        with self._lock:
            handle = self._turns.get((agent, thread_id))
        return handle.turn_id if handle else None

    # ── steers (API) ─────────────────────────────────────────────────────
    def steer(
        self,
        agent: str,
        thread_id: str,
        text: str,
        *,
        expected_turn_id: str | None = None,
        steer_id: str | None = None,
        sender: str = "",
        channel: str = "",
    ) -> SteerMessage | None:
        """Accept a steer for the turn running on ``(agent, thread_id)``.

        Returns ``None`` when no turn is running there, or when
        ``expected_turn_id`` names a different turn — the caller falls back to
        queueing. Raises ``ValueError`` for an empty/oversized text or a turn
        that already holds ``MAX_PER_TURN`` steers.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("steer text is empty")
        if len(text) > MAX_STEER_CHARS:
            raise ValueError(f"steer text exceeds {MAX_STEER_CHARS} characters")
        with self._lock:
            handle = self._turns.get((agent, thread_id))
            if handle is None:
                return None
            if expected_turn_id and expected_turn_id != handle.turn_id:
                return None
            if len(handle.pending) + len(handle.delivered) >= MAX_PER_TURN:
                raise ValueError(f"this turn already holds {MAX_PER_TURN} steers")
            msg = SteerMessage(
                id=steer_id or uuid.uuid4().hex,
                text=text,
                turn_id=handle.turn_id,
                sender=sender,
                channel=channel,
            )
            handle.pending.append(msg)
        return msg

    def cancel(self, agent: str, thread_id: str, steer_id: str) -> str:
        """Withdraw a steer the model has not read yet.

        ``"cancelled"`` when it was still pending, ``"delivered"`` when the
        model already read it (nothing to withdraw — the transcript shows it),
        ``"unknown"`` when no running turn holds that id (the turn ended, or
        the id was never accepted).
        """
        with self._lock:
            handle = self._turns.get((agent, thread_id))
            if handle is None:
                return "unknown"
            for i, msg in enumerate(handle.pending):
                if msg.id == steer_id:
                    del handle.pending[i]
                    return "cancelled"
            if any(m.id == steer_id for m in handle.delivered):
                return "delivered"
        return "unknown"

    # ── delivery (middleware + runner) ───────────────────────────────────
    def drain(self, agent: str, thread_id: str) -> list[SteerMessage]:
        """Move every pending steer to delivered and return them, in order.

        Called by the graph hook right before a model call. Atomic, so a node
        that runs twice (a LangGraph retry) finds nothing the second time.
        """
        with self._lock:
            handle = self._turns.get((agent, thread_id))
            if handle is None or not handle.pending:
                return []
            drained = handle.pending
            handle.pending = []
            now = time.time()
            for msg in drained:
                msg.delivered_at = now
            handle.delivered.extend(drained)
            handle.has_delivered = True
        return list(drained)

    def pop_delivered(self, handle: TurnHandle) -> list[SteerMessage]:
        """Steers delivered since the last call — the runner's hand-off for the
        ``("steer", …)`` event and the transcript row. Lock-free when nothing
        was delivered (the common case, once per streamed chunk)."""
        if not handle.has_delivered:
            return []
        with self._lock:
            out = list(handle.delivered)
            handle.delivered = []
            handle.has_delivered = False
        return out

    # ── introspection / tests ────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                f"{agent}:{thread}": {
                    "turn_id": h.turn_id,
                    "pending": len(h.pending),
                    "delivered": len(h.delivered),
                }
                for (agent, thread), h in self._turns.items()
            }

    def reset(self) -> None:
        with self._lock:
            self._turns.clear()


REGISTRY = SteerRegistry()
