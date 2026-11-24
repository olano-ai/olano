"""Tests for the SkillSmith worker integration (Phase 7)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from olano.cognition.worker import WorkerOutcome
from olano.config.agent_config import AgentConfig
from olano.memory.markdown_memory import MarkdownMemory
from olano.memory.self_learning import SelfLearningService


def _svc(tmp_workspace: Path, base_config: AgentConfig) -> SelfLearningService:
    return SelfLearningService(
        agent_config=base_config,
        workspace_root=tmp_workspace,
        markdown_memory=MarkdownMemory(tmp_workspace),
        rag_memory=None,
    )


async def test_polished_uses_skillsmith_when_completed(
    tmp_workspace: Path, base_config: AgentConfig
) -> None:
    svc = _svc(tmp_workspace, base_config)
    candidate = {"name": "summarize_emails", "description": "d", "trigger": "t", "steps": ["a"]}
    outcome = WorkerOutcome(status="completed", summary="wrote skill")
    with (
        patch.object(svc, "_draft_skill_candidates", new=AsyncMock(return_value=[candidate])),
        patch("olano.cognition.skillsmith.run_worker", new=AsyncMock(return_value=outcome)) as rw,
        patch.object(svc, "_capture_learned_skills_pulse") as pulse,
    ):
        await svc._extract_skills_polished([{"role": "user", "content": "x"}])
    rw.assert_awaited_once()
    pulse.assert_called_once_with(["summarize_emails"])


async def test_polished_falls_back_to_direct_write(
    tmp_workspace: Path, base_config: AgentConfig
) -> None:
    svc = _svc(tmp_workspace, base_config)
    candidate = {"name": "do_thing", "description": "d", "trigger": "t", "steps": ["a"]}
    outcome = WorkerOutcome(status="error", error="boom")
    with (
        patch.object(svc, "_draft_skill_candidates", new=AsyncMock(return_value=[candidate])),
        patch("olano.cognition.skillsmith.run_worker", new=AsyncMock(return_value=outcome)),
        patch.object(svc, "_capture_learned_skills_pulse"),
    ):
        await svc._extract_skills_polished([{"role": "user", "content": "x"}])
    # Fallback direct write created the folder/SKILL.md (new standard structure).
    assert (tmp_workspace / "skills" / "learned" / "do-thing" / "SKILL.md").exists()


async def test_polished_disabled_uses_plain_extract(
    tmp_workspace: Path, base_config: AgentConfig
) -> None:
    svc = _svc(tmp_workspace, base_config)
    base_config.cognition.enable_skillsmith = False
    with patch.object(svc, "_extract_skills", new=AsyncMock()) as plain:
        await svc._extract_skills_polished([{"role": "user", "content": "x"}])
    plain.assert_awaited_once()
