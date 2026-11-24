"""Tests for enhanced self-learning service."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Self-improvement and skill extraction are Cortex work, and the engine is off
# by default; these tests are about what they do once it runs.
pytestmark = pytest.mark.usefixtures("cortex_on")


@pytest.fixture
def workspace(tmp_path):
    """Create a minimal workspace."""
    (tmp_path / "memories").mkdir()
    (tmp_path / "MEMORY.md").write_text(
        "## User Preferences\n- Prefers Python\n- Works in UTC\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def agent_config():
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    return AgentConfig(
        name="test",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )


@pytest.fixture
def svc(workspace, agent_config):
    from olano.memory.markdown_memory import MarkdownMemory
    from olano.memory.self_learning import SelfLearningService

    mem = MarkdownMemory(workspace)
    return SelfLearningService(
        agent_config=agent_config,
        workspace_root=workspace,
        markdown_memory=mem,
        interval_seconds=60,
    )


def test_status_initial(svc):
    status = svc.status()
    assert status["distillation_count"] == 0
    assert status["last_run"] is None
    assert status["running"] is False


async def test_self_improve_returns_string(svc):
    # Self-improvement is now a headless deepagent (the CortexEngine improve phase).
    from olano.cognition.worker import WorkerOutcome

    outcome = WorkerOutcome(
        status="completed",
        summary=json.dumps(
            {
                "soul_additions": ["Be more concise in explanations"],
                "agents_additions": ["Always check the workspace before answering"],
                "summary": "The agent should focus on brevity and workspace awareness.",
            }
        ),
    )
    with patch("olano.cognition.worker.run_worker", new=AsyncMock(return_value=outcome)):
        result = await svc.self_improve()

    assert "brevity" in result or "concise" in result or "workspace" in result


async def test_self_improve_saves_proposals_file(svc, workspace):
    from olano.cognition.worker import WorkerOutcome

    async def _fake_worker(**kwargs):
        # The improve worker writes its proposals to the workspace; emulate that.
        (workspace / "memories").mkdir(exist_ok=True)
        (workspace / "memories" / "improvement-proposals.md").write_text(
            "## Self-Improvement Proposals\n\n- Remember to be patient\n", encoding="utf-8"
        )
        return WorkerOutcome(
            status="completed",
            summary="Wrote proposals.",
            artifacts=["memories/improvement-proposals.md"],
        )

    with patch("olano.cognition.worker.run_worker", new=AsyncMock(side_effect=_fake_worker)):
        await svc.self_improve()

    proposals_file = workspace / "memories" / "improvement-proposals.md"
    assert proposals_file.exists()
    content = proposals_file.read_text()
    assert "Self-Improvement Proposals" in content


async def test_self_improve_skipped_when_capped(svc, workspace):
    # Deep-only: when the budget/cap gate fails, self-improve is skipped (no run).
    with patch("olano.cognition.orchestrator.deep_gate", return_value=(False, "daily cap reached")):
        result = await svc.self_improve()
    assert "skipped" in result.lower()


async def test_extract_skills_creates_skill_files(svc, workspace):
    from olano.cognition.worker import WorkerOutcome

    outcome = WorkerOutcome(
        status="completed",
        summary=json.dumps(
            [
                {
                    "name": "summarize_emails",
                    "description": "Summarize unread emails from Gmail",
                    "trigger": "summarize my emails",
                    "steps": ["Check Gmail", "Filter unread", "Summarize each"],
                }
            ]
        ),
    )

    turns = [
        {"role": "user", "content": "Can you check my emails?"},
        {"role": "assistant", "content": "Sure, I'll check Gmail for you."},
    ]

    with patch("olano.cognition.worker.run_worker", new=AsyncMock(return_value=outcome)):
        await svc._extract_skills(turns)

    # New standard: skills live in <slug>/SKILL.md folders.
    skill_file = workspace / "skills" / "learned" / "summarize-emails" / "SKILL.md"
    assert skill_file.exists()
    content = skill_file.read_text()
    assert "summarize-emails" in content
    assert "auto_learned: true" in content


def test_update_writes_learned_shadow_with_provenance(svc, workspace):
    import hashlib

    # An existing (third-party-ish) skill lives under skills/ — NOT learned.
    source = workspace / "skills" / "deep-research" / "SKILL.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\nname: deep-research\ndescription: original\n---\n\n# deep-research\n",
        encoding="utf-8",
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    slug = svc._write_skill_file(
        {
            "action": "update",
            "target": "deep-research",
            "name": "deep-research",
            "description": "improved research workflow",
            "trigger": "research deeply",
            "steps": ["scope", "search", "verify", "synthesize"],
        }
    )
    assert slug == "deep-research"

    # Original is untouched; an improved shadow is written under learned/.
    assert "original" in source.read_text()
    shadow = workspace / "skills" / "learned" / "deep-research" / "SKILL.md"
    assert shadow.exists()
    content = shadow.read_text()
    # Shadows the original by its EXACT name so it wins by name.
    assert "name: deep-research" in content
    assert "lc_improves: deep-research" in content
    assert "lc_source_path: skills/deep-research/SKILL.md" in content
    assert f"lc_source_sha256: {source_hash}" in content


def test_reimproving_shadowed_skill_keeps_provenance(svc, workspace):
    # Original under skills/ (non-learned).
    source = workspace / "skills" / "deep-research" / "SKILL.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\nname: deep-research\ndescription: orig\n---\n# deep-research\n", "utf-8"
    )

    cand = {
        "action": "update",
        "target": "deep-research",
        "name": "deep-research",
        "description": "improved",
        "trigger": "t",
        "steps": ["a", "b", "c"],
    }
    svc._write_skill_file(cand)  # first improvement → shadow created
    # Second improvement: the shadow now exists, but the ORIGINAL must still be
    # found (not hidden by the shadow), so provenance survives.
    svc._write_skill_file(cand)
    content = (workspace / "skills" / "learned" / "deep-research" / "SKILL.md").read_text()
    assert "name: deep-research" in content
    assert "lc_improves: deep-research" in content
    assert "lc_source_path: skills/deep-research/SKILL.md" in content


def test_update_with_unresolvable_target_degrades_to_clean_create(svc, workspace):
    # No such original anywhere → must NOT write a mis-named, provenance-less dup.
    slug = svc._write_skill_file(
        {
            "action": "update",
            "target": "no-such-original",
            "name": "no_such_original",
            "description": "x",
            "trigger": "t",
            "steps": ["a", "b", "c"],
        }
    )
    assert slug == "no-such-original"
    content = (workspace / "skills" / "learned" / "no-such-original" / "SKILL.md").read_text()
    # Named by slug (clean learned skill), with NO broken provenance.
    assert "name: no-such-original" in content
    assert "lc_improves" not in content


def test_skill_exists_does_not_fuse_verb_prefixed_siblings(svc, workspace):
    existing = workspace / "skills" / "deploy-backend" / "SKILL.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("---\nname: deploy-backend\ndescription: d\n---\n# d\n", "utf-8")
    # deploy-frontend shares the 'deploy' prefix but is a distinct skill — not fused.
    assert svc._skill_exists("deploy-frontend") is False
    assert svc._skill_exists("deploy-backend") is True


def test_create_skips_duplicate_of_existing_skill(svc, workspace):
    # A skill already exists under skills/ (a managed/third-party one would be the
    # same): a CREATE of the same capability must be skipped, not duplicated.
    existing = workspace / "skills" / "weather-now" / "SKILL.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("---\nname: weather-now\ndescription: w\n---\n# weather-now\n", "utf-8")

    svc._write_skill_file(
        {
            "action": "create",
            "name": "weather_now",
            "description": "get the weather",
            "trigger": "weather",
            "steps": ["call api", "format", "reply"],
        }
    )
    # No learned duplicate created.
    assert not (workspace / "skills" / "learned" / "weather-now").exists()


async def test_extract_skills_empty_response(svc):
    from olano.cognition.worker import WorkerOutcome

    outcome = WorkerOutcome(status="completed", summary="[]")
    turns = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}]

    with patch("olano.cognition.worker.run_worker", new=AsyncMock(return_value=outcome)):
        # Should not raise
        await svc._extract_skills(turns)


# ----------------------------------------------------------------- fork-based updates

RICH_BODY = (
    "## Fase 0 - Perfil\n\n"
    + ("Profile, keywords, recipients.\n" * 10)
    + "\n## Fase 1 - Busqueda\n\n"
    + ("Exact commands and dedup rules.\n" * 10)
    + "\n## Fase 2 - Reporte\n\n"
    + ("Nine mandatory sections.\n" * 10)
)


def _rich_original(workspace, slug="deep-research"):
    d = workspace / "skills" / slug
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: original runbook\n---\n\n# {slug}\n\n{RICH_BODY}",
        encoding="utf-8",
    )
    (d / "scripts" / "run.py").write_text("print('run')\n", encoding="utf-8")
    return d


def test_update_forks_full_folder_and_appends_delta(svc, workspace):
    d = _rich_original(workspace)
    slug = svc._write_skill_file(
        {
            "action": "update",
            "target": "deep-research",
            "name": "deep-research",
            "description": "handle rate-limit errors during search",
            "trigger": "research deeply",
            "steps": ["retry with backoff on HTTP 429"],
            "update_reason": "original lacks rate-limit handling",
        }
    )
    assert slug == "deep-research"
    fork = workspace / "skills" / "learned" / "deep-research"
    content = (fork / "SKILL.md").read_text()
    # Full original content travels into the fork — nothing is lost…
    assert "Fase 0 - Perfil" in content and "Nine mandatory sections" in content
    assert (fork / "scripts" / "run.py").is_file()
    # …the delta is appended, clearly labelled…
    assert "Learned update" in content
    assert "retry with backoff on HTTP 429" in content
    # …and provenance is recorded (frontmatter + sidecar).
    assert "lc_improves: deep-research" in content
    assert (fork / ".lc-provenance.json").is_file()
    # Original untouched.
    assert "original runbook" in (d / "SKILL.md").read_text()


def test_update_replaces_legacy_machine_stub_with_fork(svc, workspace):
    _rich_original(workspace)
    stub_dir = workspace / "skills" / "learned" / "deep-research"
    stub_dir.mkdir(parents=True)
    (stub_dir / "SKILL.md").write_text(
        "---\nname: deep-research\nauto_learned: true\n---\n\n## deep-research\n\n1. Search\n",
        encoding="utf-8",
    )
    svc._write_skill_file(
        {
            "action": "update",
            "target": "deep-research",
            "name": "deep-research",
            "description": "x",
            "trigger": "t",
            "steps": ["new step"],
        }
    )
    content = (stub_dir / "SKILL.md").read_text()
    assert "Fase 0 - Perfil" in content  # stub replaced by the full fork
    assert (stub_dir / "scripts" / "run.py").is_file()
    assert (stub_dir / ".lc-provenance.json").is_file()


def test_update_leaves_hand_written_learned_override_intact(svc, workspace):
    _rich_original(workspace)
    override_dir = workspace / "skills" / "learned" / "deep-research"
    override_dir.mkdir(parents=True)
    (override_dir / "SKILL.md").write_text(
        "---\nname: deep-research\ndescription: my own flow\n---\n\n# Mine\n\nMy way.\n",
        encoding="utf-8",
    )
    svc._write_skill_file(
        {
            "action": "update",
            "target": "deep-research",
            "name": "deep-research",
            "description": "add retries",
            "trigger": "t",
            "steps": ["retry on 429"],
        }
    )
    content = (override_dir / "SKILL.md").read_text()
    assert "My way." in content  # never replaced by a fork
    assert "retry on 429" in content  # delta appended additively
    assert not (override_dir / ".lc-provenance.json").exists()


async def test_improve_existing_skill_rolls_back_lossy_worker(svc, workspace):
    from olano.cognition.worker import WorkerOutcome

    _rich_original(workspace)
    candidate = {
        "action": "update",
        "target": "deep-research",
        "name": "deep-research",
        "description": "handle 429s",
        "trigger": "t",
        "steps": ["retry with backoff"],
    }

    async def _mangling_worker(**kwargs):
        fork_md = workspace / "skills" / "learned" / "deep-research" / "SKILL.md"
        fork_md.write_text("---\nname: deep-research\n---\n\n## Summary\n\nTiny.\n", "utf-8")
        return WorkerOutcome(status="completed", summary="rewrote it")

    with patch.object(svc, "_run_deep_worker", new=AsyncMock(side_effect=_mangling_worker)):
        slug = await svc._improve_existing_skill(candidate)

    assert slug == "deep-research"
    content = (workspace / "skills" / "learned" / "deep-research" / "SKILL.md").read_text()
    # Gate rolled the mangling back; the fork still carries the full runbook…
    assert "Fase 0 - Perfil" in content
    # …the delta survived via the additive fallback…
    assert "retry with backoff" in content
    # …and the gate failure is visible in Self-Checks.
    assert "skill_improve_gate" in svc.status()["failures"]


async def test_improve_existing_skill_keeps_preserving_worker_edit(svc, workspace):
    from olano.cognition.worker import WorkerOutcome

    _rich_original(workspace)
    candidate = {
        "action": "update",
        "target": "deep-research",
        "name": "deep-research",
        "description": "handle 429s",
        "trigger": "t",
        "steps": ["retry with backoff"],
    }

    async def _good_worker(**kwargs):
        fork_md = workspace / "skills" / "learned" / "deep-research" / "SKILL.md"
        fork_md.write_text(
            fork_md.read_text(encoding="utf-8")
            + "\n### Rate limits\n\nRetry with exponential backoff on HTTP 429.\n",
            encoding="utf-8",
        )
        return WorkerOutcome(status="completed", summary="ARTIFACTS: SKILL.md")

    with patch.object(svc, "_run_deep_worker", new=AsyncMock(side_effect=_good_worker)):
        await svc._improve_existing_skill(candidate)

    content = (workspace / "skills" / "learned" / "deep-research" / "SKILL.md").read_text()
    assert "Rate limits" in content and "Fase 0 - Perfil" in content
    assert "skill_improve_gate" not in svc.status()["failures"]


async def test_improve_never_runs_worker_on_hand_written_override(svc, workspace):
    _rich_original(workspace)
    override_dir = workspace / "skills" / "learned" / "deep-research"
    override_dir.mkdir(parents=True)
    (override_dir / "SKILL.md").write_text(
        "---\nname: deep-research\ndescription: my own flow\n---\n\n# Mine\n\nMy way.\n",
        encoding="utf-8",
    )
    worker = AsyncMock()
    with patch.object(svc, "_run_deep_worker", new=worker):
        slug = await svc._improve_existing_skill(
            {
                "action": "update",
                "target": "deep-research",
                "name": "deep-research",
                "description": "add retries",
                "trigger": "t",
                "steps": ["retry on 429"],
            }
        )
    worker.assert_not_awaited()
    assert slug == "deep-research"
    content = (override_dir / "SKILL.md").read_text()
    assert "My way." in content and "retry on 429" in content


async def test_improve_rolls_back_worker_that_deletes_sidecar(svc, workspace):
    from olano.cognition.worker import WorkerOutcome

    _rich_original(workspace)
    candidate = {
        "action": "update",
        "target": "deep-research",
        "name": "deep-research",
        "description": "x",
        "trigger": "t",
        "steps": ["a step"],
    }

    async def _sidecar_killer(**kwargs):
        (workspace / "skills" / "learned" / "deep-research" / ".lc-provenance.json").unlink()
        return WorkerOutcome(status="completed", summary="done")

    with patch.object(svc, "_run_deep_worker", new=AsyncMock(side_effect=_sidecar_killer)):
        await svc._improve_existing_skill(candidate)

    fork = workspace / "skills" / "learned" / "deep-research"
    assert (fork / ".lc-provenance.json").is_file()  # restored from snapshot
    assert "skill_improve_gate" in svc.status()["failures"]


async def test_polished_create_collision_skips_skillsmith(svc, workspace):
    from olano.cognition.worker import WorkerOutcome

    _rich_original(workspace, slug="weather-now")
    draft = WorkerOutcome(
        status="completed",
        summary='[{"action": "create", "name": "weather_now", "description": "w", '
        '"trigger": "t", "steps": ["a", "b", "c"]}]',
    )
    smith = AsyncMock()
    with (
        patch.object(svc, "_run_deep_worker", new=AsyncMock(return_value=draft)),
        patch("olano.cognition.skillsmith.run_skillsmith", new=smith),
    ):
        await svc._extract_skills_polished([{"role": "user", "content": "x"}] * 4)

    smith.assert_not_awaited()
    assert not (workspace / "skills" / "learned" / "weather-now").exists()


async def test_embed_memory_into_rag_calls_ingest(svc, workspace):
    mock_rag = MagicMock()
    svc.rag_memory = mock_rag

    await svc._embed_memory_into_rag()

    mock_rag.ingest_text.assert_called_once()
    call_kwargs = mock_rag.ingest_text.call_args
    assert "MEMORY.md" in str(call_kwargs)
    assert "memory" in str(call_kwargs)


async def test_embed_memory_no_file(svc, workspace):
    """Should not raise if MEMORY.md doesn't exist."""
    (workspace / "MEMORY.md").unlink()
    mock_rag = MagicMock()
    svc.rag_memory = mock_rag

    # Should not raise
    await svc._embed_memory_into_rag()
    mock_rag.ingest_text.assert_not_called()


# ----------------------------------------------------------------- /improve command


async def test_improve_command(tmp_workspace, base_config, mock_agent):
    from olano.cli.commands import CommandContext, CommandDispatcher
    from olano.core.runner import AgentRunner

    runner = AgentRunner(mock_agent, base_config)
    ctx = CommandContext(config=base_config, runner=runner, thread_id="test")

    dispatcher = CommandDispatcher()

    from olano.cognition.worker import WorkerOutcome

    outcome = WorkerOutcome(
        status="completed",
        summary="Good performance overall.",
        artifacts=["memories/improvement-proposals.md"],
    )

    with patch("olano.cognition.worker.run_worker", new=AsyncMock(return_value=outcome)):
        result = await dispatcher.dispatch("/improve", ctx)

    assert result.output
