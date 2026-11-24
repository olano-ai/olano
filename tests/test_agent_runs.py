"""Detached agent runs (core/runs.py) — the object that lets a turn outlive the
browser that started it, and lets a returning browser rejoin it.

The API halves (``POST /api/chat/stream`` starting a detached run,
``GET /api/chat/attach`` replaying it, ``POST /api/chat/stop`` cancelling it)
are covered in test_runtime_api.py."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from olano.core import runs as runs_mod
from olano.core.runs import REGISTRY, RUN_END, AgentRun, RunRegistry


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


class TestRegistryLifecycle:
    def test_begin_claims_the_thread_and_refuses_a_second_run(self) -> None:
        reg = RunRegistry()
        first = reg.begin("chat", "alpha", "t1", "turn-1")
        assert first is not None
        # A thread holds one ordered conversation: braiding two turns into it
        # is the failure this refusal exists to prevent.
        assert reg.begin("chat", "alpha", "t1", "turn-2") is None
        # …but a different thread, and a different kind on the same thread, are
        # unrelated conversations.
        assert reg.begin("chat", "alpha", "t2", "turn-3") is not None
        assert reg.begin("group", "alpha", "t1", "turn-4") is not None

    def test_finishing_frees_the_thread_for_the_next_turn(self) -> None:
        reg = RunRegistry()
        first = reg.begin("chat", "alpha", "t1", "turn-1")
        assert first is not None
        reg.finish(first)
        assert reg.for_thread("chat", "alpha", "t1") is None
        assert reg.begin("chat", "alpha", "t1", "turn-2") is not None

    def test_a_superseded_run_finishing_late_cannot_evict_its_successor(self) -> None:
        reg = RunRegistry()
        first = reg.begin("chat", "alpha", "t1", "turn-1")
        assert first is not None
        reg.finish(first)
        second = reg.begin("chat", "alpha", "t1", "turn-2")
        assert second is not None
        reg.finish(first)  # late, out of order
        assert reg.get(second.run_id) is second

    def test_lookup_by_id_and_by_thread(self) -> None:
        reg = RunRegistry()
        run = reg.begin("chat", "alpha", "t1", "turn-1")
        assert run is not None
        assert reg.get(run.run_id) is run
        assert reg.for_thread("chat", "alpha", "t1") is run
        # for_thread reports only a RUNNING run — a finished one is not
        # something a client should be told to attach to as live.
        run.finish()
        assert reg.for_thread("chat", "alpha", "t1") is None

    def test_finished_runs_are_swept_after_the_retention_window(self, monkeypatch) -> None:
        reg = RunRegistry()
        run = reg.begin("chat", "alpha", "t1", "turn-1")
        assert run is not None
        run.finish()
        # Still attachable right after finishing: a phone that comes back a
        # moment after the answer landed must get the tail, not a 404.
        assert reg.get(run.run_id) is run
        run.ended_at -= runs_mod.RETENTION_S + 1
        assert reg.get(run.run_id) is None

    async def test_a_run_whose_task_died_never_holds_its_thread_hostage(self) -> None:
        """The failure this guards: a turn that raised before its own
        try/finally would never reach finish(), so the thread key would be held
        forever and every later turn on that conversation would 409 against a
        run that no longer exists. The task's state is the ground truth."""

        async def _boom() -> None:
            raise RuntimeError("died before finishing")

        reg = RunRegistry()
        run = reg.begin("chat", "alpha", "t1", "turn-1")
        assert run is not None
        run.task = asyncio.get_running_loop().create_task(_boom())
        with contextlib.suppress(RuntimeError):
            await run.task

        assert run.done is False  # it never got to say so…
        assert reg.for_thread("chat", "alpha", "t1") is None  # …but it is not live
        assert reg.begin("chat", "alpha", "t1", "turn-2") is not None  # the thread is free

    def test_stop_reports_whether_there_was_anything_to_stop(self) -> None:
        reg = RunRegistry()
        run = reg.begin("chat", "alpha", "t1", "turn-1")
        assert run is not None
        assert reg.stop_thread("chat", "alpha", "t1") is True
        assert run.stopped is True
        # Idempotent: two devices watching one turn both press Stop, and the
        # second is told honestly that there was nothing left to stop.
        assert reg.stop_thread("chat", "alpha", "t1") is False
        run.finish()
        assert reg.stop_thread("chat", "alpha", "t1") is False
        assert reg.stop("nonexistent") is False

    def test_a_just_finished_run_is_still_attachable_by_thread(self) -> None:
        """The race this closes: a page reads `run` from the thread payload,
        the turn finishes, and the attach a beat later must still deliver the
        ending rather than 404 the reconnect it was made for."""
        reg = RunRegistry()
        run = reg.begin("chat", "alpha", "t1", "turn-1")
        assert run is not None
        reg.finish(run)
        assert reg.for_thread("chat", "alpha", "t1") is None  # not busy any more
        assert reg.latest_for_thread("chat", "alpha", "t1") is run  # but still there

    def test_a_new_run_supersedes_the_finished_one_at_the_same_key(self) -> None:
        reg = RunRegistry()
        first = reg.begin("chat", "alpha", "t1", "turn-1")
        assert first is not None
        reg.finish(first)
        second = reg.begin("chat", "alpha", "t1", "turn-2")
        assert second is not None
        assert reg.latest_for_thread("chat", "alpha", "t1") is second


