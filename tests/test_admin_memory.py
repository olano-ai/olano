"""Tests for olano.admin.memory — the memory report, the monitor toggle, and
the three leak-fix regressions (bounded tool-arg registry, LRU hook contexts,
pruned self-learning offsets).

No live runtime is bound in the test process; an autouse fixture pins the
offline path, and individual tests fake an in-process runtime to exercise the
live report + toggle.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from olano import admin
from olano.admin import memory as M


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No in-process runtime, no daemon API, no daemon pid → deterministic tier."""
    monkeypatch.setattr(M, "_get_runtime", lambda: None)
    monkeypatch.setattr(M, "_api_get_json", lambda *a, **k: None)
    monkeypatch.setattr(M, "_daemon_pid", lambda: None)


def _fake_runtime(monitor: M.MemoryMonitor | None = None) -> SimpleNamespace:
    cfg = SimpleNamespace(name="merli", tools=["a", "b"], mcp_servers=[])
    runner = SimpleNamespace(
        config=cfg,
        _hook_contexts={"t1": {}, "t2": {}},
        _override_agents={},
        _post_turn_pulse_tasks={},
        _boot_rss_mb=42.0,
    )
    svc = SimpleNamespace(
        config=cfg,
        _processed_offsets={"/a.jsonl": 10, "/b.jsonl": 3},
        _distillation_count=2,
        _failures={"distill": 1},
        rag_memory=SimpleNamespace(_stores={"col": object()}),
    )
    return SimpleNamespace(
        _runners={"merli": runner},
        _self_learning=[svc],
        _background_tasks=[1, 2],
        _blocked_agents={},
        scheduler=None,
        _memory_monitor=monitor or M.MemoryMonitor(),
    )


# ── report: offline (no daemon) ───────────────────────────────────────────────


def test_memory_report_offline_is_process_only() -> None:
    r = M.memory_report(include_gc=True, include_tracemalloc=True)
    assert r["source"] == "none"
    assert r["running"] is False
    assert r["structures"] is None
    assert "process" in r
    if sys.platform.startswith("linux"):
        assert isinstance(r["process"]["rss_mb"], float)
        assert r["process"]["threads"] is not None


def test_render_report_never_crashes_on_either_tier() -> None:
    off = M.memory_report()
    assert "Memory" in M.render_memory_report_text(off)


# ── report: in-process (fake runtime) ─────────────────────────────────────────


