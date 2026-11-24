"""Reusable network-health watchdog for long-lived channel adapters.

Channels that maintain a persistent upstream connection (Telegram polling,
Discord gateway websocket, Slack Socket Mode) tend to wedge themselves
when the underlying network state changes — Wi-Fi flap, VPN reconnect,
DHCP renewal — because the cached connection pool / DNS resolver inside
the SDK's HTTP client doesn't always recover by itself. The classic
symptom is the same ``[Errno 8] nodename nor servname provided`` looping
forever in the daemon log even after DNS is back.

The mixin in this module periodically probes the upstream with a cheap
call (``get_me``, ``auth_test``, ws ready check…). After
``_HEALTH_FAILURE_THRESHOLD`` consecutive failures it asks the adapter to
rebuild its underlying client — that drops the stale pool, triggers
fresh DNS lookups, and recovers polling without a daemon restart.

Adapters wire it in three steps:

    class FooChannel(HealthWatchdogMixin, BaseChannel):
        async def _health_check(self) -> None:
            await self._app.foo.ping()

        async def _health_rebuild(self) -> None:
            await self._app.shutdown()
            self._app = None
            await self.start()      # rebuilds with a fresh HTTP client

        async def start(self) -> None:
            ... existing code ...
            self._start_health_watchdog()
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class HealthWatchdogMixin:
    # Tunables — override on the subclass when a particular service needs
    # a different cadence (Slack's auth_test for instance is rate-limited
    # to roughly tier-2 so the default 30 s is comfortable).
    _HEALTH_CHECK_INTERVAL: float = 30.0  # seconds between probes
    _HEALTH_CHECK_TIMEOUT: float = 10.0  # per-probe timeout
    _HEALTH_FAILURE_THRESHOLD: int = 2  # consecutive failures before rebuild

    # Instance state — declared here for type-checkers; populated lazily.
    _watchdog_task: asyncio.Task | None = None
    _rebuilding: bool = False

    # ── Required overrides ────────────────────────────────────────────────────

    async def _health_check(self) -> None:
        """Probe the upstream service. Raise on failure (anything truthy works).
        Subclasses override this with a cheap idempotent call.
        """
        raise NotImplementedError

    async def _health_rebuild(self) -> None:
        """Tear down the broken client and re-construct it, typically by
        clearing references and re-calling ``self.start()``."""
        raise NotImplementedError

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _start_health_watchdog(self) -> None:
        """Spawn the watchdog task once. Re-entrant safe: re-calling this
        from inside a rebuild path won't stack duplicate watchdogs."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._health_watchdog_loop())
            track = getattr(self, "track_task", None)
            if callable(track):
                track(self._watchdog_task)

    async def _health_watchdog_loop(self) -> None:
        consecutive = 0
        cls_name = type(self).__name__
        while True:
            try:
                await asyncio.sleep(self._HEALTH_CHECK_INTERVAL)
            except asyncio.CancelledError:
                return
            if self._rebuilding:
                continue
            try:
                await asyncio.wait_for(
                    self._health_check(),
                    timeout=self._HEALTH_CHECK_TIMEOUT,
                )
                if consecutive > 0:
                    logger.info(
                        "%s health check recovered (was failing for %d ticks)",
                        cls_name,
                        consecutive,
                    )
                consecutive = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                consecutive += 1
                logger.warning(
                    "%s health check failed (%d/%d): %s",
                    cls_name,
                    consecutive,
                    self._HEALTH_FAILURE_THRESHOLD,
                    exc,
                )
                if consecutive >= self._HEALTH_FAILURE_THRESHOLD:
                    try:
                        await self._do_health_rebuild()
                    except Exception as rebuild_exc:
                        logger.error(
                            "%s rebuild failed: %s — retrying after %d more failed checks",
                            cls_name,
                            rebuild_exc,
                            self._HEALTH_FAILURE_THRESHOLD,
                        )
                    finally:
                        # Re-arm whether or not the rebuild raised. Resetting only on
                        # success meant a permanently-failing rebuild (e.g. a rejected
                        # token) stayed at/above the threshold and re-triggered a
                        # rebuild on EVERY tick — flooding the log. Clearing the
                        # counter here caps rebuild attempts at one per
                        # _HEALTH_FAILURE_THRESHOLD checks while still retrying
                        # indefinitely so a transient outage eventually recovers.
                        consecutive = 0

    async def _do_health_rebuild(self) -> None:
        cls_name = type(self).__name__
        logger.warning("Rebuilding %s to recover network state", cls_name)
        self._rebuilding = True
        try:
            await self._health_rebuild()
            logger.info("%s rebuilt successfully", cls_name)
        finally:
            self._rebuilding = False
