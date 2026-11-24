"""Phase 1 coverage for prompt modes, system zone refresh, and runtime hooks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from olano.archetypes.installer import sync_system_zone
from olano.cli.commands import CommandContext, CommandDispatcher
from olano.config.agent_config import AgentConfig
from olano.config.workspace import Workspace
from olano.core.prompt_assembler import PromptAssembler
from olano.core.runner import _BUDGET_NOTICE_70, AgentRunner


@pytest.fixture
def phase1_workspace(tmp_path: Path) -> Path:
    (tmp_path / "AGENTS.md").write_text("# Agent\nFollow the workspace guide.\n", encoding="utf-8")
    (tmp_path / "IDENTITY.md").write_text("# Identity\nBuilder\n", encoding="utf-8")
    (tmp_path / "SOUL.md").write_text("# Soul\nHelpful and direct.\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("# User\nPrefers concise answers.\n", encoding="utf-8")
    (tmp_path / "TOOLS.md").write_text("# Tools\nPrefer exact tools.\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# Memory\nRoadmap notes.\n", encoding="utf-8")
    (tmp_path / "memories").mkdir()
    (tmp_path / ".olano").mkdir()
    (tmp_path / ".olano" / "workspace_map.md").write_text(
        "# Workspace Map\napi/\ncli/\n", encoding="utf-8"
    )
    (tmp_path / ".olano" / "workspace_bridge.md").write_text(
        "# Workspace Bridge\nStart with cli/ for interaction code.\n", encoding="utf-8"
    )
    (tmp_path / "system" / "prompts").mkdir(parents=True)
    (tmp_path / "system" / "prompts" / "CORE_VIBES.md").write_text(
        "# Core Vibes\nWrite it down.\n", encoding="utf-8"
    )
    (tmp_path / "system" / "prompts" / "SAFETY_BOUNDARIES.md").write_text(
        "# Safety\nConfirm destructive actions.\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def phase1_config(phase1_workspace: Path) -> AgentConfig:
    from olano.config.backend_config import FilesystemBackendConfig

    return AgentConfig(
        name="phase1-agent",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="Fallback prompt",
        workspace=str(phase1_workspace),
        tools=["zeta_tool", "alpha_tool"],
        backend=FilesystemBackendConfig(root_dir=str(phase1_workspace)),
        memory={"markdown": True, "rag": False, "self_learning": False},
    )


def test_workspace_init_creates_phase1_state_tree(tmp_path: Path) -> None:
    workspace = Workspace(root=tmp_path)
    workspace.init()

    assert (tmp_path / ".olano" / "state" / "workspace").exists()
    assert (tmp_path / ".olano" / "cache").exists()
    assert (tmp_path / ".olano" / "workspace_map.md").exists()
    assert (tmp_path / ".olano" / "workspace_bridge.md").exists()


def test_prompt_assembler_full_mode_includes_core_vibes_and_sorted_tools(
    phase1_config: AgentConfig, phase1_workspace: Path
) -> None:
    assembler = PromptAssembler(phase1_config, Workspace(root=phase1_workspace))

    prompt = assembler.build()

    assert "Write it down." in prompt
    assert prompt.index("`alpha_tool`") < prompt.index("`zeta_tool`")


def test_prompt_assembler_minimal_task_and_none_modes(
    phase1_config: AgentConfig, phase1_workspace: Path
) -> None:
    assembler = PromptAssembler(phase1_config, Workspace(root=phase1_workspace))

    minimal = assembler.build(mode="minimal")
    task = assembler.build(mode="task", task_query="cli roadmap")
    none_mode = assembler.build(mode="none")

    assert "Safety" in minimal
    assert "Builder" in minimal
    assert "Workspace Bridge" not in minimal
    assert "Roadmap notes." in task or "cli/" in task
    assert "no olano startup context" in none_mode.lower()


@pytest.mark.asyncio
async def test_runner_does_not_auto_compact_after_async_turn(
    phase1_config: AgentConfig,
) -> None:
    agent = MagicMock()

    async def _astream(*args, **kwargs):
        yield AIMessage(content="Done")

    agent.astream = _astream
    agent.ainvoke = AsyncMock(return_value={"messages": [AIMessage(content="Done")]})
    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    runner = AgentRunner(agent, phase1_config)

    await runner.ainvoke("hello", thread_id="thread-summarize")

    # Model-context bounding is owned by deepagents' SummarizationMiddleware.
    # olano no longer destructively compacts its append-only JSONL per turn, so a
    # normal turn never writes a "[Conversation compacted]" marker into the transcript.
    session = runner.load_session("thread-summarize")
    assert not any(
        e.get("role") == "system" and e.get("content", "").startswith("[Conversation compacted]")
        for e in session
    )


@pytest.mark.asyncio
async def test_mode_command_switches_prompt_mode_and_forks_clean_thread(
    phase1_config: AgentConfig,
) -> None:
    agent = MagicMock()
    runner = AgentRunner(agent, phase1_config)
    ctx = CommandContext(config=phase1_config, runner=runner, thread_id="thread-mode")
    dispatcher = CommandDispatcher()

    rebuilt = MagicMock()
    fork_mock = AsyncMock(return_value="thread-mode-fork")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("olano.core.builder.build_agent_async", AsyncMock(return_value=rebuilt))
        monkeypatch.setattr(runner, "fork_thread", fork_mock)
        result = await dispatcher.dispatch("/mode minimal", ctx)

    assert result.output.startswith("Prompt mode:")
    assert ctx.config.prompt_mode == "minimal"
    assert ctx.runner.agent is rebuilt
    assert result.new_thread_id == "thread-mode-fork"
    fork_mock.assert_awaited_once_with(
        "thread-mode",
        carry_history=True,
        carry_state=True,
    )


def test_system_zone_sync_only_touches_system_directory(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    user_file = workspace_root / "USER.md"
    user_file.write_text("do not change\n", encoding="utf-8")

    changed = sync_system_zone(
        workspace_root,
        agent_name="Merli",
        template={
            "description": "Test agent",
            "system_prompt": "You are a test agent.",
            "subagents": [],
            "tools": [],
            "archetype": "default",
        },
        overwrite=True,
    )

    assert changed
    assert user_file.read_text(encoding="utf-8") == "do not change\n"
    assert (workspace_root / "system" / "prompts" / "CORE_VIBES.md").exists()
    assert (workspace_root / "system" / "CHANGELOG.md").exists()


@pytest.mark.asyncio
async def test_astream_events_emits_budget_notice_and_continues_truncated_response(
    phase1_config: AgentConfig,
) -> None:
    phase1_config.iteration_budget = 4

    async def fake_astream(*args, **kwargs):
        yield (
            AIMessage(content="", tool_calls=[{"id": "t1", "name": "tool_a", "args": {}}]),
            {},
        )
        yield (ToolMessage(content="ok", tool_call_id="t1", name="tool_a"), {})
        yield (
            AIMessage(content="", tool_calls=[{"id": "t2", "name": "tool_b", "args": {}}]),
            {},
        )
        yield (ToolMessage(content="ok", tool_call_id="t2", name="tool_b"), {})
        yield (
            AIMessage(content="", tool_calls=[{"id": "t3", "name": "tool_c", "args": {}}]),
            {},
        )
        yield (ToolMessage(content="ok", tool_call_id="t3", name="tool_c"), {})
        yield (AIMessage(content="Hello", response_metadata={"finish_reason": "length"}), {})

    agent = MagicMock()
    agent.astream = fake_astream
    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    agent.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content=" world", response_metadata={"finish_reason": "stop"})]
        }
    )

    runner = AgentRunner(agent, phase1_config)
    events = []
    async for event in runner.astream_events("hello", thread_id="thread-events"):
        events.append(event)

    text_payloads = [payload for kind, payload in events if kind == "text"]

    assert _BUDGET_NOTICE_70 in text_payloads
    assert "Hello" in text_payloads
    assert " world" in text_payloads
