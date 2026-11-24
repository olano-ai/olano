"""Agent-to-agent message bus for direct, broadcast, and delegate patterns."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentMessage:
    """A message sent between agents."""

    id: str
    from_agent: str
    to_agent: str  # "*" for broadcast
    content: str
    type: str = "message"  # "message" | "task" | "result" | "error"
    priority: int = 50
    correlation_id: str = ""  # Links replies to original messages
    data: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


class AgentMessageBus:
    """
    Direct and broadcast messaging between agents.

    Patterns:
    - Direct: send(from="merli", to="google_agent", content="...")
    - Broadcast: send(from="merli", to="*", content="...")
    - Subscribe: subscribe("google_agent", handler)
    - Delegate: delegate(task, to="github_agent") — fire-and-forget
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._broadcast_handlers: list[Any] = []

    def register_agent(self, agent_name: str) -> None:
        if agent_name not in self._queues:
            self._queues[agent_name] = asyncio.Queue(maxsize=100)

    def unregister_agent(self, agent_name: str) -> None:
        self._queues.pop(agent_name, None)

    async def send(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        type: str = "message",
        priority: int = 50,
        data: dict | None = None,
        correlation_id: str = "",
    ) -> str:
        """Send a message. Returns message ID."""
        import uuid

        msg = AgentMessage(
            id=str(uuid.uuid4())[:8],
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            type=type,
            priority=priority,
            correlation_id=correlation_id,
            data=data or {},
        )

        if to_agent == "*":
            # Broadcast
            for name, queue in self._queues.items():
                if name != from_agent:
                    try:
                        queue.put_nowait(msg)
                    except asyncio.QueueFull:
                        logger.warning("MessageBus: queue full for agent %s", name)
        else:
            target_queue = self._queues.get(to_agent)
            if target_queue:
                await target_queue.put(msg)
            else:
                logger.warning("MessageBus: agent '%s' not registered", to_agent)

        return msg.id

    async def receive(self, agent_name: str, timeout: float = 0.1) -> AgentMessage | None:
        """Non-blocking receive from an agent's queue."""
        queue = self._queues.get(agent_name)
        if not queue:
            return None
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def delegate(
        self,
        from_agent: str,
        to_agent: str,
        task: str,
        data: dict | None = None,
    ) -> str:
        """Delegate a task to another agent (fire-and-forget)."""
        return await self.send(
            from_agent=from_agent,
            to_agent=to_agent,
            content=task,
            type="task",
            data=data,
        )

    def pending_count(self, agent_name: str) -> int:
        q = self._queues.get(agent_name)
        return q.qsize() if q else 0
