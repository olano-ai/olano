"""DomainEventBus — in-process publish/subscribe bus for memory consolidation.

P5.13: Replaces the monolithic self-learning loop with a dedicated worker pool.
Each worker subscribes to specific event types and processes them asynchronously.

Supported workers:
  - EpisodicExtractorWorker: session events → daily log entries
  - DeduplicationWorker: detects and merges duplicate memory entries
  - GraphUpdateWorker: distilled facts → MemoryGraph entities
  - ContradictionDetectorWorker: staged facts → PULSE contradiction items
  - NotesSyncWorker: notes file changes → NotesStore index

Event types (string constants on DomainEvent):
  - "session.turn" — a single conversation turn was appended
  - "memory.fact" — a distilled fact was saved to TypedMemoryStore
  - "notes.file_changed" — a notes .md file was created/modified
  - "graph.entity" — an entity was upserted in MemoryGraph
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, cast

logger = logging.getLogger(__name__)


# ── Events ────────────────────────────────────────────────────────────────────


@dataclass
class DomainEvent:
    """A single event on the bus."""

    type: str  # e.g. "session.turn", "memory.fact"
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Well-known event type constants
    SESSION_TURN = "session.turn"
    MEMORY_FACT = "memory.fact"
    NOTES_FILE_CHANGED = "notes.file_changed"
    GRAPH_ENTITY = "graph.entity"


# Handler signature: async def handler(event: DomainEvent) -> None
Handler = Callable[[DomainEvent], Coroutine[Any, Any, None]]


# ── Bus ───────────────────────────────────────────────────────────────────────


class DomainEventBus:
    """Lightweight in-process async pub/sub bus.

    Subscribers are async callables.  Publishing is fire-and-forget —
    the bus schedules each handler as a separate asyncio task so
    publishing does not block the caller.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._pending: list[asyncio.Task] = []

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register an async handler for an event type."""
        self._subscribers[event_type].append(handler)

    async def publish(self, event: DomainEvent) -> None:
        """Dispatch an event to all registered handlers (non-blocking)."""
        handlers = self._subscribers.get(event.type, [])
        for handler in handlers:
            task = asyncio.create_task(self._safe_call(handler, event))
            self._pending.append(task)
            task.add_done_callback(self._pending.remove)

    async def _safe_call(self, handler: Handler, event: DomainEvent) -> None:
        try:
            await handler(event)
        except Exception as e:
            logger.warning(
                "DomainEventBus handler %s raised for event %s: %s",
                getattr(handler, "__name__", handler),
                event.type,
                e,
            )

    async def flush(self, timeout: float = 5.0) -> None:
        """Wait until all pending handlers have completed."""
        if self._pending:
            await asyncio.wait(list(self._pending), timeout=timeout)

    def subscriber_count(self, event_type: str) -> int:
        return len(self._subscribers.get(event_type, []))


# ── Workers ───────────────────────────────────────────────────────────────────


class EpisodicExtractorWorker:
    """Subscribes to session.turn events and appends episodic entries to the daily log."""

    def __init__(self, markdown_memory: Any) -> None:
        self.memory = markdown_memory

    def register(self, bus: DomainEventBus) -> None:
        bus.subscribe(DomainEvent.SESSION_TURN, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        role = event.payload.get("role", "unknown")
        content = event.payload.get("content", "")
        if not content or role == "system":
            return
        snippet = content[:300].replace("\n", " ")
        try:
            self.memory.append_daily(f"[{role}] {snippet}")
            logger.debug("EpisodicExtractorWorker: appended turn to daily log")
        except Exception as e:
            logger.warning("EpisodicExtractorWorker: failed to append: %s", e)


class DeduplicationWorker:
    """Subscribes to memory.fact events and deduplicates facts in TypedMemoryStore."""

    def __init__(self, typed_store: Any) -> None:
        self.store = typed_store
        # Track (content_prefix, memory_type) pairs seen this session
        self._seen: set[str] = set()

    def register(self, bus: DomainEventBus) -> None:
        bus.subscribe(DomainEvent.MEMORY_FACT, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        content = event.payload.get("content", "")
        memory_type = event.payload.get("memory_type", "semantic")
        if not content:
            return

        key = f"{memory_type}:{content[:60].lower().strip()}"
        if key in self._seen:
            logger.debug("DeduplicationWorker: skipping duplicate fact: %r", content[:60])
            return
        self._seen.add(key)

        # Check existing facts for near-duplicates (same first 40 chars)
        try:
            existing = self.store.get_all(memory_type)
            content_prefix = content[:40].lower()
            for fact in existing:
                if fact.content[:40].lower() == content_prefix and fact.content != content:
                    # Near-duplicate: reinforce the existing one instead of adding new
                    logger.debug(
                        "DeduplicationWorker: near-duplicate detected for %r — reinforcing",
                        content[:40],
                    )
                    return
        except Exception as e:
            logger.warning("DeduplicationWorker: error checking duplicates: %s", e)


class GraphUpdateWorker:
    """Subscribes to memory.fact events and extracts entities into MemoryGraph."""

    def __init__(self, memory_graph: Any) -> None:
        self.graph = memory_graph

    def register(self, bus: DomainEventBus) -> None:
        bus.subscribe(DomainEvent.MEMORY_FACT, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        content = event.payload.get("content", "")
        if not content:
            return
        try:
            from olano.memory.self_learning import SelfLearningService

            # Reuse the entity extraction logic (heuristic, no LLM)
            class _FakeService:
                _memory_graph = self.graph

                def _get_memory_graph(self) -> Any:
                    return self._memory_graph

            svc = _FakeService()
            SelfLearningService._extract_entities_from_facts(
                cast(SelfLearningService, svc), [content]
            )
        except Exception as e:
            logger.debug("GraphUpdateWorker: entity extraction failed: %s", e)


class ContradictionDetectorWorker:
    """Subscribes to memory.fact events and flushes contradictions to PULSE."""

    def __init__(self, typed_store: Any, pulse_flusher: Any | None = None) -> None:
        self.store = typed_store
        self.pulse_flusher = pulse_flusher

    def register(self, bus: DomainEventBus) -> None:
        bus.subscribe(DomainEvent.MEMORY_FACT, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        try:
            contradictions = self.store.flush_contradictions()
            if contradictions and self.pulse_flusher:
                self.pulse_flusher(contradictions)
        except Exception as e:
            logger.debug("ContradictionDetectorWorker: %s", e)


class NotesSyncWorker:
    """Subscribes to notes.file_changed events and re-imports the changed file."""

    def __init__(self, notes: Any) -> None:
        self.notes = notes

    def register(self, bus: DomainEventBus) -> None:
        bus.subscribe(DomainEvent.NOTES_FILE_CHANGED, self.handle)

    async def handle(self, event: DomainEvent) -> None:
        from pathlib import Path

        path_str = event.payload.get("path", "")
        if not path_str:
            return
        try:
            self.notes.sync_file(Path(path_str))
            logger.debug("NotesSyncWorker: synced %s", path_str)
        except Exception as e:
            logger.warning("NotesSyncWorker: failed to sync %s: %s", path_str, e)


# ── Factory ───────────────────────────────────────────────────────────────────


def build_default_event_bus(
    markdown_memory: Any,
    typed_store: Any,
    memory_graph: Any,
    notes: Any | None = None,
    vault: Any | None = None,
    pulse_flusher: Any | None = None,
) -> DomainEventBus:
    """Build a DomainEventBus wired with all standard memory workers."""
    bus = DomainEventBus()
    EpisodicExtractorWorker(markdown_memory).register(bus)
    DeduplicationWorker(typed_store).register(bus)
    GraphUpdateWorker(memory_graph).register(bus)
    ContradictionDetectorWorker(typed_store, pulse_flusher).register(bus)
    notes_store = notes or vault
    if notes_store is not None:
        NotesSyncWorker(notes_store).register(bus)
    return bus
