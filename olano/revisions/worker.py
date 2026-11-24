"""Debounced single-writer worker for home revision tracking.

One daemon thread per process owns all commits — two git processes must
never race on the bare repo (same one-at-a-time discipline as the
cartography refresh). Turn-end hooks call :func:`notify_turn`, which is a
cheap flag-set; the thread commits after the quiet period. ``commit_now``
(pre-upgrade snapshots, the API) runs synchronously through the same lock.

The worker only runs where it is started: the runtime server. Bare CLI
processes never spawn it, so ``notify_turn`` from those is a no-op and a
short-lived CLI exit can't be blocked by a commit in flight.

When the history is connected to GitHub (``revisions.remote``), the same
thread runs the automatic half of the sync: after a commit for
``sync: on_commit``, on a slow timer for ``sync: daily``. Pushes run outside
the debounce wait but under the same writer lock as commits
(:data:`olano.revisions.remote.WRITER_LOCK`), and a failed one is recorded
for the dashboard, never raised into the loop.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from olano.revisions.engine import SYSTEM_AUTHOR, RevisionsEngine, SnapshotResult
from olano.revisions.remote import WRITER_LOCK

logger = logging.getLogger(__name__)

# How often the thread wakes with nothing pending, to see whether a daily
# push (or a retry of a failed automatic one) has come due.
_REMOTE_TICK_SECONDS = 600.0


class RevisionsWorker:
    def __init__(self, engine: RevisionsEngine, *, debounce_seconds: float = 45.0) -> None:
        self._engine = engine
        self.debounce_seconds = max(5.0, float(debounce_seconds))
        self._cv = threading.Condition()
        self._deadline: float | None = None
        self._push_requested = False
        self._next_remote_tick = time.monotonic() + 60.0
        self._authors: set[str] = set()
        self._reasons: list[str] = []
        self._stop = False
        self._thread: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="revisions", daemon=True)
        self._thread.start()

    def stop(self, *, flush: bool = True) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if flush and self._deadline is not None:
            self._flush_pending()

    # ── producers ──────────────────────────────────────────────────────

    def notify(self, *, author: str = "", reason: str = "") -> None:
        """Coalescing change signal — cheap enough for every agent turn."""
        with self._cv:
            if author:
                self._authors.add(author)
            if reason and reason not in self._reasons:
                self._reasons.append(reason)
            self._deadline = time.monotonic() + self.debounce_seconds
            self._cv.notify_all()

    def commit_now(
        self, *, author: str = SYSTEM_AUTHOR, reason: str = "update", tag: str = ""
    ) -> SnapshotResult:
        """Synchronous snapshot through the writer lock (pre-upgrade, API)."""
        with WRITER_LOCK:
            result = self._engine.snapshot(author=author, reason=reason, tag=tag)
        if result.committed:
            self.request_push()
        return result

    def request_push(self) -> None:
        """New history exists: let the thread push it if the sync mode says so.

        Cheap and non-blocking; the network round trip happens on the thread.
        """
        with self._cv:
            self._push_requested = True
            self._cv.notify_all()

    # ── the thread ─────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass
        while True:
            with self._cv:
                while not self._stop and not self._due():
                    wake = self._next_remote_tick
                    if self._deadline is not None:
                        wake = min(wake, self._deadline)
                    self._cv.wait(timeout=max(0.1, wake - time.monotonic()))
                if self._stop:
                    return
                commit_due = self._deadline is not None and time.monotonic() >= self._deadline
                pushed_for = self._push_requested
                self._push_requested = False
            if commit_due:
                self._flush_pending()
            self._remote_tick(committed=pushed_for)

    def _due(self) -> bool:
        now = time.monotonic()
        return (
            self._push_requested
            or (self._deadline is not None and now >= self._deadline)
            or now >= self._next_remote_tick
        )

    def _remote_tick(self, *, committed: bool) -> None:
        from olano.revisions.remote import auto_sync

        with self._cv:
            self._next_remote_tick = time.monotonic() + _REMOTE_TICK_SECONDS
            # A commit landing during this tick re-requests a push itself.
            committed = committed or self._push_requested
            self._push_requested = False
        auto_sync(committed=committed)

    def _flush_pending(self) -> None:
        with self._cv:
            authors, reasons = self._authors, self._reasons
            self._authors, self._reasons, self._deadline = set(), [], None
        # One author keeps attribution; several collapse to the system
        # committer with the roster in the message.
        author = next(iter(authors)) if len(authors) == 1 else SYSTEM_AUTHOR
        reason = "; ".join(reasons) if reasons else "workspace update"
        if len(authors) > 1:
            reason += f" (agents: {', '.join(sorted(authors))})"
        try:
            result = self.commit_now(author=author, reason=reason)
            if result.committed:
                logger.info("revisions: committed %s (%s)", result.commit[:10], reason)
        except Exception:
            logger.warning("revisions: background commit failed", exc_info=True)


_WORKER: RevisionsWorker | None = None
_WORKER_LOCK = threading.Lock()


def get_revisions_worker(start: bool = False) -> RevisionsWorker | None:
    """The process singleton, or None when revisions is disabled/unavailable.

    ``start=True`` (runtime server boot) creates and starts it; ``False``
    returns the existing one without side effects.
    """
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None or not start:
            return _WORKER
        from olano.config.system_settings import load_revisions_settings
        from olano.revisions.engine import home_engine

        settings = load_revisions_settings()
        if not settings.enabled or not RevisionsEngine.available():
            return None
        _WORKER = RevisionsWorker(home_engine(), debounce_seconds=settings.debounce_seconds)
        _WORKER.start()
        return _WORKER


def reset_revisions_worker() -> None:
    """Test hook — drop the singleton (stops without flushing)."""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None:
            _WORKER.stop(flush=False)
        _WORKER = None


def notify_turn(agent_name: str) -> None:
    """Turn-end hook: never raises, no-op unless the server started the worker."""
    try:
        worker = get_revisions_worker(start=False)
        if worker is not None:
            worker.notify(author=agent_name, reason=f"turn: {agent_name}")
    except Exception:
        pass
