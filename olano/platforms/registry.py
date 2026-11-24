"""Platform registry — holds live ``BasePlatformAdapter`` instances.

Multi-tenant: adapters are keyed by ``(agent_name, platform)`` so each
agent can own its own e.g. WhatsApp number or Slack workspace without
the second registration silently overwriting the first.

Parallel to the legacy ``olano.channels.registry.ChannelRegistry``,
but typed against the richer hermes-derived adapter API. Existing
callers that reach for ``ChannelRegistry`` continue to work during the
migration; new code should prefer ``PlatformRegistry``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from olano.platforms.base import BasePlatformAdapter
    from olano.platforms.config import Platform

logger = logging.getLogger(__name__)


def _platform_key(platform: Platform | str) -> str:
    return platform.value if hasattr(platform, "value") else str(platform)


class PlatformRegistry:
    """Central registry of connected platform adapters.

    Adapters register themselves on successful ``connect()`` (one entry
    per agent, per platform) and are looked up by ``(agent, platform)``
    for outbound delivery (cron jobs, pulse notifications, agent tools).

    When a caller doesn't specify an agent (legacy code path), lookups
    fall back to the single registered adapter of that platform — with
    a warning logged if more than one agent owns it, because the
    selection becomes non-deterministic.
    """

    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], BasePlatformAdapter] = {}

    # ── registration ──────────────────────────────────────────────────
    def register(
        self,
        agent_name: str,
        platform: Platform | str,
        adapter: BasePlatformAdapter,
    ) -> None:
        key = (agent_name, _platform_key(platform))
        self._adapters[key] = adapter
        logger.info("PlatformRegistry: registered '%s' for agent '%s'", key[1], agent_name)

    def unregister(self, agent_name: str, platform: Platform | str) -> None:
        self._adapters.pop((agent_name, _platform_key(platform)), None)

    def unregister_agent(self, agent_name: str) -> None:
        for key in [k for k in self._adapters if k[0] == agent_name]:
            self._adapters.pop(key, None)

    # ── lookups ───────────────────────────────────────────────────────
    def get(
        self,
        agent_name: str,
        platform: Platform | str,
    ) -> BasePlatformAdapter | None:
        if agent_name:
            adapter = self._adapters.get((agent_name, _platform_key(platform)))
            if adapter is not None:
                return adapter
            logger.warning(
                "PlatformRegistry: agent '%s' has no '%s' adapter registered",
                agent_name,
                _platform_key(platform),
            )
            return None
        # Legacy/ambiguous lookup — fall back to any adapter of this
        # platform; warn if there's more than one.
        peers = self.for_platform(platform)
        if not peers:
            return None
        if len(peers) == 1:
            return next(iter(peers.values()))
        logger.warning(
            "PlatformRegistry: ambiguous routing — %d agents own '%s' (%s). "
            "Pass agent_name to disambiguate.",
            len(peers),
            _platform_key(platform),
            ", ".join(sorted(peers)),
        )
        return next(iter(peers.values()))

    def for_agent(self, agent_name: str) -> dict[str, BasePlatformAdapter]:
        """Return ``{platform_value: adapter}`` for one agent."""
        return {
            plat: adapter
            for (owner, plat), adapter in self._adapters.items()
            if owner == agent_name
        }

    def for_platform(self, platform: Platform | str) -> dict[str, BasePlatformAdapter]:
        """Return ``{agent_name: adapter}`` for one platform."""
        key = _platform_key(platform)
        return {owner: adapter for (owner, plat), adapter in self._adapters.items() if plat == key}

    def has(self, agent_name: str, platform: Platform | str) -> bool:
        return (agent_name, _platform_key(platform)) in self._adapters

    def list_platforms(self) -> list[tuple[str, str]]:
        """Returns ``[(agent_name, platform_value), ...]`` for every adapter."""
        return list(self._adapters)


async def republish_command_menus(agent_name: str) -> dict[str, bool]:
    """Re-publish one agent's slash-command menu on every connected adapter.

    Called after the agent's ``commands`` config changes (the dashboard
    Commands tab, ``/reload-agent``) so what Telegram's ``/`` list and
    Discord's command tree advertise matches what the inbound gate will now
    actually run. Adapters with no menu to publish no-op.

    Never raises: a menu is an autocomplete hint, and one unreachable platform
    must not fail the save that triggered this. Returns ``{platform: published}``.
    """
    adapters = get_platform_registry().for_agent(agent_name)
    if not adapters:
        return {}
    results = await asyncio.gather(
        *(adapter.publish_command_menu() for adapter in adapters.values()),
        return_exceptions=True,
    )
    published: dict[str, bool] = {}
    for platform, result in zip(adapters, results, strict=False):
        if isinstance(result, BaseException):
            logger.warning(
                "Command menu re-publish failed for '%s' on %s: %s", agent_name, platform, result
            )
            published[platform] = False
        else:
            published[platform] = bool(result)
    return published


_default_registry: PlatformRegistry | None = None


def get_platform_registry() -> PlatformRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = PlatformRegistry()
    return _default_registry
