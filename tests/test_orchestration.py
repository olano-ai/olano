"""Tests for olano.orchestration.*"""

from __future__ import annotations

import asyncio

# ─────────────────────────────────── EventBus ────────────────────────────────


class TestEventBus:
    def test_emit_and_receive(self):
        from olano.orchestration.event_bus import Event, EventBus

        bus = EventBus()
        received: list = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe("test.event", handler)
        asyncio.get_event_loop().run_until_complete(
            bus.emit_simple("test.event", source="unit_test", key="value")
        )
        assert len(received) == 1
        assert received[0].type == "test.event"
        assert received[0].data["key"] == "value"

    def test_wildcard_subscription(self):
        from olano.orchestration.event_bus import EventBus

        bus = EventBus()
        received: list = []

        async def handler(event):
            received.append(event.type)

        bus.subscribe("*", handler)

        async def run():
            await bus.emit_simple("a.b", source="x")
            await bus.emit_simple("c.d", source="y")

        asyncio.get_event_loop().run_until_complete(run())
        assert "a.b" in received
        assert "c.d" in received

    def test_no_handler_no_error(self):
        from olano.orchestration.event_bus import EventBus

        bus = EventBus()
        asyncio.get_event_loop().run_until_complete(
            bus.emit_simple("unknown.event", source="test")
        )  # Should not raise

    def test_recent_history(self):
        from olano.orchestration.event_bus import EventBus

        bus = EventBus()

        async def run():
            await bus.emit_simple("msg.in", source="telegram")
            await bus.emit_simple("msg.out", source="agent")

        asyncio.get_event_loop().run_until_complete(run())
        all_recent = bus.recent(limit=10)
        assert len(all_recent) == 2

        filtered = bus.recent("msg.in", limit=10)
        assert len(filtered) == 1
        assert filtered[0].type == "msg.in"

    def test_history_capped(self):
        from olano.orchestration.event_bus import EventBus

        bus = EventBus()
        bus._max_history = 5

        async def run():
            for i in range(10):
                await bus.emit_simple(f"event.{i}", source="test")

        asyncio.get_event_loop().run_until_complete(run())
        assert len(bus._history) <= 5

    def test_handler_error_does_not_raise(self):
        from olano.orchestration.event_bus import EventBus

        bus = EventBus()

        async def bad_handler(event):
            raise RuntimeError("oops")

        bus.subscribe("bad.event", bad_handler)
        # Should not raise despite handler error
        asyncio.get_event_loop().run_until_complete(bus.emit_simple("bad.event", source="test"))

    def test_get_event_bus_singleton(self):
        from olano.orchestration.event_bus import get_event_bus

        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2


# ─────────────────────────────────── AgentRegistry ───────────────────────────