class TestFrameBuffer:
    def test_every_frame_is_numbered_from_one(self) -> None:
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        assert run.emit({"type": "start"}) == 1
        assert run.emit({"type": "text", "text": "hi"}) == 2
        backlog, _ = run.attach(0)
        assert [f["seq"] for f in backlog] == [1, 2]
        assert backlog[1]["text"] == "hi"

    def test_attach_replays_only_what_the_cursor_missed(self) -> None:
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        for i in range(5):
            run.emit({"type": "text", "text": str(i)})
        backlog, _ = run.attach(3)
        assert [f["text"] for f in backlog] == ["3", "4"]

    def test_a_finished_run_replays_its_backlog_and_offers_no_tail(self) -> None:
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        run.emit({"type": "text", "text": "done"})
        run.finish()
        backlog, queue = run.attach(0)
        assert [f["text"] for f in backlog] == ["done"]
        assert queue is None

    def test_the_ring_drops_the_oldest_frames_and_says_where_it_starts(self, monkeypatch) -> None:
        monkeypatch.setattr(runs_mod, "MAX_FRAMES", 4)
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        for i in range(10):
            run.emit({"type": "text", "text": str(i)})
        backlog, _ = run.attach(6)
        assert [f["text"] for f in backlog] == ["6", "7", "8", "9"]
        assert run.dropped_before == 6

    def test_a_byte_heavy_run_trims_on_weight_too(self, monkeypatch) -> None:
        monkeypatch.setattr(runs_mod, "MAX_BYTES", 4096)
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        for _ in range(20):
            run.emit({"type": "tool_result", "content": "x" * 1024})
        # Far fewer than 20 held, and the frame cap was never the binding one.
        assert len(run.frames) < 20
        assert run.dropped_before > 0

    def test_a_cursor_below_the_buffer_gets_resync_not_a_hole(self, monkeypatch) -> None:
        # A gap the client cannot see is worse than an honest refetch: it would
        # render a reply missing its middle with no seam to notice.
        monkeypatch.setattr(runs_mod, "MAX_FRAMES", 3)
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        for i in range(10):
            run.emit({"type": "text", "text": str(i)})
        backlog, _ = run.attach(1)
        assert [f["type"] for f in backlog] == ["resync"]


