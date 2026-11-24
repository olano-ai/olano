"""Agent runs that outlive the browser that started them.

A dashboard turn used to *be* its HTTP request. ``POST /api/chat/stream`` ran
``AgentRunner.astream_events`` inside the SSE response generator, so the turn's
lifetime was the socket's lifetime: a phone that locked, an app switch, a
network moving 5G→4G — anything that dropped the connection closed the async
generator, which ``aclose()``d the stream and cancelled the graph mid-tool-call.
The user came back to "Connection lost while … was replying" and a plan frozen
at step 2 of 6, and there was nothing on the server left to rejoin: no run
object, no frame buffer, no cursor.

This module is the run object. A turn is started as a detached
:class:`asyncio.Task` that **emits** frames into an :class:`AgentRun` instead of
yielding them at a socket; the HTTP response becomes a *view* of that run. Zero,
one or several viewers may come and go — from the tab that started it, from a
tab opened afterwards, from another device — and the run neither notices nor
cares. Nothing here is a second agent loop: the coroutine a run holds is exactly
the turn the route used to run inline.

Two properties make re-attaching honest rather than approximate:

* **Every frame is numbered.** A viewer attaches with the last ``seq`` it saw
  and gets precisely what it missed, then tails live — tool calls, tool results,
  usage and approvals included. The throttled ``thread_activity`` preview
  (:mod:`olano.core.thread_events`) is text-only and lossy by design; it tells
  *other* surfaces that a thread is busy, and it is not a recovery mechanism.
* **Replay and tail are one critical section.** :meth:`AgentRun.attach`
  snapshots the buffer and registers the viewer's queue under the same lock, so
  no frame can be emitted into the gap between "here is what you missed" and
  "you are now subscribed".

The buffer is bounded in both directions that can run away — frame count and
bytes — because a long tool-heavy turn is exactly the kind that gets abandoned.
On overflow the oldest frames are dropped and ``dropped_before`` advances; a
viewer whose cursor has fallen behind that mark is told to ``resync`` (refetch
the transcript) rather than handed a hole with no seam.

Deliberately in-process, for the same reason as
:class:`olano.groups.cascade.CascadeRegistry`: a run *is* coroutines on this
runtime's event loop, so anything that outlived the process would describe work
nobody can still stop. A restart ends every run, and the transcript rows the
runner already persisted are what survives.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

#: Frames one run will hold for replay. A turn that emits more than this has
#: almost certainly streamed more text than a returning reader will scroll.
MAX_FRAMES = 2000

#: …and the same bound by weight, because a handful of large tool results can
#: cost more memory than two thousand text chunks. Whichever trips first.
MAX_BYTES = 4 * 1024 * 1024

#: How long a finished run stays attachable. A phone that comes back a minute
#: after the answer landed should still receive the tail and the terminal frame
#: rather than a 404 that reads as "your turn vanished".
RETENTION_S = 300.0

#: Frames a single viewer may fall behind before it is dropped. A viewer that
#: cannot keep up with this is not reading; the run must never block on it.
VIEWER_QUEUE_MAX = 512

#: Sentinel pushed to every viewer queue when a run ends, so a tailing view
#: stops waiting instead of sitting on the queue until its own socket dies.
RUN_END = object()


class AgentRun:
    """One detached turn: its frames, its viewers, and the task running it.

    ``thread_id`` is always the **namespaced** id (``auth.namespace_thread_id``)
    — the same value the transcript and the steer registry use. Storing the raw
    id would make the attach endpoint's visibility check meaningless.
    """

    __slots__ = (
        "_lock",
        "agent",
        "bytes_held",
        "done",
        "dropped_before",
        "ended_at",
        "frames",
        "kind",
        "run_id",
        "seq",
        "started_at",
        "stopped",
        "task",
        "thread_id",
        "turn_id",
        "viewers",
    )

    def __init__(self, kind: str, agent: str, thread_id: str, turn_id: str) -> None:
        self.run_id = uuid.uuid4().hex
        self.kind = kind
        self.agent = agent
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.started_at = time.time()
        self.ended_at = 0.0
        self.done = False
        self.stopped = False
        self.task: Any | None = None
        self.seq = 0
        self.frames: deque[tuple[int, dict]] = deque()
        self.dropped_before = 0
        self.bytes_held = 0
        self.viewers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()

    # ── producing ────────────────────────────────────────────────────────
    def emit(self, frame: dict) -> int:
        """Number ``frame``, buffer it, and hand it to every live viewer.

        Never blocks and never raises: a viewer that has fallen
        ``VIEWER_QUEUE_MAX`` frames behind is dropped rather than allowed to
        stall the agent. Its client reconnects and replays from its cursor,
        which is the whole point of the cursor.
        """
        with self._lock:
            self.seq += 1
            seq = self.seq
            stamped = {**frame, "seq": seq}
            self.frames.append((seq, stamped))
            self.bytes_held += _frame_weight(stamped)
            self._trim_locked()
            viewers = list(self.viewers)
        for q in viewers:
            try:
                q.put_nowait(stamped)
            except asyncio.QueueFull:
                logger.debug("run %s: dropping a viewer that fell behind", self.run_id)
                self.detach(q)
        return seq

    def _trim_locked(self) -> None:
        while self.frames and (len(self.frames) > MAX_FRAMES or self.bytes_held > MAX_BYTES):
            seq, frame = self.frames.popleft()
            self.bytes_held -= _frame_weight(frame)
            self.dropped_before = seq

    def finish(self) -> None:
        """Mark the run complete and release every tailing view."""
        with self._lock:
            if self.done:
                return
            self.done = True
            self.ended_at = time.time()
            viewers = list(self.viewers)
        for q in viewers:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(RUN_END)

    def stop(self) -> bool:
        """Cancel the run. Returns whether there was anything to cancel.

        Idempotent: a run already stopping (or finished) answers ``False``, so a
        second Stop — two devices watching the same turn, or an impatient
        double-click — reports honestly instead of claiming it stopped a turn
        that was already on its way out.

        Two mechanisms, both needed and both borrowed from
        :meth:`olano.groups.cascade.CascadeRegistry.stop`: the flag lets a
        cooperative loop notice between steps, and the cancel interrupts a task
        already inside a model call — a human who presses Stop must not wait out
        a turn that may take minutes.
        """
        if self.done or self.stopped:
            return False
        self.stopped = True
        task = self.task
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:  # pragma: no cover - defensive
                logger.debug("run %s: cancel failed", self.run_id, exc_info=True)
        return True

    # ── viewing ──────────────────────────────────────────────────────────
    def attach(self, cursor: int = 0) -> tuple[list[dict], asyncio.Queue | None]:
        """Frames after ``cursor``, plus a queue tailing everything later.

        The snapshot and the registration happen under one lock so a frame
        emitted concurrently lands in exactly one of them, never neither.

        A ``cursor`` older than what the buffer still holds cannot be served
        honestly, so it gets a single ``resync`` frame: the client refetches the
        thread instead of rendering a gap it cannot see. The queue is ``None``
        for a run that has already finished — there is nothing left to tail.
        """
        with self._lock:
            if cursor < self.dropped_before:
                backlog = [{"type": "resync", "run_id": self.run_id, "seq": self.seq}]
            else:
                backlog = [f for seq, f in self.frames if seq > cursor]
            if self.done:
                return backlog, None
            q: asyncio.Queue = asyncio.Queue(maxsize=VIEWER_QUEUE_MAX)
            self.viewers.add(q)
            return backlog, q

    def detach(self, q: asyncio.Queue) -> None:
        with self._lock:
            self.viewers.discard(q)

    def is_viewing(self, q: asyncio.Queue) -> bool:
        """Whether ``q`` still receives frames (``emit`` drops one that fell behind)."""
        with self._lock:
            return q in self.viewers

    @property
    def viewer_count(self) -> int:
        with self._lock:
            return len(self.viewers)

    def snapshot(self) -> dict[str, Any]:
        """What a thread payload advertises so a fresh page can auto-attach."""
        with self._lock:
            return {
                "run_id": self.run_id,
                "kind": self.kind,
                "agent": self.agent,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "cursor": self.seq,
                "started_at": self.started_at,
                # The same truth `RunRegistry._live` uses: a task that died
                # without reaching finish() is not running, whatever `done` says.
                "running": not self.done and not (self.task is not None and self.task.done()),
                "viewers": len(self.viewers),
            }


def _frame_weight(frame: dict) -> int:
    """Cheap size estimate — the text fields are what actually grow."""
    total = 64
    for value in frame.values():
        if isinstance(value, str):
            total += len(value)
        elif isinstance(value, list | dict):
            total += 256
    return total


class RunRegistry:
    """The runs alive in this process, addressable by id and by thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, AgentRun] = {}
        self._by_key: dict[tuple[str, str, str], AgentRun] = {}

    @staticmethod
    def _key(kind: str, agent: str, thread_id: str) -> tuple[str, str, str]:
        return (kind, agent, thread_id)

    @staticmethod
    def _live(run: AgentRun | None) -> bool:
        """Whether ``run`` is still working.

        Not simply ``not run.done``: a task that died before its ``finally``
        could reach :meth:`finish` would otherwise hold its thread key forever,
        and every later turn on that conversation would 409 against a run that
        no longer exists. The task's own state is the ground truth, exactly as
        in :meth:`olano.groups.cascade.CascadeRegistry._finished`.
        """
        if run is None or run.done:
            return False
        task = run.task
        return not (task is not None and task.done())

    def begin(self, kind: str, agent: str, thread_id: str, turn_id: str) -> AgentRun | None:
        """Claim the thread for a new run.

        ``None`` when one is already running there. A thread holds a single
        ordered conversation, so a second concurrent run would braid two turns
        into one transcript — the caller answers 409 and the client steers into
        the live run instead (:mod:`olano.core.steering`).
        """
        key = self._key(kind, agent, thread_id)
        with self._lock:
            self._sweep_locked()
            if self._live(self._by_key.get(key)):
                return None
            run = AgentRun(kind, agent, thread_id, turn_id)
            self._by_key[key] = run
            self._by_id[run.run_id] = run
            return run

    def get(self, run_id: str) -> AgentRun | None:
        with self._lock:
            self._sweep_locked()
            return self._by_id.get(run_id)

    def for_thread(self, kind: str, agent: str, thread_id: str) -> AgentRun | None:
        """The run **currently working** on this thread, if any.

        Live-only: this is what decides "is this thread busy" for the thread
        payload's ``run`` field and for the one-run-per-thread rule in
        :meth:`begin`. Attaching asks :meth:`latest_for_thread` instead.
        """
        with self._lock:
            self._sweep_locked()
            run = self._by_key.get(self._key(kind, agent, thread_id))
            return run if self._live(run) else None

    def latest_for_thread(self, kind: str, agent: str, thread_id: str) -> AgentRun | None:
        """The most recent run on this thread, finished ones included.

        A client that knows only "this thread was busy" — a page that just
        reloaded — attaches by thread. The turn may well have completed in the
        moment between reading the thread and asking to attach, and answering
        404 there would drop the ending it was reconnecting for.
        """
        with self._lock:
            self._sweep_locked()
            return self._by_key.get(self._key(kind, agent, thread_id))

    def finish(self, run: AgentRun) -> None:
        """Close a run.

        The thread key is deliberately **kept**: a run that ended a moment ago
        is still attachable (see :meth:`latest_for_thread`), and dropping the
        mapping here would lose the ending to a race — a page that read
        ``run`` from the thread payload and attached a beat later would get a
        404 for a turn that had just finished successfully. The sweeper evicts
        it once :data:`RETENTION_S` has passed, and :meth:`begin` replaces a
        finished run at the same key immediately.
        """
        run.finish()
        with self._lock:
            self._sweep_locked()

    def stop(self, run_id: str) -> bool:
        run = self.get(run_id)
        return run.stop() if run is not None else False

    def stop_thread(self, kind: str, agent: str, thread_id: str) -> bool:
        run = self.for_thread(kind, agent, thread_id)
        return run.stop() if run is not None else False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {rid: run.snapshot() for rid, run in self._by_id.items()}

    def reset(self) -> None:
        """Drop every entry (tests; a runtime restart)."""
        with self._lock:
            self._by_id.clear()
            self._by_key.clear()

    def _sweep_locked(self) -> None:
        """Evict finished runs past :data:`RETENTION_S`.

        Called from every accessor rather than on a timer: the registry is only
        interesting when someone is asking about it, and a sweep that needs its
        own task is a task to remember to cancel.
        """
        if not self._by_id:
            return
        cutoff = time.time() - RETENTION_S
        stale = [
            rid
            for rid, run in self._by_id.items()
            if run.done and run.ended_at and run.ended_at < cutoff
        ]
        # …and any run whose task is gone but which never marked itself done:
        # nothing will ever finish it, so retention cannot start counting.
        stale += [
            rid
            for rid, run in self._by_id.items()
            if rid not in stale and not run.done and run.task is not None and run.task.done()
        ]
        for rid in stale:
            run = self._by_id.pop(rid, None)
            if run is None:
                continue
            key = self._key(run.kind, run.agent, run.thread_id)
            if self._by_key.get(key) is run:
                del self._by_key[key]


