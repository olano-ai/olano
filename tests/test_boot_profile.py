"""Tests for the per-agent boot profiler (``olano/core/boot_profile.py``)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from olano.config.agent_config import AgentConfig
from olano.core import boot_profile as bp


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty profile ring."""
    with bp._PROFILES_LOCK:
        bp._PROFILES.clear()
    yield
    with bp._PROFILES_LOCK:
        bp._PROFILES.clear()


def _labels(profile: bp.BootProfile) -> list[tuple[int, str]]:
    return [(int(r["depth"]), str(r["label"])) for r in profile.rows()]


def test_phase_outside_profile_is_a_noop() -> None:
    """The builder is re-entered by /model switches and cognition workers — those
    must not record anything."""
    with bp.phase("build"):
        pass
    assert bp.all_profiles() == []


def test_nested_phases_render_in_entry_order() -> None:
    with bp.profile_boot("alpha") as prof:
        with bp.phase("build"):
            with bp.phase("mcp"):
                pass
            with bp.phase("rest"):
                with bp.phase("extra_tools"):
                    pass
        with bp.phase("scheduler"):
            pass

    labels = _labels(prof)
    assert labels[:2] == [(0, "build"), (1, "mcp")]
    assert (1, "rest") in labels
    assert (2, "extra_tools") in labels
    assert labels[-1] == (0, "scheduler")
    assert prof.total is not None and prof.total >= 0


def test_unattributed_remainder_is_reported_after_the_subtree() -> None:
    """The gap between a parent and its children is the whole point — that is
    where an uninstrumented call or thread/GIL contention hides."""
    prof = bp.BootProfile("beta")
    parent = prof.open("build", 0)
    child = prof.open("mcp", 1)
    grandchild = prof.open("connect x", 2)
    assert parent and child and grandchild
    grandchild.seconds = 0.4
    child.seconds = 0.5
    parent.seconds = 2.0
    prof.finish()

    labels = _labels(prof)
    # The parent's 1.5s remainder lands after its whole subtree, as a child row.
    assert labels == [
        (0, "build"),
        (1, "mcp"),
        (2, "connect x"),
        (2, "(unattributed)"),
        (1, "(unattributed)"),
    ]
    rows = {(int(r["depth"]), r["label"]): float(r["seconds"]) for r in prof.rows()}
    assert rows[(1, "(unattributed)")] == pytest.approx(1.5)
    assert rows[(2, "(unattributed)")] == pytest.approx(0.1)


def test_root_remainder_catches_uninstrumented_top_level_work() -> None:
    """A stall in code nobody wrapped must still be visible, not vanish between
    the top-level rows."""
    prof = bp.BootProfile("beta2")
    rec = prof.open("build", 0)
    assert rec
    rec.seconds = 1.0
    prof.finish()
    prof.total = 5.0  # 4s spent outside any instrumented phase

    rows = {(int(r["depth"]), r["label"]): float(r["seconds"]) for r in prof.rows()}
    assert rows[(0, "(unattributed)")] == pytest.approx(4.0)
    assert _labels(prof)[-1] == (0, "(unattributed)")


def test_no_root_remainder_when_phases_account_for_the_boot() -> None:
    with bp.profile_boot("beta3") as prof:
        with bp.phase("build"):
            time.sleep(0.02)
    # `build` covers essentially the whole boot; no spurious root remainder.
    assert all(label != "(unattributed)" for _d, label in _labels(prof))


def test_no_remainder_row_for_a_leaf() -> None:
    prof = bp.BootProfile("gamma")
    rec = prof.open("config", 0)
    assert rec
    rec.seconds = 1.0
    prof.finish()
    assert _labels(prof) == [(0, "config")]


def test_record_adds_a_leaf_phase_in_place() -> None:
    with bp.profile_boot("delta") as prof:
        with bp.phase("build"):
            pass
        bp.record("channels (2)", 3.25)

    labels = _labels(prof)
    assert labels[-1] == (0, "channels (2)")
    seconds = {r["label"]: float(r["seconds"]) for r in prof.rows()}
    assert seconds["channels (2)"] == pytest.approx(3.25)


def test_record_outside_profile_is_a_noop() -> None:
    bp.record("channels", 1.0)
    assert bp.all_profiles() == []


async def test_context_propagates_into_to_thread() -> None:
    """``asyncio.to_thread`` copies the context, so a phase entered in the worker
    must nest under the phase that awaited it — this is how the build's blocking
    half gets attributed at all."""

    def _work() -> None:
        with bp.phase("extra_tools"):
            pass

    with bp.profile_boot("epsilon") as prof:
        with bp.phase("rest"):
            await asyncio.to_thread(_work)

    assert _labels(prof)[:2] == [(0, "rest"), (1, "extra_tools")]


def test_detached_thread_is_not_billed_to_the_boot() -> None:
    """A bare Thread does NOT copy context — the background RAG ingest must stay
    unattributed rather than land on whichever agent spawned it."""
    done = threading.Event()

    def _detached() -> None:
        with bp.phase("rag_ingest"):
            pass
        done.set()

    with bp.profile_boot("zeta") as prof:
        with bp.phase("build"):
            t = threading.Thread(target=_detached)
            t.start()
            t.join()
    assert done.is_set()
    assert all(label != "rag_ingest" for _depth, label in _labels(prof))


