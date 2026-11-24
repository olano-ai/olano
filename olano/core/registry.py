"""Agent registry — loads, caches, and retrieves built agents by name."""

from __future__ import annotations

import asyncio
from typing import Any

from olano.config.agent_config import AgentConfig
from olano.config.loader import list_agents, load_agent_config
from olano.core.builder import build_agent, build_agent_async


class AgentRegistry:
    """
    Singleton registry that lazily builds and caches deepagent instances.

    Usage:
        registry = AgentRegistry()
        agent = await registry.get("researcher")
        result = agent.invoke(...)
    """

    def __init__(self) -> None:
        self._configs: dict[str, AgentConfig] = {}
        self._agents: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    def load_config(self, name: str) -> AgentConfig:
        if name not in self._configs:
            self._configs[name] = load_agent_config(name)
        return self._configs[name]

    async def get(self, name: str, use_mcp: bool = True) -> Any:
        """Return a cached or freshly built agent graph."""
        async with self._lock:
            if name not in self._agents:
                config = self.load_config(name)
                self._agents[name] = await build_agent_async(
                    config,
                    include_mcp=use_mcp and bool(config.mcp_servers),
                )
        return self._agents[name]

    def get_sync(self, name: str) -> Any:
        """Build and return agent synchronously (no MCP)."""
        if name not in self._agents:
            config = self.load_config(name)
            self._agents[name] = build_agent(config)
        return self._agents[name]

    def invalidate(self, name: str) -> None:
        """Remove agent from cache (forces rebuild on next get)."""
        self._agents.pop(name, None)
        self._configs.pop(name, None)

    def list(self) -> list[str]:
        return list_agents()

    def get_config(self, name: str) -> AgentConfig:
        return self.load_config(name)


# Global registry instance
_registry: AgentRegistry | None = None


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
