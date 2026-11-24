"""Tests for progress delivery (olano/platforms/progress_reporter.py).

The pump coalesces events into throttled pushes, and how the view ends
depends on verbosity: ``normal`` clears it so the answer stands alone,
``verbose`` keeps the settled log.
"""

from __future__ import annotations

import asyncio
import json

from olano.platforms.progress_reporter import (
    MAX_APPEND_MESSAGES,
    MAX_CONSECUTIVE_FAILURES,
    ProgressReporter,
    make_platform_reporter,
    resolve_progress_mode,
)
from olano.platforms.verbosity import PLAIN

# Fast enough that a test never waits on the real 2s/8s intervals.
TICK = 0.02


class FakeSink:
    def __init__(self, *, edits: bool = True, fail: bool = False) -> None:
        self.edits = edits
        self.fail = fail
        self.pushes: list[str] = []
        self.cleared = 0
        self.released = 0

    async def push(self, text: str) -> bool:
        self.pushes.append(text)
        return not self.fail

    async def clear(self) -> None:
        self.cleared += 1

    async def release(self) -> None:
        self.released += 1


def _reporter(sink, verbosity: str = "normal") -> ProgressReporter:
    return ProgressReporter(sink, verbosity=verbosity, dialect=PLAIN, interval=TICK)


def _call(name: str, args: dict, call_id: str = "c1") -> tuple[str, dict]:
    return ("tool_call", {"id": call_id, "name": name, "args": json.dumps(args)})


async def _settle(times: int = 4) -> None:
    """Let the pump run a few cycles."""
    for _ in range(times):
        await asyncio.sleep(TICK)


# ── enablement ──────────────────────────────────────────────────────────────


async def test_quiet_disables_the_reporter():
    sink = FakeSink()
    reporter = ProgressReporter(sink, verbosity="quiet", dialect=PLAIN, interval=TICK)
    assert reporter.enabled is False
    reporter.start()
    reporter.feed(*_call("read_file", {"file_path": "a.py"}))
    await _settle()
    await reporter.finish()
    assert sink.pushes == []
    assert sink.cleared == 0


async def test_missing_sink_is_a_no_op():
    reporter = ProgressReporter(None)
    assert reporter.enabled is False
    reporter.start()
    reporter.feed(*_call("read_file", {"file_path": "a.py"}))
    await reporter.finish()
    assert reporter.delivered is False


async def test_text_only_turn_pushes_nothing():
    sink = FakeSink()
    reporter = _reporter(sink)
    reporter.start()
    reporter.feed("text", "hello there")
    await _settle()
    await reporter.finish()
    assert sink.pushes == []
    assert sink.cleared == 0


# ── pump behaviour ──────────────────────────────────────────────────────────


async def test_first_update_is_pushed_and_reflects_the_tool_call():
    sink = FakeSink()
    reporter = _reporter(sink)
    reporter.start()
    reporter.feed(*_call("web_search", {"query": "cinema showtimes"}))
    await _settle()
    assert sink.pushes
    assert "cinema showtimes" in sink.pushes[0]
    assert reporter.delivered is True
    await reporter.finish()


async def test_identical_renders_are_not_re_pushed():
    sink = FakeSink()
    reporter = _reporter(sink)
    reporter.start()
    reporter.feed(*_call("read_file", {"file_path": "a.py"}))
    await _settle()
    before = len(sink.pushes)
    # A subagent_text event for an already-seen peer bumps nothing visible.
    reporter.feed("subagent_text", {"agents": ["kevin"]})
    reporter.feed("subagent_text", {"agents": ["kevin"]})
    await _settle()
    # The first one does change the render (new peer), the second must not.
    assert len(sink.pushes) <= before + 1
    await reporter.finish()


async def test_burst_of_events_coalesces_into_fewer_pushes():
    sink = FakeSink()
    reporter = ProgressReporter(sink, verbosity="normal", dialect=PLAIN, interval=10.0)
    reporter.start()
    for i in range(20):
        reporter.feed(*_call("read_file", {"file_path": f"f{i}.py"}, call_id=f"c{i}"))
    await asyncio.sleep(TICK)
    # One push covers the whole burst — the pump is sleeping out its interval.
    assert len(sink.pushes) == 1
    await reporter.finish()


async def test_delivery_failures_retire_the_reporter():
    sink = FakeSink(fail=True)
    reporter = _reporter(sink)
    reporter.start()
    for i in range(MAX_CONSECUTIVE_FAILURES + 5):
        reporter.feed(*_call("read_file", {"file_path": f"f{i}.py"}, call_id=f"c{i}"))
        await _settle(2)
    assert len(sink.pushes) <= MAX_CONSECUTIVE_FAILURES
    assert reporter.delivered is False
    await reporter.finish()


