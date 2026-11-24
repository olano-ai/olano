"""In-memory rate limiting helpers."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


class InMemoryRateLimiter:
    """Simple sliding-window rate limiter."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(
        self,
        key: str,
        *,
        max_calls: int,
        window_seconds: int = 60,
    ) -> RateLimitDecision:
        if max_calls <= 0:
            return RateLimitDecision(allowed=False, retry_after=window_seconds)

        now = time.time()
        floor = now - window_seconds
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= floor:
                bucket.popleft()
            if len(bucket) >= max_calls:
                retry_after = max(1, int(window_seconds - (now - bucket[0])))
                return RateLimitDecision(allowed=False, retry_after=retry_after)
            bucket.append(now)
            return RateLimitDecision(allowed=True)

    def check_windows(
        self,
        key: str,
        limits: list[tuple[int, int]],
        *,
        record: bool = True,
    ) -> RateLimitDecision:
        """Evaluate several ``(max_calls, window_seconds)`` caps against one key.

        All windows share a single timestamp deque so the caps stay mutually
        consistent (e.g. minute + hour + day): if *any* window is at/over its
        cap the call is rejected and *no* timestamp is recorded, so a request
        that trips the hour cap does not inflate the minute count. On success a
        single timestamp is appended, counting once across every window.

        ``record=False`` performs a pure check (a "peek") without recording the
        event — used to evaluate two independent scopes (agent + platform) and
        only commit both once both pass, so a message rejected by one scope
        never inflates the other's counters.

        ``max_calls <= 0`` for a window means "block everything" for that window.
        Empty ``limits`` means "unlimited" → always allowed.
        """
        active = [(mc, ws) for mc, ws in limits if ws > 0]
        if not active:
            return RateLimitDecision(allowed=True)

        now = time.time()
        max_window = max(ws for _, ws in active)
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= now - max_window:
                bucket.popleft()
            for max_calls, window_seconds in active:
                if max_calls <= 0:
                    return RateLimitDecision(allowed=False, retry_after=window_seconds)
                floor = now - window_seconds
                count = sum(1 for ts in bucket if ts > floor)
                if count >= max_calls:
                    # Oldest event still inside this window bounds the wait.
                    oldest_in_window = next(ts for ts in bucket if ts > floor)
                    retry_after = max(1, int(window_seconds - (now - oldest_in_window)))
                    return RateLimitDecision(allowed=False, retry_after=retry_after)
            if record:
                bucket.append(now)
            return RateLimitDecision(allowed=True)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


RUNTIME_RATE_LIMITER = InMemoryRateLimiter()
TOOL_RATE_LIMITER = InMemoryRateLimiter()
