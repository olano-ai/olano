"""Tests for actionable Pulse: run_worker action, undo, async approve, curation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from olano.cognition.worker import WorkerOutcome
from olano.config.agent_config import AgentConfig
from olano.config.backend_config import FilesystemBackendConfig
from olano.pulse.engine import PulseEngine
from olano.pulse.models import PulseLane, PulseType


@pytest.fixture(autouse=True)
def _configured_model(monkeypatch):
    """Curation tests assume a reachable model; satisfy the readiness preflight
    with a dummy provider key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


def _engine(tmp_path: Path) -> PulseEngine:
    config = AgentConfig(
        name="pa",
        model="anthropic:claude-haiku-4-5",
        workspace=str(tmp_path),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
    )
    return PulseEngine(config, tmp_path / ".olano")


def test_sync_approve_run_worker_enqueues(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    item = engine.add(
        content="Expand this idea",
        type=PulseType.PROPOSAL,
        lane=PulseLane.APPROVAL,
        title="Expand idea",
        action={"type": "run_worker", "playbook": "idea_expand", "goal": "expand"},
    )
    ok, msg = engine.approve(item.id)
    assert ok
    assert "Working on it" in msg
    # A job descriptor was written and the item tagged.
    jobs = list((tmp_path / ".olano" / "state" / "cognition" / "jobs").glob("*.json"))
    assert len(jobs) == 1
    descriptor = json.loads(jobs[0].read_text())
    assert descriptor["status"] == "queued"
    assert descriptor["action"]["playbook"] == "idea_expand"
    assert item.metadata.get("worker_job_id")


async def test_execute_action_async_runs_worker(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    item = engine.add(
        content="Expand",
        type=PulseType.PROPOSAL,
        lane=PulseLane.APPROVAL,
        title="Expand",
        action={
            "type": "run_worker",
            "playbook": "idea_expand",
            "goal": "expand",
            "output_hint": "IDEAS.md",
        },
    )
    outcome = WorkerOutcome(status="completed", summary="wrote one-pager", artifacts=["IDEAS.md"])
    with patch("olano.cognition.pulse_agent.run_worker", new=AsyncMock(return_value=outcome)) as rw:
        ok, msg, result = await engine.execute_action_async(item)
    assert ok
    assert "one-pager" in msg
    assert result.artifacts == ["IDEAS.md"]
    rw.assert_awaited_once()
    # A backup was recorded for undo.
    assert item.metadata.get("backup_job_id")


def test_append_lines_action_still_works(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    item = engine.add(
        content="add note",
        type=PulseType.PROPOSAL,
        lane=PulseLane.APPROVAL,
        title="note",
        action={"type": "append_lines", "path": "NOTES.md", "lines": ["hello"], "heading": "Log"},
    )
    ok, msg = engine.approve(item.id)
    assert ok
    assert (tmp_path / "NOTES.md").read_text(encoding="utf-8").count("hello") == 1


def test_restore_backup_action_and_undo(tmp_path: Path) -> None:
    from olano.cognition.backups import snapshot_backup

    target = tmp_path / "IDEAS.md"
    target.write_text("original", encoding="utf-8")
    job_id = snapshot_backup(tmp_path, ["IDEAS.md"], "test")
    target.write_text("changed", encoding="utf-8")

    engine = _engine(tmp_path)
    item = engine.add(
        content="Done — wrote IDEAS.md",
        type=PulseType.FINDING,
        lane=PulseLane.PULSE,
        title="Idea expanded",
        action={"type": "restore_backup", "job_id": job_id},
    )
    ok, msg = engine.undo(item.id)
    assert ok
    assert target.read_text(encoding="utf-8") == "original"
    assert item.metadata.get("undone_at")


async def test_curate_pulse_falls_back_when_budget_blocked(tmp_path: Path) -> None:
    from olano.cognition.pulse_agent import curate_pulse
    from olano.pulse.generators import PulseGenerator

    gen = PulseGenerator(
        AgentConfig(
            name="pa",
            model="anthropic:claude-haiku-4-5",
            workspace=str(tmp_path),
            backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
        ),
        tmp_path,
    )
    with (
        patch("olano.cognition.pulse_agent.budget_gate", return_value=(False, "over")),
        patch.object(
            gen, "capture_agent_curated_pulse", new=AsyncMock(return_value=["fallback"])
        ) as fast,
    ):
        result = await curate_pulse(gen, force=True)
    # Budget-blocked but the FAST fallback surfaced items → completed with those items.
    assert result.status == "completed"
    assert result.items == ["fallback"]
    fast.assert_awaited_once()


async def test_curate_pulse_budget_blocked_empty_reports_reason(tmp_path: Path) -> None:
    """Budget-blocked AND the fallback found nothing → status carries a fixable reason."""
    from olano.cognition.pulse_agent import curate_pulse
    from olano.pulse.generators import PulseGenerator

    gen = PulseGenerator(
        AgentConfig(
            name="pa",
            model="anthropic:claude-haiku-4-5",
            workspace=str(tmp_path),
            backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
        ),
        tmp_path,
    )
    with (
        patch("olano.cognition.pulse_agent.budget_gate", return_value=(False, "over budget")),
        patch.object(gen, "capture_agent_curated_pulse", new=AsyncMock(return_value=[])),
    ):
        result = await curate_pulse(gen, force=True)
    assert result.status == "budget_blocked"
    assert not result.items
    assert "budget" in result.reason.lower()


def test_add_curated_items_with_action(tmp_path: Path) -> None:
    from olano.pulse.generators import PulseGenerator

    gen = PulseGenerator(
        AgentConfig(
            name="pa",
            model="anthropic:claude-haiku-4-5",
            workspace=str(tmp_path),
            backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
        ),
        tmp_path,
    )
    items = gen.add_curated_items(
        [
            {
                "type": "opportunity",
                "title": "Try X",
                "summary": "X looks promising",
                "action": {"type": "run_worker", "playbook": "idea_expand", "goal": "g"},
            }
        ]
    )
    assert len(items) == 1
    assert items[0].action["playbook"] == "idea_expand"
    assert items[0].lane == PulseLane.PULSE
