"""One lock per checkpoint thread, shared by every writer in the process.

The checkpoint sweep's compaction (``core/checkpoint_gc.compact_namespace``)
rebuilds a channel's value from stored writes and appends a checkpoint with
that value on the thread's tip. Nothing in the saver serializes that
read-rebuild-publish sequence against a turn on the same thread: a turn that
resumed the thread between the rebuild and the publish wrote its own
checkpoint, and the compactor's row, carrying the pre-turn channel versions
under a newer id, became the tip every later load read. The turn was gone
from the resumable state (reproduced on a real Postgres by the review of
#682). A quiet-age check and a pending-write check made before the rebuild
only narrow that window; they cannot close it.

So writers and the compactor share a lock, keyed on the exact ``thread_id``
the checkpoint rows carry (the agent-scoped id the runner invokes with, the
``cog::`` id a cognition worker invokes with). A turn holds it for the whole
run (`runner.astream_events`, `cognition.worker.run_worker`), the compactor
takes it without waiting, re-reads the tip under it, publishes, and lets go
within milliseconds, and a turn that arrives meanwhile waits those
milliseconds. The compactor never waits for a turn: a held lock means skip
this namespace this pass.

In-process only, which is the scope that matters: the sweep runs inside the
runtime, and every turn the runtime executes goes through the two sites
above. A second process writing the same thread (a one-shot CLI run against
a running server's database) is outside it, which is why the compactor still
keeps its quiet window and its tip re-check.

Not an ``asyncio.Lock``: that cannot be tried without waiting, and the
registry must be safe to touch from a scheduler fallback loop on another
thread, so entries hand off through futures resolved on the waiter's loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _Entry:
    held: bool = False
    waiters: deque[asyncio.Future[None]] = field(default_factory=deque)


_REGISTRY: dict[str, _Entry] = {}
_GUARD = threading.Lock()


class Hold:
    """A held thread lock; ``release()`` exactly once, or use as a context."""

    __slots__ = ("_released", "thread_id")

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _release(self.thread_id)

    async def __aenter__(self) -> Hold:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.release()


def try_acquire(thread_id: str) -> Hold | None:
    """Take the lock now, or ``None`` when a writer holds it. Never waits."""
    with _GUARD:
        entry = _REGISTRY.get(thread_id)
        if entry is None:
            entry = _Entry()
            _REGISTRY[thread_id] = entry
        if entry.held:
            return None
        entry.held = True
    return Hold(thread_id)


async def acquire(thread_id: str) -> Hold:
    """Take the lock, waiting for the current holder to release it."""
    loop = asyncio.get_running_loop()
    with _GUARD:
        entry = _REGISTRY.get(thread_id)
        if entry is None:
            entry = _Entry()
            _REGISTRY[thread_id] = entry
        if not entry.held:
            entry.held = True
            return Hold(thread_id)
        fut: asyncio.Future[None] = loop.create_future()
        entry.waiters.append(fut)
    try:
        await fut
    except asyncio.CancelledError:
        # Cancelled while waiting: give the hand-off back if it already
        # landed on us, otherwise just leave the queue.
        with _GUARD:
            if fut.done() and not fut.cancelled():
                _release_locked(thread_id)
            else:
                try:
                    entry.waiters.remove(fut)
                except ValueError:
                    pass
                _drop_if_idle(thread_id)
        raise
    return Hold(thread_id)


def is_held(thread_id: str) -> bool:
    with _GUARD:
        entry = _REGISTRY.get(thread_id)
        return bool(entry and entry.held)


def _release(thread_id: str) -> None:
    with _GUARD:
        _release_locked(thread_id)


def _release_locked(thread_id: str) -> None:
    entry = _REGISTRY.get(thread_id)
    if entry is None:
        return
    while entry.waiters:
        fut = entry.waiters.popleft()
        if fut.cancelled():
            continue
        # Hand the lock straight to the next waiter: it stays held, so a
        # try_acquire slipping in between cannot steal it.
        entry.held = True
        fut.get_loop().call_soon_threadsafe(_resolve, fut)
        return
    entry.held = False
    _drop_if_idle(thread_id)


def _resolve(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


def _drop_if_idle(thread_id: str) -> None:
    entry = _REGISTRY.get(thread_id)
    if entry is not None and not entry.held and not entry.waiters:
        del _REGISTRY[thread_id]


def _reset_for_tests() -> None:
    with _GUARD:
        _REGISTRY.clear()
