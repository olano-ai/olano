"""Tests for P5.13: DomainEventBus and memory consolidation workers."""

from __future__ import annotations

import pytest


@pytest.fixture
def bus():
    from olano.memory.domain_event_bus import DomainEventBus

    return DomainEventBus()


@pytest.fixture
def markdown_memory(tmp_path):
    from olano.memory.markdown_memory import MarkdownMemory

    return MarkdownMemory(tmp_path)


@pytest.fixture
def typed_store(tmp_path):
    from olano.memory.typed_memory import TypedMemoryStore

    return TypedMemoryStore(state_memory_dir=tmp_path / "state" / "memory")


@pytest.fixture
def memory_graph(tmp_path):
    from olano.memory.memory_graph import MemoryGraph

    return MemoryGraph(db_path=tmp_path / "graph.db")


# ── DomainEventBus core ───────────────────────────────────────────────────────


class TestDomainEventBus:
    async def test_subscribe_and_publish(self, bus):
        from olano.memory.domain_event_bus import DomainEvent

        received = []

        async def handler(event: DomainEvent) -> None:
            received.append(event)

        bus.subscribe(DomainEvent.SESSION_TURN, handler)
        await bus.publish(
            DomainEvent(type=DomainEvent.SESSION_TURN, payload={"role": "user", "content": "hello"})
        )
        await bus.flush()
        assert len(received) == 1
        assert received[0].payload["role"] == "user"

    async def test_multiple_subscribers_for_same_event(self, bus):
        from olano.memory.domain_event_bus import DomainEvent

        calls = []

        async def h1(e):
            calls.append("h1")

        async def h2(e):
            calls.append("h2")

        bus.subscribe(DomainEvent.MEMORY_FACT, h1)
        bus.subscribe(DomainEvent.MEMORY_FACT, h2)
        await bus.publish(DomainEvent(type=DomainEvent.MEMORY_FACT))
        await bus.flush()
        assert "h1" in calls
        assert "h2" in calls

    async def test_subscriber_count(self, bus):
        from olano.memory.domain_event_bus import DomainEvent

        async def noop(e):
            pass

        bus.subscribe(DomainEvent.SESSION_TURN, noop)
        bus.subscribe(DomainEvent.SESSION_TURN, noop)
        assert bus.subscriber_count(DomainEvent.SESSION_TURN) == 2

    async def test_no_subscriber_for_event_type(self, bus):
        from olano.memory.domain_event_bus import DomainEvent

        # Should not raise; handlers just aren't called
        await bus.publish(DomainEvent(type="unknown.event"))
        await bus.flush()

    async def test_handler_error_does_not_crash_bus(self, bus):
        from olano.memory.domain_event_bus import DomainEvent

        async def bad_handler(e):
            raise RuntimeError("Intentional test error")

        bus.subscribe(DomainEvent.SESSION_TURN, bad_handler)
        # Should not raise
        await bus.publish(DomainEvent(type=DomainEvent.SESSION_TURN))
        await bus.flush()


# ── EpisodicExtractorWorker ───────────────────────────────────────────────────


class TestEpisodicExtractorWorker:
    async def test_session_turn_appended_to_daily_log(self, bus, markdown_memory):
        from olano.memory.domain_event_bus import DomainEvent, EpisodicExtractorWorker

        worker = EpisodicExtractorWorker(markdown_memory)
        worker.register(bus)

        await bus.publish(
            DomainEvent(
                type=DomainEvent.SESSION_TURN,
                payload={"role": "user", "content": "Please help me debug the auth flow."},
            )
        )
        await bus.flush()

        today_log = markdown_memory.read_today()
        assert "auth flow" in today_log

    async def test_system_turns_are_skipped(self, bus, markdown_memory):
        from olano.memory.domain_event_bus import DomainEvent, EpisodicExtractorWorker

        worker = EpisodicExtractorWorker(markdown_memory)
        worker.register(bus)

        await bus.publish(
            DomainEvent(
                type=DomainEvent.SESSION_TURN,
                payload={"role": "system", "content": "Internal system message."},
            )
        )
        await bus.flush()

        today_log = markdown_memory.read_today()
        assert "Internal system message" not in today_log

    async def test_empty_content_is_skipped(self, bus, markdown_memory):
        from olano.memory.domain_event_bus import DomainEvent, EpisodicExtractorWorker

        worker = EpisodicExtractorWorker(markdown_memory)
        worker.register(bus)
        original = markdown_memory.read_today()

        await bus.publish(
            DomainEvent(
                type=DomainEvent.SESSION_TURN,
                payload={"role": "user", "content": ""},
            )
        )
        await bus.flush()
        assert markdown_memory.read_today() == original