def test_phase_records_even_when_the_body_raises() -> None:
    with pytest.raises(RuntimeError), bp.profile_boot("eta"):
        with bp.phase("build"):
            raise RuntimeError("boom")

    prof = bp.get_profile("eta")
    assert prof is not None
    assert [label for _d, label in _labels(prof)] == ["build"]


def test_nested_profile_boot_reuses_the_outer_tree() -> None:
    """A boot triggered inside a boot must not start a competing profile."""
    with bp.profile_boot("theta") as outer:
        with bp.profile_boot("theta") as inner:
            with bp.phase("build"):
                pass
        assert inner is outer
    assert [label for _d, label in _labels(outer)] == ["build"]
    assert len(bp.all_profiles()) == 1


def test_profiles_are_retained_per_agent_and_bounded() -> None:
    for i in range(bp._MAX_PROFILES + 5):
        with bp.profile_boot(f"agent-{i}"):
            pass
    assert len(bp.all_profiles()) == bp._MAX_PROFILES
    # Oldest evicted, newest kept.
    assert bp.get_profile("agent-0") is None
    assert bp.get_profile(f"agent-{bp._MAX_PROFILES + 4}") is not None


def test_reboot_replaces_the_previous_profile_for_that_agent() -> None:
    with bp.profile_boot("iota"):
        with bp.phase("first"):
            pass
    with bp.profile_boot("iota"):
        with bp.phase("second"):
            pass
    prof = bp.get_profile("iota")
    assert prof is not None
    assert [label for _d, label in _labels(prof)] == ["second"]
    assert len(bp.all_profiles()) == 1


def test_report_logging_is_env_gated(monkeypatch, caplog) -> None:
    monkeypatch.delenv("LIONCLAW_BOOT_PROFILE", raising=False)
    with caplog.at_level("INFO", logger="olano.core.boot_profile"):
        with bp.profile_boot("kappa"):
            with bp.phase("build"):
                pass
    assert "boot profile" not in caplog.text

    caplog.clear()
    monkeypatch.setenv("LIONCLAW_BOOT_PROFILE", "1")
    with caplog.at_level("INFO", logger="olano.core.boot_profile"):
        with bp.profile_boot("lambda"):
            with bp.phase("build"):
                pass
    assert "boot profile 'lambda'" in caplog.text


def test_fleet_summary_names_the_slowest_agents() -> None:
    for name, secs in (("fast", 1.0), ("slow", 100.0), ("mid", 10.0)):
        prof = bp.BootProfile(name)
        prof.finish()
        prof.total = secs
        with bp._PROFILES_LOCK:
            bp._PROFILES[name] = prof

    summary = bp.fleet_summary()
    assert "111.0s across 3 agents" in summary
    assert summary.index("slow 100.0s") < summary.index("mid 10.0s") < summary.index("fast 1.0s")


def test_fleet_summary_empty_without_profiles() -> None:
    assert bp.fleet_summary() == ""


async def test_real_build_is_attributed_to_its_phases(base_config: AgentConfig) -> None:
    """End to end against the real builder: the phases that matter for diagnosing
    a slow boot must actually show up, at the right depth, with the slow one
    identifiable. ``create_deep_agent``/``_build_model`` are patched (per
    CLAUDE.md) so this stays a timing test, not a network test."""

    def slow_tools(*_a: Any, **_k: Any) -> list[Any]:
        time.sleep(0.2)
        return []

    from olano.core.builder import build_agent_async

    with (
        patch("deepagents.create_deep_agent", return_value=MagicMock()),
        patch("olano.core.builder._build_model", return_value=MagicMock()),
        patch("olano.core.builder._build_extra_tools", side_effect=slow_tools),
        bp.profile_boot("nu") as prof,
    ):
        await build_agent_async(base_config)

    by_label = {str(r["label"]): r for r in prof.rows()}
    # The three top-level build phases, plus the sub-phase breakdown of `rest`
    # that localises where a 168s stall would be.
    for label in ("prelude", "model", "rest", "graph", "extra_tools", "system_prompt"):
        assert label in by_label, f"{label} missing from {sorted(by_label)}"
    assert by_label["rest"]["depth"] == 0
    assert by_label["extra_tools"]["depth"] == 1
    # The planted 0.2s cost is attributed to extra_tools, and rolls up into rest.
    assert float(by_label["extra_tools"]["seconds"]) >= 0.2
    assert float(by_label["rest"]["seconds"]) >= float(by_label["extra_tools"]["seconds"])


def test_to_dict_is_json_shaped() -> None:
    with bp.profile_boot("mu") as prof:
        with bp.phase("build"):
            with bp.phase("mcp"):
                pass

    payload = prof.to_dict()
    assert payload["agent"] == "mu"
    assert isinstance(payload["total_seconds"], float)
    assert {p["label"] for p in payload["phases"]} >= {"build", "mcp"}
    assert all(isinstance(p["seconds"], float) for p in payload["phases"])