async def test_append_mode_is_capped():
    sink = FakeSink(edits=False)
    reporter = _reporter(sink)
    reporter.start()
    for i in range(MAX_APPEND_MESSAGES + 10):
        reporter.feed(*_call("read_file", {"file_path": f"f{i}.py"}, call_id=f"c{i}"))
        await _settle(2)
    assert len(sink.pushes) <= MAX_APPEND_MESSAGES
    await reporter.finish()


async def test_feed_never_raises_on_garbage():
    sink = FakeSink()
    reporter = _reporter(sink)
    reporter.start()
    reporter.feed("tool_call", None)
    reporter.feed("tool_result", "not-a-dict")
    reporter.feed("nonsense", object())
    await _settle()
    await reporter.finish()


# ── hand-off ────────────────────────────────────────────────────────────────


async def test_progress_view_vanishes_at_finish_in_every_mode():
    # The openclaw-style hand-off: the tools message disappears and the
    # answer arrives as a fresh message — in verbose just like normal.
    for mode in ("normal", "verbose"):
        sink = FakeSink()
        reporter = _reporter(sink, mode)
        reporter.start()
        reporter.feed(*_call("read_file", {"file_path": "a.py"}))
        await _settle()
        await reporter.finish()
        assert sink.cleared == 1, mode
        assert sink.released == 0, mode


async def test_keep_log_keeps_the_settled_view_instead_of_clearing():
    sink = FakeSink()
    reporter = ProgressReporter(
        sink, verbosity="verbose", dialect=PLAIN, interval=TICK, keep_log=True
    )
    reporter.start()
    reporter.feed(
        "tool_call",
        {
            "id": "t",
            "name": "write_todos",
            "args": json.dumps({"todos": [{"content": "Ship it", "status": "in_progress"}]}),
        },
    )
    reporter.feed(*_call("read_file", {"file_path": "a.py"}))
    await _settle()
    await reporter.finish()
    assert sink.cleared == 0
    final = sink.pushes[-1]
    # Settled: no "Working…" header, and the dangling todo reads as done.
    assert "🧭" not in final
    assert "✅ Ship it" in final
    # The kept log is handed back, so the adapter's end-of-turn orphan sweep
    # does not delete the very thing keep_log exists to leave behind.
    assert sink.released == 1


async def test_keep_log_never_clears_even_when_delivery_failed():
    # keep_log releases rather than clears, whatever happened to the pushes —
    # release is a no-op when the adapter tracks no message, and gating it on
    # "known delivered" would let a push raced by cancellation get deleted.
    sink = FakeSink(fail=True)
    reporter = ProgressReporter(
        sink, verbosity="verbose", dialect=PLAIN, interval=TICK, keep_log=True
    )
    reporter.start()
    reporter.feed(*_call("read_file", {"file_path": "a.py"}))
    await _settle()
    await reporter.finish()
    assert sink.cleared == 0
    assert sink.released == 1


async def test_finish_is_safe_before_start():
    sink = FakeSink()
    reporter = _reporter(sink)
    await reporter.finish()
    assert sink.pushes == []


async def test_disabled_reporter_never_touches_the_sink_at_finish():
    sink = FakeSink()
    reporter = ProgressReporter(sink, verbosity="quiet", dialect=PLAIN, interval=TICK)
    await reporter.finish()
    assert sink.cleared == 0
    assert sink.released == 0


# ── mode resolution ─────────────────────────────────────────────────────────


class FakeAdapter:
    PROGRESS_MODE = "edit"

    def __init__(self, extra=None):
        self.config = type("Cfg", (), {"extra": extra or {}})()


def test_mode_defaults_to_the_adapter_setting():
    assert resolve_progress_mode(FakeAdapter()) == "edit"


def test_platform_config_can_disable_progress():
    assert resolve_progress_mode(FakeAdapter({"progress_updates": False})) == "off"
    assert resolve_progress_mode(FakeAdapter({"progress_updates": "off"})) == "off"


def test_platform_config_can_force_append():
    assert resolve_progress_mode(FakeAdapter({"progress_updates": "append"})) == "append"


def test_env_overrides_platform_config(monkeypatch):
    monkeypatch.setenv("LIONCLAW_PROGRESS_UPDATES", "off")
    assert resolve_progress_mode(FakeAdapter({"progress_updates": "edit"})) == "off"


def test_on_never_forces_progress_onto_an_opted_out_surface(monkeypatch):
    class QuietAdapter(FakeAdapter):
        PROGRESS_MODE = "off"

    monkeypatch.setenv("LIONCLAW_PROGRESS_UPDATES", "on")
    assert resolve_progress_mode(QuietAdapter()) == "off"


def test_disabled_adapter_yields_a_disabled_reporter():
    class QuietAdapter(FakeAdapter):
        PROGRESS_MODE = "off"

    reporter = make_platform_reporter(QuietAdapter(), object(), verbosity="normal", dialect=PLAIN)
    assert reporter.enabled is False