def test_memory_report_inprocess_breakdown(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _fake_runtime()
    monkeypatch.setattr(M, "_get_runtime", lambda: rt)
    r = M.memory_report(include_gc=True, include_tracemalloc=False)
    assert r["source"] == "runtime"
    s = r["structures"]
    assert s["hook_contexts_total"] == 2
    assert s["processed_offsets_total"] == 2
    assert s["runners_count"] == 1
    assert s["per_agent"]["merli"]["config_tools"] == 2
    assert s["per_agent"]["merli"]["boot_rss_mb"] == 42.0
    # RAG collections now map agent → {collection: vector_count} (None for the
    # fake store) plus a vectors_total.
    assert s["rag"]["collections"] == {"merli": {"col": None}}
    assert "vectors_total" in s["rag"]
    # Child-process breakdown is present (this process has no children in the
    # test, so the list may be empty — but the shape is stable).
    assert "children" in r and "processes" in r["children"]
    assert r["gc"]["objects_total"] > 0
    assert r["monitor"]["enabled"] is False
    # renders without error
    assert "Structures" in M.render_memory_report_text(r)


def test_glibc_allocator_counters_are_read_without_touching_the_heap() -> None:
    """The report tells allocator-retained memory apart from native and
    traced allocations: on glibc every counter is a number, elsewhere the
    fields are null, and asking never allocates, frees or trims anything."""
    import sys

    before = M.current_rss_mb()
    out = M.glibc_allocator()
    assert set(out) == {
        "allocated_mb",
        "free_mb",
        "top_releasable_mb",
        "mmapped_mb",
        "mmapped_regions",
        "tracemalloc_overhead_mb",
    }
    if sys.platform.startswith("linux") and out["allocated_mb"] is not None:
        assert out["allocated_mb"] > 0
        assert out["free_mb"] >= 0 and out["top_releasable_mb"] >= 0
        assert isinstance(out["mmapped_regions"], int)
    else:
        assert out["allocated_mb"] is None
    after = M.current_rss_mb()
    if before is not None and after is not None:
        assert after >= before - 1.0  # reading counters released nothing


def test_current_rss_mb_and_children_helpers() -> None:
    if sys.platform.startswith("linux"):
        assert isinstance(M.current_rss_mb(), float)
    kids = M._child_processes(os.getpid())
    assert set(kids) >= {"supported", "total_rss_mb", "processes"}
    assert isinstance(kids["processes"], list)


# ── monitor toggle ────────────────────────────────────────────────────────────


def test_apply_monitor_action_on_off_frees_tracemalloc() -> None:
    import tracemalloc

    mon = M.MemoryMonitor()
    rt = SimpleNamespace(_memory_monitor=mon)

    on = M.apply_monitor_action(rt, "on")
    assert on["enabled"] is True and on["interval_seconds"] == M._DEFAULT_SAMPLE_SECONDS

    M.apply_monitor_action(rt, "tracemalloc-on")
    assert tracemalloc.is_tracing()

    off = M.apply_monitor_action(rt, "off")
    assert off["enabled"] is False
    assert not tracemalloc.is_tracing()  # off also stops tracemalloc


def test_set_memory_monitor_inprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    mon = M.MemoryMonitor()
    monkeypatch.setattr(M, "_get_runtime", lambda: SimpleNamespace(_memory_monitor=mon))
    out = M.set_memory_monitor("on")
    assert out["enabled"] is True


def test_set_memory_monitor_no_runtime_is_graceful() -> None:
    # _force_offline pins no runtime + no daemon → POST returns None → error dict.
    out = M.set_memory_monitor("on")
    assert out.get("enabled") is None
    assert "error" in out


def test_monitor_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIONCLAW_MEM_SAMPLE_SECONDS", "120")
    monkeypatch.delenv("LIONCLAW_TRACEMALLOC", raising=False)
    mon = M.monitor_from_env()
    assert mon.enabled is True and mon.interval_seconds == 120


# ── sampler line ──────────────────────────────────────────────────────────────


def test_sample_memory_line_stashes_on_monitor() -> None:
    import logging

    rt = _fake_runtime()
    line = M.sample_memory_line(rt, logging.getLogger("test"))
    assert line["runners"] == 1 and line["hooks"] == 2
    assert rt._memory_monitor._last_sample is not None


# ── leak fix #1: bounded _TOOL_ARGS_REGISTRY ──────────────────────────────────


def test_tool_args_registry_is_bounded() -> None:
    import olano.security.deepagents_middleware as MW

    MW._TOOL_ARGS_REGISTRY.clear()
    for i in range(MW._TOOL_ARGS_REGISTRY_MAX + 250):
        MW._record_tool_args(f"id{i}", "x", {"n": i})
    assert len(MW._TOOL_ARGS_REGISTRY) == MW._TOOL_ARGS_REGISTRY_MAX
    assert "id0" not in MW._TOOL_ARGS_REGISTRY  # oldest evicted
    last = f"id{MW._TOOL_ARGS_REGISTRY_MAX + 249}"
    assert last in MW._TOOL_ARGS_REGISTRY  # newest kept
    MW._TOOL_ARGS_REGISTRY.clear()


# ── leak fix #2: LRU-bounded _hook_contexts ───────────────────────────────────


def test_hook_contexts_are_lru_bounded(base_config, mock_agent) -> None:
    from olano.core.runner import _HOOK_CONTEXTS_MAX, AgentRunner

    runner = AgentRunner(mock_agent, base_config)
    for i in range(_HOOK_CONTEXTS_MAX + 100):
        ctx = runner._hook_context(f"thread-{i}")
        assert ctx["thread_id"] == f"thread-{i}"
    assert len(runner._hook_contexts) == _HOOK_CONTEXTS_MAX
    # oldest evicted, newest retained (deterministic rebuild on re-access)
    assert "thread-0" not in runner._hook_contexts
    assert f"thread-{_HOOK_CONTEXTS_MAX + 99}" in runner._hook_contexts


# ── leak fix #3: streaming read + pruned _processed_offsets ────────────────────


def test_read_new_lines_streams_tail(tmp_path) -> None:
    from olano.memory.self_learning import _read_new_lines

    p = tmp_path / "s.jsonl"
    p.write_text("l0\nl1\nl2\nl3\n", encoding="utf-8")
    # The offset is a byte position the read seeks to (6 == past "l0\nl1\n"),
    # and the returned offset is the new byte position — not a line count.
    new, end = _read_new_lines(p, 6)
    assert new == ["l2", "l3"] and end == p.stat().st_size


async def test_processed_offsets_pruned_on_distill(tmp_workspace) -> None:
    import json

    from olano.config.home import get_olano_paths
    from olano.memory.self_learning import SelfLearningService

    cfg = SimpleNamespace(name="prune-agent")
    svc = SelfLearningService(
        agent_config=cfg,  # type: ignore[arg-type]
        workspace_root=tmp_workspace,
        markdown_memory=MagicMock(),
    )
    sdir = get_olano_paths().sessions_dir / "prune-agent"
    sdir.mkdir(parents=True, exist_ok=True)
    real = sdir / "live.jsonl"
    real.write_text(json.dumps({"role": "user", "content": "hi"}) + "\n", encoding="utf-8")

    # Seed a stale offset for a file that no longer exists + a baseline for the
    # live file. <4 new turns means the cycle commits offsets and returns before
    # any deep-run gate, exercising just the prune + streaming read.
    svc._processed_offsets = {str(sdir / "gone.jsonl"): 99, str(real): 0}
    await svc._distill_from_sessions()

    assert str(sdir / "gone.jsonl") not in svc._processed_offsets  # pruned
    assert svc._processed_offsets[str(real)] == 1  # committed via streaming read


# ── CLI: olano mem ─────────────────────────────────────────────────────────


def test_cli_mem_json() -> None:
    from click.testing import CliRunner

    from olano.cli.main import cli

    res = CliRunner().invoke(cli, ["mem", "--json", "--no-gc", "--no-tracemalloc"])
    assert res.exit_code == 0, res.output
    assert '"source"' in res.output


def test_admin_reexports_memory() -> None:
    assert hasattr(admin, "memory_report")
    assert hasattr(admin, "set_memory_monitor")
