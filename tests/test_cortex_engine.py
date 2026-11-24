"""Tests for the autonomous CortexEngine orchestrator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from olano.cognition import orchestrator as eng
from olano.cognition.worker import WorkerOutcome
from olano.config.agent_config import AgentConfig
from olano.config.system_settings import FEATURES, INTENSITIES

# ──────────────────────────────── registry ──────────────────────────────────


def test_phase_registry_is_coherent() -> None:
    assert eng.PHASES, "expected a non-empty phase registry"
    ids = {p.id for p in eng.PHASES}
    # Think funnel is a subset of all phases.
    assert set(eng.THINK_CYCLE_ORDER) <= ids
    for p in eng.PHASES:
        assert p.feature in FEATURES, f"{p.id} → unknown feature {p.feature}"
        assert p.policy in ("auto", "guarded", "readonly")
        # Every intensity has a cadence.
        for intensity in INTENSITIES:
            assert eng.cadence_for(p, intensity), f"{p.id} missing cadence for {intensity}"


def test_deep_cap_scales_with_intensity() -> None:
    assert eng.deep_cap("relaxed") < eng.deep_cap("balanced") < eng.deep_cap("aggressive")
    # Unknown intensity falls back to the default (relaxed) cap, never the busiest.
    assert eng.deep_cap("nonsense") == eng.deep_cap("relaxed")


def test_distill_interval_scales_with_intensity() -> None:
    assert (
        eng.distill_interval_seconds("aggressive")
        <= eng.distill_interval_seconds("balanced")
        <= eng.distill_interval_seconds("relaxed")
    )


# ──────────────────────────────── gating ────────────────────────────────────


@pytest.fixture
def cfg(base_config: AgentConfig) -> AgentConfig:
    # The engine is off by default; this agent opts in, the per-agent override
    # that runs Cortex through a fleet-wide off.
    base_config.cognition.enabled = True
    return base_config


async def test_run_phase_disabled_while_the_engine_is_off(
    base_config: AgentConfig, tmp_workspace: Path
) -> None:
    """The default: no agent runs a phase until someone turns the engine on."""
    result = await eng.run_phase("brainstorm", base_config, tmp_workspace, source="autonomous")
    assert result["status"] == "disabled"


async def test_run_phase_disabled_when_feature_off(cfg: AgentConfig, tmp_workspace: Path) -> None:
    cfg.cognition.brainstorming = False  # per-agent feature override
    result = await eng.run_phase("brainstorm", cfg, tmp_workspace, source="autonomous")
    assert result["status"] == "disabled"


async def test_run_phase_skipped_when_autonomy_off(cfg: AgentConfig, tmp_workspace: Path) -> None:
    cfg.cognition.autonomous = False  # master per-agent autonomy off
    result = await eng.run_phase("brainstorm", cfg, tmp_workspace, source="autonomous")
    assert result["status"] == "disabled"
    assert "utonomy" in result["summary"]


async def test_run_phase_manual_ignores_autonomy_off(cfg: AgentConfig, tmp_workspace: Path) -> None:
    cfg.cognition.autonomous = False
    outcome = WorkerOutcome(status="completed", summary="brainstormed")
    with patch("olano.cognition.ideation.run_worker", new=AsyncMock(return_value=outcome)):
        result = await eng.run_phase("brainstorm", cfg, tmp_workspace, source="manual", force=True)
    # Manual run bypasses the autonomy gate.
    assert result["status"] in ("completed", "empty")


async def test_run_phase_skips_when_capped_and_audits(
    cfg: AgentConfig, tmp_workspace: Path
) -> None:
    from olano.cognition.audit import read_actions

    with patch("olano.cognition.orchestrator.deep_gate", return_value=(False, "daily cap reached")):
        result = await eng.run_phase("brainstorm", cfg, tmp_workspace, source="autonomous")
    assert result["status"] == "skipped"
    actions = read_actions(tmp_workspace, limit=10)
    assert any(
        a["action"] == "cortex_brainstorm" and a["detail"].get("status") == "skipped"
        for a in actions
    )


async def test_run_phase_runs_and_records_audit(cfg: AgentConfig, tmp_workspace: Path) -> None:
    from olano.cognition.audit import read_actions
    from olano.cognition.router import deep_runs_today

    before = deep_runs_today(tmp_workspace)
    outcome = WorkerOutcome(
        status="completed", summary="brainstormed Y", artifacts=["BRAINSTORMING.md"]
    )
    with patch("olano.cognition.ideation.run_worker", new=AsyncMock(return_value=outcome)):
        result = await eng.run_phase("brainstorm", cfg, tmp_workspace, source="autonomous")
    assert result["status"] in ("completed", "empty")
    # The deep-run counter advanced exactly once for this phase.
    assert deep_runs_today(tmp_workspace) == before + 1
    actions = read_actions(tmp_workspace, limit=10)
    assert actions[0]["action"] == "cortex_brainstorm"
    assert actions[0]["detail"]["source"] == "autonomous"


async def test_run_cycle_yields_phase_events(cfg: AgentConfig, tmp_workspace: Path) -> None:
    outcome = WorkerOutcome(status="completed", summary="ok")
    events = []
    with patch("olano.cognition.ideation.run_worker", new=AsyncMock(return_value=outcome)):
        async for ev in eng.run_cycle(
            cfg, tmp_workspace, phase_ids=["brainstorm", "checks"], source="manual", force=True
        ):
            events.append(ev)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "phase_start"
    assert kinds[-1] == "all_done"
    assert kinds.count("phase_done") == 2
