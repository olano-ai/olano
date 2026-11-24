"""Global event bus for operational events across all agents and channels."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Event:
    type: str  # e.g. "agent.started", "message.received", "tool.called"
    source: str  # Component that emitted the event
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    trace_id: str = ""

    def __post_init__(self):
        if not self.trace_id:
            import uuid

            self.trace_id = str(uuid.uuid4())[:8]


EventHandler = Callable[[Event], Coroutine]


class EventBus:
    """
    Async pub/sub event bus.

    Usage::

        bus = EventBus()
        bus.subscribe("message.received", my_handler)
        await bus.emit(Event(type="message.received", source="telegram", data={...}))
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._wildcard: list[EventHandler] = []
        self._history: list[Event] = []
        self._max_history = 500

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe to a specific event type. Use '*' for all events."""
        if event_type == "*":
            self._wildcard.append(handler)
        else:
            self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        if event_type == "*":
            if handler in self._wildcard:
                self._wildcard.remove(handler)
        else:
            handlers = self._subscribers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

    async def emit(self, event: Event) -> None:
        """Emit an event to all subscribers. Errors in handlers are logged, not raised."""
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        handlers = self._subscribers.get(event.type, []) + self._wildcard
        if not handlers:
            return

        coros = [h(event) for h in handlers]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("EventBus: handler error for %s: %s", event.type, r)

    async def emit_simple(self, event_type: str, source: str, **data: Any) -> None:
        """Convenience method."""
        await self.emit(Event(type=event_type, source=source, data=data))

    def recent(self, event_type: str | None = None, limit: int = 20) -> list[Event]:
        events = (
            self._history if not event_type else [e for e in self._history if e.type == event_type]
        )
        return events[-limit:]


# Module-level singleton
_default_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBus()
    return _default_bus
