"""Tests for the channel health watchdog (``olano.channels._health``)."""

from __future__ import annotations

import asyncio

from olano.channels._health import HealthWatchdogMixin


class _FlakyChannel(HealthWatchdogMixin):
    """A channel whose probe AND rebuild always fail (e.g. a permanently
    bad token). Counts how many times a rebuild is attempted."""

    _HEALTH_CHECK_INTERVAL = 0.0
    _HEALTH_CHECK_TIMEOUT = 1.0
    _HEALTH_FAILURE_THRESHOLD = 2

    def __init__(self) -> None:
        self.rebuild_calls = 0

    async def _health_check(self) -> None:
        raise RuntimeError("updater is not running")

    async def _health_rebuild(self) -> None:
        self.rebuild_calls += 1
        raise RuntimeError("token rejected by the server")


async def test_watchdog_rearms_after_failed_rebuild(monkeypatch) -> None:
    """A persistently-failing rebuild must NOT fire on every single tick.

    Regression: previously ``consecutive`` was only reset to 0 on rebuild
    *success*, so once the threshold was hit a failing rebuild re-triggered
    every tick (the log filled with ``rebuild failed … (53/2)`` lines). After
    the fix the counter re-arms whether or not the rebuild raised, so a rebuild
    is attempted at most once per ``_HEALTH_FAILURE_THRESHOLD`` checks.
    """
    ch = _FlakyChannel()

    real_sleep = asyncio.sleep
    sleeps = {"n": 0}

    async def fake_sleep(delay, *args, **kwargs):
        # The watchdog's pacing sleep is the only asyncio.sleep on this path.
        # Allow 6 ticks, then break the loop the way a cancel would.
        sleeps["n"] += 1
        if sleeps["n"] > 6:
            raise asyncio.CancelledError()
        await real_sleep(0)  # yield control without recursing into fake_sleep

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await ch._health_watchdog_loop()

    # 6 failing checks, threshold 2 → rebuild attempted once per 2 checks = 3.
    # The buggy version rebuilt on ticks 2..6 = 5 times.
    assert ch.rebuild_calls == 3


async def test_watchdog_recovers_and_resets(monkeypatch) -> None:
    """When the probe starts passing again the counter clears and no rebuild
    is attempted — the happy path stays intact."""

    class _Recovering(HealthWatchdogMixin):
        _HEALTH_CHECK_INTERVAL = 0.0
        _HEALTH_CHECK_TIMEOUT = 1.0
        _HEALTH_FAILURE_THRESHOLD = 2

        def __init__(self) -> None:
            self.checks = 0
            self.rebuild_calls = 0

        async def _health_check(self) -> None:
            self.checks += 1
            if self.checks == 1:  # one transient failure, then healthy
                raise RuntimeError("blip")

        async def _health_rebuild(self) -> None:
            self.rebuild_calls += 1

    ch = _Recovering()
    real_sleep = asyncio.sleep
    sleeps = {"n": 0}

    async def fake_sleep(delay, *args, **kwargs):
        sleeps["n"] += 1
        if sleeps["n"] > 4:
            raise asyncio.CancelledError()
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await ch._health_watchdog_loop()

    # Single failure never reached the threshold → no rebuild.
    assert ch.rebuild_calls == 0