class TestAgentRegistry:
    def test_register_and_get(self):
        from olano.orchestration.agent_registry import AgentEntry, AgentRegistry

        reg = AgentRegistry()
        entry = AgentEntry(name="merli", model="anthropic:claude-sonnet-4-6", status="running")
        reg.register(entry)

        found = reg.get("merli")
        assert found is not None
        assert found.name == "merli"

    def test_update_status(self):
        from olano.orchestration.agent_registry import AgentEntry, AgentRegistry

        reg = AgentRegistry()
        reg.register(AgentEntry(name="agent1", status="starting"))
        reg.update_status("agent1", "running")
        assert reg.get("agent1").status == "running"

    def test_is_healthy_running(self):
        from olano.orchestration.agent_registry import AgentEntry

        entry = AgentEntry(name="x", status="running")
        assert entry.is_healthy

    def test_is_healthy_stopped(self):
        from olano.orchestration.agent_registry import AgentEntry

        entry = AgentEntry(name="x", status="stopped")
        assert not entry.is_healthy

    def test_list_healthy(self):
        from olano.orchestration.agent_registry import AgentEntry, AgentRegistry

        reg = AgentRegistry()
        reg.register(AgentEntry(name="a", status="running"))
        reg.register(AgentEntry(name="b", status="stopped"))
        healthy = reg.list_healthy()
        assert len(healthy) == 1
        assert healthy[0].name == "a"

    def test_find_by_capability(self):
        from olano.orchestration.agent_registry import AgentEntry, AgentRegistry

        reg = AgentRegistry()
        reg.register(AgentEntry(name="devbot", status="running", capabilities=["github", "linear"]))
        reg.register(AgentEntry(name="finbot", status="running", capabilities=["finance"]))

        devs = reg.find_by_capability("github")
        assert len(devs) == 1
        assert devs[0].name == "devbot"

    def test_to_summary_format(self):
        from olano.orchestration.agent_registry import AgentEntry, AgentRegistry

        reg = AgentRegistry()
        reg.register(AgentEntry(name="agent1", status="running", model="anthropic:x"))
        summary = reg.to_summary()
        assert "agent1" in summary
        assert "running" in summary

    def test_to_dict(self):
        from olano.orchestration.agent_registry import AgentEntry

        entry = AgentEntry(name="test", status="running", trust_level=3)
        d = entry.to_dict()
        assert d["name"] == "test"
        assert d["trust_level"] == 3
        assert "healthy" in d


# ─────────────────────────────────── TaskBus ─────────────────────────────────


class TestTaskBus:
    def test_submit_and_get(self):
        from olano.orchestration.task_bus import TaskBus, TaskStatus

        bus = TaskBus()
        bus.register_agent("worker")

        async def run():
            task = await bus.submit("Do X", "Detailed instructions", "worker", "user")
            return task

        task = asyncio.get_event_loop().run_until_complete(run())
        assert task.id
        assert task.title == "Do X"
        assert task.status == TaskStatus.PENDING

    def test_next_task(self):
        from olano.orchestration.task_bus import TaskBus

        bus = TaskBus()
        bus.register_agent("worker")

        async def run():
            await bus.submit("Task A", "desc", "worker", "user")
            return await bus.next_task("worker")

        task = asyncio.get_event_loop().run_until_complete(run())
        assert task is not None
        assert task.title == "Task A"

    def test_next_task_empty_returns_none(self):
        from olano.orchestration.task_bus import TaskBus

        bus = TaskBus()
        bus.register_agent("idle")

        result = asyncio.get_event_loop().run_until_complete(bus.next_task("idle", timeout=0.01))
        assert result is None

    def test_update_status(self):
        from olano.orchestration.task_bus import TaskBus, TaskStatus

        bus = TaskBus()
        bus.register_agent("worker")

        async def run():
            task = await bus.submit("Task", "desc", "worker", "user")
            bus.update_status(task.id, TaskStatus.COMPLETED, result="done")
            return task.id

        task_id = asyncio.get_event_loop().run_until_complete(run())
        found = bus.get(task_id)
        assert found.status == TaskStatus.COMPLETED
        assert found.result == "done"

    def test_list_tasks_filtered(self):
        from olano.orchestration.task_bus import TaskBus

        bus = TaskBus()
        bus.register_agent("a")
        bus.register_agent("b")

        async def run():
            await bus.submit("For A", "desc", "a", "user")
            await bus.submit("For B", "desc", "b", "user")

        asyncio.get_event_loop().run_until_complete(run())
        a_tasks = bus.list_tasks(agent="a")
        assert len(a_tasks) == 1
        assert a_tasks[0].assigned_to == "a"

    def test_task_to_dict(self):
        from olano.orchestration.task_bus import TaskBus

        bus = TaskBus()
        bus.register_agent("w")

        async def run():
            return await bus.submit("T", "d", "w", "u", priority=75)

        task = asyncio.get_event_loop().run_until_complete(run())
        d = task.to_dict()
        assert d["priority"] == 75
        assert "status" in d
        assert "created_at" in d
