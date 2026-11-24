"""Tests for the agentic (deep-only) self-learning paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from olano.cognition.worker import WorkerOutcome
from olano.config.agent_config import AgentConfig
from olano.memory.markdown_memory import MarkdownMemory
from olano.memory.self_learning import SelfLearningService

# Deep self-learning is Cortex work, and the engine is off by default. Without
# this the "skipped when X" tests below would pass because the engine is off.
pytestmark = pytest.mark.usefixtures("cortex_on")


@pytest.fixture
def md_memory(tmp_workspace: Path) -> MarkdownMemory:
    return MarkdownMemory(tmp_workspace)


@pytest.fixture
def svc(
    base_config: AgentConfig, tmp_workspace: Path, md_memory: MarkdownMemory
) -> SelfLearningService:
    return SelfLearningService(
        agent_config=base_config,
        workspace_root=tmp_workspace,
        markdown_memory=md_memory,
        rag_memory=None,
    )


async def test_weekly_routed_uses_deep_worker(svc: SelfLearningService, md_memory) -> None:
    md_memory.append_daily("Worked on auth refactor")
    outcome = WorkerOutcome(status="completed", summary="consolidated", artifacts=["LEARNINGS.md"])
    with patch("olano.cognition.worker.run_worker", new=AsyncMock(return_value=outcome)) as rw:
        await svc._weekly_distillation_routed()
    rw.assert_awaited_once()
    # The deep self-learning playbook was used.
    _, kwargs = rw.call_args
    assert "self-learning analyst" in kwargs["playbook"]


async def test_weekly_routed_skipped_when_deep_disabled(
    svc: SelfLearningService, md_memory
) -> None:
    # Deep-only: with deep self-learning disabled there is no cheap fallback —
    # the weekly cycle is skipped entirely (the worker never runs).
    md_memory.append_daily("Something happened")
    svc.config.cognition.deep_self_learning = False
    with (
        patch("olano.cognition.worker.run_worker", new=AsyncMock()) as rw,
        patch.object(svc, "_weekly_distillation", new=AsyncMock()) as inner,
    ):
        await svc._weekly_distillation_routed()
    rw.assert_not_awaited()
    inner.assert_not_awaited()


async def test_weekly_routed_skipped_when_feature_off(svc: SelfLearningService, md_memory) -> None:
    md_memory.append_daily("x")
    with (
        patch("olano.config.system_settings.feature_enabled", return_value=False),
        patch("olano.cognition.worker.run_worker", new=AsyncMock()) as rw,
        patch.object(svc, "_weekly_distillation", new=AsyncMock()) as inner,
    ):
        await svc._weekly_distillation_routed()
    rw.assert_not_awaited()
    inner.assert_not_awaited()


async def test_skill_extraction_gated_off(svc: SelfLearningService) -> None:
    turns = [{"role": "user", "content": "x"} for _ in range(6)]
    with (
        patch.object(svc, "_distill_facts", new=AsyncMock(return_value=["a fact"])),
        patch.object(svc, "_save_facts", new=AsyncMock()),
        patch.object(svc, "_extract_skills_polished", new=AsyncMock()) as extract,
        patch("olano.config.system_settings.load_system_settings") as ls,
    ):
        ls.return_value.enabled = True
        ls.return_value.autonomous = True
        ls.return_value.intensity = "aggressive"
        ls.return_value.features.skills_optimization = False
        # Seed a session file so _distill_from_sessions has >=4 new turns.
        from olano.config.home import get_olano_paths

        sess_dir = get_olano_paths().sessions_dir / svc.config.name
        sess_dir.mkdir(parents=True, exist_ok=True)
        import json

        (sess_dir / "t.jsonl").write_text("\n".join(json.dumps(t) for t in turns), encoding="utf-8")
        await svc._distill_from_sessions()
    extract.assert_not_awaited()


async def test_self_improve_deep_returns_summary(svc: SelfLearningService, md_memory) -> None:
    (svc.workspace_root / "MEMORY.md").write_text(
        "## User Preferences\n- Prefers Python\n- Works in UTC\n- Likes brevity\n", encoding="utf-8"
    )
    # The improve worker returns a JSON proposals object; its summary surfaces back.
    outcome = WorkerOutcome(
        status="completed",
        summary='{"soul_additions": ["Be concise"], "agents_additions": [], '
        '"summary": "proposed 3 changes"}',
    )
    with patch("olano.cognition.worker.run_worker", new=AsyncMock(return_value=outcome)):
        result = await svc.self_improve_deep()
    assert "proposed 3 changes" in result
