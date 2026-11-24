"""Tests for the tiered FAST/DEEP cognition router."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from olano.cognition.router import (
    TaskSpec,
    Tier,
    _counter_path,
    run_tiered,
)
from olano.cognition.worker import WorkerOutcome
from olano.config.agent_config import AgentConfig
from olano.config.system_settings import set_cortex_master

# Tier routing only happens with the Cortex engine on, and it is off by default.
# Without this every DEEP request below would go FAST before reaching the
# budget, cap or failure path its test is about, and pass for the wrong reason
# (the engine-off case has its own test).
pytestmark = pytest.mark.usefixtures("cortex_on")


def _deep_spec() -> TaskSpec:
    return TaskSpec("memory_garden", Tier.DEEP)


async def _fast() -> str:
    return "fast-result"


async def test_engine_off_never_runs_deep(base_config: AgentConfig, tmp_path) -> None:
    set_cortex_master(False)
    with patch("olano.cognition.router.run_worker", new=AsyncMock()) as rw:
        tier, result = await run_tiered(
            _deep_spec(),
            parent_config=base_config,
            workspace_root=tmp_path,
            cognition_cfg=base_config.cognition,
            fast_fn=_fast,
            deep_playbook="pb",
            deep_goal="goal",
        )
    assert tier == Tier.FAST
    assert result == "fast-result"
    rw.assert_not_awaited()


async def test_fast_default_runs_fast(base_config: AgentConfig, tmp_path) -> None:
    tier, result = await run_tiered(
        TaskSpec("fact_distill", Tier.FAST),
        parent_config=base_config,
        workspace_root=tmp_path,
        cognition_cfg=base_config.cognition,
        fast_fn=_fast,
        deep_playbook="pb",
        deep_goal="goal",
    )
    assert tier == Tier.FAST
    assert result == "fast-result"


async def test_deep_runs_worker(base_config: AgentConfig, tmp_path) -> None:
    outcome = WorkerOutcome(status="completed", summary="did it", artifacts=["MEMORY.md"])
    with patch("olano.cognition.router.run_worker", new=AsyncMock(return_value=outcome)) as rw:
        tier, result = await run_tiered(
            _deep_spec(),
            parent_config=base_config,
            workspace_root=tmp_path,
            cognition_cfg=base_config.cognition,
            fast_fn=_fast,
            deep_playbook="pb",
            deep_goal="goal",
            deep_tools=["memory_read"],
        )
    assert tier == Tier.DEEP
    assert result.summary == "did it"
    rw.assert_awaited_once()
    # Deep counter incremented.
    data = json.loads(_counter_path(tmp_path).read_text())
    assert data[date.today().isoformat()] == 1


async def test_force_tier_fast_skips_deep(base_config: AgentConfig, tmp_path) -> None:
    base_config.cognition.force_tier = "fast"
    with patch("olano.cognition.router.run_worker", new=AsyncMock()) as rw:
        tier, result = await run_tiered(
            _deep_spec(),
            parent_config=base_config,
            workspace_root=tmp_path,
            cognition_cfg=base_config.cognition,
            fast_fn=_fast,
            deep_playbook="pb",
            deep_goal="goal",
        )
    assert tier == Tier.FAST
    assert result == "fast-result"
    rw.assert_not_awaited()


async def test_budget_blocked_falls_back_to_fast(base_config: AgentConfig, tmp_path) -> None:
    with (
        patch("olano.cognition.router.budget_gate", return_value=(False, "over")),
        patch("olano.cognition.router.run_worker", new=AsyncMock()) as rw,
    ):
        tier, result = await run_tiered(
            _deep_spec(),
            parent_config=base_config,
            workspace_root=tmp_path,
            cognition_cfg=base_config.cognition,
            fast_fn=_fast,
            deep_playbook="pb",
            deep_goal="goal",
        )
    assert tier == Tier.FAST
    assert result == "fast-result"
    rw.assert_not_awaited()


async def test_daily_cap_falls_back_to_fast(base_config: AgentConfig, tmp_path) -> None:
    base_config.cognition.max_deep_runs_per_day = 2
    counter = _counter_path(tmp_path)
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text(json.dumps({date.today().isoformat(): 2}))
    with patch("olano.cognition.router.run_worker", new=AsyncMock()) as rw:
        tier, result = await run_tiered(
            _deep_spec(),
            parent_config=base_config,
            workspace_root=tmp_path,
            cognition_cfg=base_config.cognition,
            fast_fn=_fast,
            deep_playbook="pb",
            deep_goal="goal",
        )
    assert tier == Tier.FAST
    rw.assert_not_awaited()


async def test_deep_worker_failure_falls_back_to_fast(base_config: AgentConfig, tmp_path) -> None:
    outcome = WorkerOutcome(status="timeout", error="too slow")
    with patch("olano.cognition.router.run_worker", new=AsyncMock(return_value=outcome)):
        tier, result = await run_tiered(
            _deep_spec(),
            parent_config=base_config,
            workspace_root=tmp_path,
            cognition_cfg=base_config.cognition,
            fast_fn=_fast,
            deep_playbook="pb",
            deep_goal="goal",
        )
    assert tier == Tier.FAST
    assert result == "fast-result"