#: Process-wide registry — the runtime is one process and a run is bound to its
#: event loop, exactly like :data:`olano.groups.cascade.REGISTRY`.
REGISTRY = RunRegistry()


async def sse_run_view(run: AgentRun, cursor: int, request: Any) -> AsyncIterator[str]:
    """Stream one :class:`~olano.core.runs.AgentRun` to one HTTP client.

    The turn runs somewhere else (a detached task); this is only a window onto
    it. Replay whatever the viewer missed after ``cursor``, then tail live until
    the run ends — and when the client goes away, drop the viewer and return.
    **Nothing here cancels the run**, which is the entire point: the browser
    disconnecting is now a viewer leaving, not a turn dying.

    The same generator serves the POST that starts a turn (``cursor=0``) and
    every later ``GET /api/chat/attach``, so a reconnecting phone and a fresh
    tab are the same code path with a different cursor.
    """
    backlog, queue = run.attach(cursor)
    try:
        for frame in backlog:
            yield "data: " + json.dumps(frame, default=str) + "\n\n"
        if queue is None:
            return  # the run had already finished; the backlog was all of it
        while True:
            # A drained queue that nothing will ever feed again must end the
            # response. `emit` drops a viewer whose queue filled (a phone
            # suspended mid-stream stops reading, so TCP backpressure fills
            # it), and `finish` cannot hand RUN_END to a queue that is full.
            # Both left this loop yielding keepalives forever: the client's
            # stream never ended, so it never reattached from its cursor and
            # every refresh waiting on "no stream open" waited for good.
            # Ending without `done` is the signal the client already reads as
            # "the connection dropped; rejoin from the cursor".
            if queue.empty() and (run.done or not run.is_viewing(queue)):
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15.0)
            except TimeoutError:
                # Keep the byte stream warm so an idle-timeout proxy in front
                # does not cut a view that is legitimately waiting on a slow
                # tool. Same cadence as /api/stream.
                if await request.is_disconnected():
                    return
                yield ": keepalive\n\n"
                continue
            if item is RUN_END:
                return
            yield "data: " + json.dumps(item, default=str) + "\n\n"
    finally:
        if queue is not None:
            run.detach(queue)
