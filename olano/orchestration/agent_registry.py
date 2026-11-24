"""Agent registry — tracks capabilities, health, and policies for all running agents."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentEntry:
    name: str
    description: str = ""
    emoji: str = "🤖"
    model: str = ""
    capabilities: list[str] = field(default_factory=list)  # tool names
    channels: list[str] = field(default_factory=list)
    trust_level: int = 2
    status: str = "stopped"  # "starting" | "running" | "stopped" | "error"
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        if self.status != "running":
            return False
        age_seconds = (datetime.utcnow() - self.last_heartbeat).total_seconds()
        return age_seconds < 300  # 5 minutes

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "emoji": self.emoji,
            "model": self.model,
            "capabilities": self.capabilities,
            "channels": self.channels,
            "trust_level": self.trust_level,
            "status": self.status,
            "healthy": self.is_healthy,
            "last_heartbeat": self.last_heartbeat.isoformat(),
            "error": self.error,
        }


class AgentRegistry:
    """Central registry of all agents in the runtime."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentEntry] = {}

    def register(self, entry: AgentEntry) -> None:
        self._agents[entry.name] = entry
        logger.info("Registry: registered agent '%s' (%s)", entry.name, entry.model)

    def update_status(self, name: str, status: str, error: str = "") -> None:
        entry = self._agents.get(name)
        if entry:
            entry.status = status
            entry.error = error
            entry.last_heartbeat = datetime.utcnow()

    def heartbeat(self, name: str) -> None:
        entry = self._agents.get(name)
        if entry:
            entry.last_heartbeat = datetime.utcnow()

    def get(self, name: str) -> AgentEntry | None:
        return self._agents.get(name)

    def list_all(self) -> list[AgentEntry]:
        return list(self._agents.values())

    def list_healthy(self) -> list[AgentEntry]:
        return [e for e in self._agents.values() if e.is_healthy]

    def find_by_capability(self, capability: str) -> list[AgentEntry]:
        return [e for e in self._agents.values() if capability in e.capabilities and e.is_healthy]

    def to_summary(self) -> str:
        """Format registry as a short summary string."""
        agents = self.list_all()
        if not agents:
            return "No agents registered."
        lines = [f"Agents ({len(agents)}):"]
        for entry in agents:
            health_icon = "✓" if entry.is_healthy else "✗"
            lines.append(
                f"  {health_icon} {entry.emoji} {entry.name} [{entry.status}] {entry.model}"
            )
        return "\n".join(lines)
