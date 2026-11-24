"""The one event loop that owns every Playwright object in the process.

Playwright's async objects are bound to the loop they were created on, and the
callers are everywhere: synchronous tool bodies on LangGraph's executor
threads, the dashboard's live-view WebSocket on uvicorn's loop, the idle
sweeper. Each of them submits work here with :func:`submit` /
:func:`run_sync`, so the browser, its contexts, the DevTools relay and the
screencast sessions all live on one long-lived loop thread. (Running each
tool call under its own ``asyncio.run()`` closed the loop between calls and
left the session attached to a dead loop: every second browser call hung.)

``run_coroutine_threadsafe`` copies the caller's contextvars into the task, so
code running here still sees the calling tool's request context, but the
convention is to capture what a coroutine needs in the sync caller and pass it
down rather than depend on that.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()
_generation = 0


def session_loop() -> asyncio.AbstractEventLoop:
    """The persistent loop, started on first use and restarted if it died."""
    global _loop, _thread, _generation
    with _lock:
        if _loop is None or _loop.is_closed() or _thread is None or not _thread.is_alive():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="olano-browser-loop", daemon=True
            )
            thread.start()
            _loop, _thread = loop, thread
            _generation += 1
        return _loop


def generation() -> int:
    """Bumped every time a new loop starts: loop-bound locks key on it."""
    session_loop()
    return _generation


def on_loop() -> bool:
    try:
        return asyncio.get_running_loop() is _loop
    except RuntimeError:
        return False


def submit(coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
    return asyncio.run_coroutine_threadsafe(coro, session_loop())


def run_sync(coro: Coroutine[Any, Any, T], timeout_s: float = 90) -> T:
    """Run *coro* on the session loop and wait. Raises ``TimeoutError`` on timeout."""
    future = submit(coro)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"browser operation timed out after {int(timeout_s)}s") from exc


async def run_async(coro: Coroutine[Any, Any, T], timeout_s: float = 90) -> T:
    """Await *coro* on the session loop from another loop (uvicorn's)."""
    future = submit(coro)
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout_s)
