"""Cross-cutting utilities reused by platform adapters.

These are pure-Python helpers lifted from ``olano/channels/adapter.py``
so new platform code does not have to reach into the legacy package.
Both locations are kept in sync until the channels tree is retired.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class MessageDebouncer:
    """Coalesce rapid messages from the same chat into a single batch.

    When multiple messages from the same ``chat_id`` arrive within
    ``window_seconds``, they are bundled and dispatched as one call to
    ``handler`` — so the agent sees a single multi-part thought rather
    than five independent requests.
    """

    def __init__(self, window_seconds: float = 1.5) -> None:
        self.window = window_seconds
        self._pending: dict[str, list[str]] = defaultdict(list)
        self._timers: dict[str, asyncio.Task] = {}

    async def submit(
        self,
        chat_id: str,
        message: str,
        handler: Callable[[str, list[str]], Coroutine],
    ) -> None:
        self._pending[chat_id].append(message)

        if chat_id in self._timers:
            self._timers[chat_id].cancel()

        async def _fire() -> None:
            await asyncio.sleep(self.window)
            batch = self._pending.pop(chat_id, [])
            self._timers.pop(chat_id, None)
            if batch:
                await handler(chat_id, batch)

        self._timers[chat_id] = asyncio.create_task(_fire())

    async def flush(self, chat_id: str | None = None) -> None:
        keys = [chat_id] if chat_id else list(self._timers.keys())
        for key in keys:
            task = self._timers.pop(key, None)
            if task:
                task.cancel()
            batch = self._pending.pop(key, [])
            logger.debug("Debouncer: flushed %d messages for chat %s", len(batch), key)


class MessageSequentializer:
    """Guarantee FIFO processing of messages from the same chat."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._queue_sizes: dict[str, int] = defaultdict(int)

    def acquire(self, chat_id: str) -> _SequentializeCtx:
        return _SequentializeCtx(self._locks[chat_id], self._queue_sizes, chat_id)

    def queue_size(self, chat_id: str) -> int:
        return self._queue_sizes.get(chat_id, 0)


class _SequentializeCtx:
    def __init__(self, lock: asyncio.Lock, sizes: dict[str, int], chat_id: str) -> None:
        self._lock = lock
        self._sizes = sizes
        self._chat_id = chat_id

    async def __aenter__(self) -> None:
        self._sizes[self._chat_id] = self._sizes.get(self._chat_id, 0) + 1
        await self._lock.acquire()

    async def __aexit__(self, *_: Any) -> None:
        self._lock.release()
        self._sizes[self._chat_id] = max(0, self._sizes.get(self._chat_id, 0) - 1)
