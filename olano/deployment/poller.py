"""Background refresh loop for cloud-managed deployment settings.

Lifecycle mirrors :class:`olano.memory.self_learning.SelfLearningService`
(``start()``/``stop()``, a tracked asyncio task, never crashes the runtime);
the loop shape mirrors ``RuntimeServer._pulse_delivery_loop``.

On every effective-settings change the poller runs the **transition
handlers** so a remote change bites live state, not just future requests:
- closes active terminal sessions when the terminal/shell entitlement drops;
- broadcasts a ``deployment_settings_changed`` SSE event so the dashboard
  refreshes its capabilities (tab visibility, admin card) immediately.

Suspension/permission propagation latency ≈ the refresh interval (default
15 min, bounded by the artifact's signed ``refresh_after``) — an explicit
product decision, recorded in the deployment-settings design record, §7.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from olano.deployment import resolver

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 15 * 60
MIN_INTERVAL_SECONDS = 60


class SettingsPoller:
    """Periodically refreshes cloud settings and applies live transitions."""

    def __init__(
        self,
        push_event: Any | None = None,
        *,
        on_settled: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        # push_event(event_type: str, payload: dict) — the runtime API's SSE
        # broadcaster; optional so the poller is testable standalone.
        self._push_event = push_event
        # on_settled() — awaited on every change that lands the box OUTSIDE
        # quarantine. The runtime hooks its first-boot retry here: a step the
        # "no artifact yet" quarantine refused at boot (the default agent's
        # install) runs again the moment the settings arrive, instead of at a
        # next boot a running box never has. Errors are logged, never raised.
        self._on_settled = on_settled
        self._task: asyncio.Task[None] | None = None
        self._last_fingerprint: tuple | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._last_fingerprint = self._fingerprint(resolver.deployment_status())
            self._task = asyncio.create_task(self._loop(), name="deployment_settings_poller")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @staticmethod
    def _fingerprint(status: resolver.DeploymentStatus) -> tuple:
        return (
            status.posture,
            status.source,
            status.policy_revision,
            status.issued_at,
            tuple(sorted(status.settings.model_dump(mode="json").items(), key=str)),
        )

    def _next_interval(self, status: resolver.DeploymentStatus) -> float:
        interval = float(DEFAULT_INTERVAL_SECONDS)
        if status.refresh_after:
            interval = min(interval, max(status.refresh_after - time.time(), 0))
        interval = max(interval, MIN_INTERVAL_SECONDS)
        # Jitter so a fleet doesn't thundering-herd the control plane.
        return interval * random.uniform(0.9, 1.1)

    async def _loop(self) -> None:
        while True:
            try:
                status = await resolver.refresh_async()
                fingerprint = self._fingerprint(status)
                if fingerprint != self._last_fingerprint:
                    self._last_fingerprint = fingerprint
                    await self._apply_transitions(status)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("deployment settings poll failed")
                status = resolver.deployment_status()
            await asyncio.sleep(self._next_interval(status))

    async def _apply_transitions(self, status: resolver.DeploymentStatus) -> None:
        """Make a settings change bite live state (never raises)."""
        logger.info(
            "Deployment settings changed: posture=%s source=%s revision=%s%s",
            status.posture,
            status.source,
            status.policy_revision,
            f" error={status.error}" if status.error else "",
        )
        settings = status.settings
        if not settings.feature_enabled("terminal") or not settings.feature_enabled("shell"):
            try:
                from olano.runtime import terminal as terminal_mod

                closed = await terminal_mod.close_all_sessions()
                if closed:
                    logger.info("Closed %d live terminal session(s) after settings change", closed)
            except Exception:
                logger.exception("closing terminal sessions failed")
        if self._push_event is not None:
            try:
                self._push_event(
                    "deployment_settings_changed",
                    {
                        "posture": status.posture,
                        "source": status.source,
                        "policy_revision": status.policy_revision,
                        "error": status.error,
                    },
                )
            except Exception:
                logger.exception("broadcasting settings change failed")
        if self._on_settled is not None and not status.quarantined:
            try:
                await self._on_settled()
            except Exception:
                logger.exception("post-settle hook failed")