# ── DeduplicationWorker ───────────────────────────────────────────────────────


class TestDeduplicationWorker:
    async def test_dedup_worker_registers_on_bus(self, bus, typed_store):
        from olano.memory.domain_event_bus import DeduplicationWorker, DomainEvent

        worker = DeduplicationWorker(typed_store)
        worker.register(bus)
        assert bus.subscriber_count(DomainEvent.MEMORY_FACT) >= 1

    async def test_duplicate_facts_are_tracked(self, bus, typed_store):
        from olano.memory.domain_event_bus import DeduplicationWorker, DomainEvent

        worker = DeduplicationWorker(typed_store)
        worker.register(bus)

        fact = "User prefers dark mode in VS Code"
        await bus.publish(
            DomainEvent(
                type=DomainEvent.MEMORY_FACT, payload={"content": fact, "memory_type": "preference"}
            )
        )
        await bus.publish(
            DomainEvent(
                type=DomainEvent.MEMORY_FACT, payload={"content": fact, "memory_type": "preference"}
            )
        )
        await bus.flush()

        # The same fact key should appear only once in _seen
        key = f"preference:{fact[:60].lower().strip()}"
        assert key in worker._seen

    async def test_different_facts_not_deduplicated(self, bus, typed_store):
        from olano.memory.domain_event_bus import DeduplicationWorker, DomainEvent

        worker = DeduplicationWorker(typed_store)
        worker.register(bus)

        await bus.publish(
            DomainEvent(
                type=DomainEvent.MEMORY_FACT,
                payload={"content": "Fact A about Python", "memory_type": "semantic"},
            )
        )
        await bus.publish(
            DomainEvent(
                type=DomainEvent.MEMORY_FACT,
                payload={"content": "Fact B about Go", "memory_type": "semantic"},
            )
        )
        await bus.flush()

        assert len(worker._seen) == 2


# ── build_default_event_bus ───────────────────────────────────────────────────


class TestBuildDefaultEventBus:
    def test_default_bus_has_workers_registered(
        self, tmp_path, markdown_memory, typed_store, memory_graph
    ):
        from olano.memory.domain_event_bus import DomainEvent, build_default_event_bus

        bus = build_default_event_bus(
            markdown_memory=markdown_memory,
            typed_store=typed_store,
            memory_graph=memory_graph,
        )
        assert bus.subscriber_count(DomainEvent.SESSION_TURN) >= 1
        assert (
            bus.subscriber_count(DomainEvent.MEMORY_FACT) >= 2
        )  # DeduplicationWorker + GraphUpdateWorker

    async def test_end_to_end_session_event(
        self, tmp_path, markdown_memory, typed_store, memory_graph
    ):
        from olano.memory.domain_event_bus import DomainEvent, build_default_event_bus

        bus = build_default_event_bus(
            markdown_memory=markdown_memory,
            typed_store=typed_store,
            memory_graph=memory_graph,
        )

        await bus.publish(
            DomainEvent(
                type=DomainEvent.SESSION_TURN,
                payload={"role": "assistant", "content": "I deployed the service to production."},
            )
        )
        await bus.flush()

        log = markdown_memory.read_today()
        assert "deployed" in log


# ── P5.14: dual system routing via MemoryRouter.route_query ──────────────────


class TestDualSystemRouting:
    def test_similarity_routes_to_rag(self):
        from olano.memory.memory_router import MemoryRouter

        assert MemoryRouter.route_query("What is similar to PostgreSQL?") == "rag"

    def test_relationship_routes_to_graph(self):
        from olano.memory.memory_router import MemoryRouter

        assert MemoryRouter.route_query("Who works on the auth module?") == "graph"

    def test_concept_routes_to_vault(self):
        from olano.memory.memory_router import MemoryRouter

        assert MemoryRouter.route_query("What is Kubernetes?") == "notes"