class TestViewers:
    async def test_replay_and_tail_have_no_gap_between_them(self) -> None:
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        run.emit({"type": "text", "text": "before"})
        backlog, queue = run.attach(0)
        assert queue is not None
        run.emit({"type": "text", "text": "after"})
        assert [f["text"] for f in backlog] == ["before"]
        assert (await queue.get())["text"] == "after"

    async def test_two_viewers_see_the_same_frames(self) -> None:
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        _, first = run.attach(0)
        _, second = run.attach(0)
        assert first is not None and second is not None
        run.emit({"type": "text", "text": "shared"})
        assert (await first.get())["text"] == "shared"
        assert (await second.get())["text"] == "shared"

    async def test_finishing_releases_every_tailing_viewer(self) -> None:
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        _, queue = run.attach(0)
        assert queue is not None
        run.finish()
        assert await asyncio.wait_for(queue.get(), timeout=1) is RUN_END

    async def test_a_viewer_that_stops_reading_is_dropped_never_blocks_the_run(
        self, monkeypatch
    ) -> None:
        # The agent must never stall on a browser that has stopped consuming;
        # that client reconnects and replays from its cursor, which is exactly
        # what the cursor is for.
        monkeypatch.setattr(runs_mod, "VIEWER_QUEUE_MAX", 2)
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        _, queue = run.attach(0)
        assert queue is not None
        for i in range(10):
            run.emit({"type": "text", "text": str(i)})
        assert run.viewer_count == 0
        # …and the run kept every frame for the replay that follows.
        backlog, _ = run.attach(0)
        assert len(backlog) == 10

    def test_detach_leaves_the_run_running(self) -> None:
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        _, queue = run.attach(0)
        assert queue is not None
        run.detach(queue)
        assert run.viewer_count == 0
        assert run.done is False  # the whole point: a viewer leaving is not the end


class TestSnapshot:
    def test_snapshot_carries_what_a_client_needs_to_attach(self) -> None:
        run = AgentRun("chat", "alpha", "t1", "turn-1")
        run.emit({"type": "start"})
        run.emit({"type": "text", "text": "hi"})
        snap = run.snapshot()
        assert snap["run_id"] == run.run_id
        assert snap["turn_id"] == "turn-1"
        assert snap["cursor"] == 2  # attach from here and miss nothing
        assert snap["running"] is True
        run.finish()
        assert run.snapshot()["running"] is False


class _AlwaysConnected:
    async def is_disconnected(self) -> bool:
        return False


class TestDroppedViewerEndsItsResponse:
    """A viewer `emit` dropped (its queue filled while a phone was suspended)
    used to get keepalives forever: the client's stream never ended, so it never
    rejoined from its cursor and the conversation stayed stale."""

    async def test_dropped_viewer_drains_then_ends(self, monkeypatch) -> None:
        monkeypatch.setattr(runs_mod, "VIEWER_QUEUE_MAX", 2)
        run = AgentRun("chat", "a", "t", "x")
        view = runs_mod.sse_run_view(run, 0, _AlwaysConnected())
        # Prime the generator so it registers its queue, then overflow it.
        run.emit({"type": "text", "text": "a"})
        first = await asyncio.wait_for(view.__anext__(), timeout=1)
        assert '"a"' in first
        for i in range(5):
            run.emit({"type": "text", "text": str(i)})
        chunks: list[str] = []

        async def drain() -> None:
            async for chunk in view:
                chunks.append(chunk)

        await asyncio.wait_for(drain(), timeout=2)  # ends, no keepalive loop
        assert not any("keepalive" in c for c in chunks)
        assert not run.done  # the run itself keeps going

    async def test_finished_run_with_full_queue_still_ends(self, monkeypatch) -> None:
        monkeypatch.setattr(runs_mod, "VIEWER_QUEUE_MAX", 1)
        run = AgentRun("chat", "a", "t", "x")
        view = runs_mod.sse_run_view(run, 0, _AlwaysConnected())
        run.emit({"type": "text", "text": "a"})
        await asyncio.wait_for(view.__anext__(), timeout=1)
        run.emit({"type": "done"})
        run.finish()  # RUN_END may not fit; the view must still end

        async def drain() -> None:
            async for _ in view:
                pass

        await asyncio.wait_for(drain(), timeout=2)
