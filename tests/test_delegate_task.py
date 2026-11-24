"""Tests for ``delegate_task`` subagent tool: single/batch modes, depth guard,
timeout classification, blocked-tool filtering, and unknown-skill errors."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import StructuredTool

from olano.config.agent_config import AgentConfig, MemoryConfig
from olano.config.backend_config import FilesystemBackendConfig
from olano.security import delegate_guards
from olano.security.delegate_guards import (
    DEFAULT_BLOCKED_TOOLS,
    DEPTH_VAR,
    MAX_DEPTH,
    at_limit,
    current_depth,
    merge_blocked_tools,
)
from olano.tools.delegate_task import (
    _build_child_config,
    _build_child_system_prompt,
    _extract_summary,
    _read_skill_body,
    make_delegate_task_tool,
)

# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _configured_model(monkeypatch):
    """delegate_task drives run_worker, which now preflights model readiness;
    provide a dummy provider key so these mechanics tests reach the mocked build."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")


@pytest.fixture
def parent_config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        name="parent-agent",
        description="parent",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="parent prompt",
        workspace=str(tmp_path),
        memory=MemoryConfig(markdown=False, rag=False, self_learning=False),
        backend=FilesystemBackendConfig(root_dir=str(tmp_path), virtual_mode=False),
        tools=["read_file", "memory_write", "delegate_task", "shell"],
    )


@pytest.fixture
def sample_skill(tmp_path: Path) -> dict[str, Any]:
    skill_dir = tmp_path / "researcher"
    skill_dir.mkdir()
    md = skill_dir / "SKILL.md"
    md.write_text(
        "---\nname: researcher\ndescription: research stuff\ntier: subagent\n---\n\n"
        "You are a web researcher. Stick to facts.\n",
        encoding="utf-8",
    )
    return {
        "name": "researcher",
        "description": "research stuff",
        "location": str(md),
        "blocked_tools": ["memory_write"],
        "max_iterations": 5,
        "timeout_seconds": 30,
        "trigger": "for research",
    }


@pytest.fixture
def registry(sample_skill: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {sample_skill["name"]: sample_skill}


@pytest.fixture(autouse=True)
def reset_depth():
    token = DEPTH_VAR.set(0)
    yield
    DEPTH_VAR.reset(token)


# ── guard module ──────────────────────────────────────────────────────────────


class TestGuards:
    def test_default_blocked_tools_include_critical_set(self) -> None:
        critical = {
            "delegate_task",
            "memory_write",
            "memory_append",
            "send_message",
            "shell",
            "safe_shell",
        }
        assert critical.issubset(DEFAULT_BLOCKED_TOOLS)

    def test_merge_blocked_tools_adds_custom_and_dedupes(self) -> None:
        merged = merge_blocked_tools(["custom_write", "shell", ""])
        assert "custom_write" in merged
        assert "shell" in merged  # default + explicit
        assert "memory_write" in merged  # from defaults

    def test_merge_blocked_tools_handles_none(self) -> None:
        assert merge_blocked_tools(None) == DEFAULT_BLOCKED_TOOLS

    def test_depth_var_defaults_to_zero(self) -> None:
        assert current_depth() == 0
        assert at_limit() is False

    def test_at_limit_true_at_max_depth(self) -> None:
        token = DEPTH_VAR.set(MAX_DEPTH)
        try:
            assert at_limit() is True
        finally:
            DEPTH_VAR.reset(token)


# ── helpers ──────────────────────────────────────────────────────────────────


class TestReadSkillBody:
    def test_strips_frontmatter(self, tmp_path: Path) -> None:
        p = tmp_path / "s.md"
        p.write_text("---\nname: x\n---\n\nBody text.\n", encoding="utf-8")
        assert _read_skill_body(str(p)) == "Body text."

    def test_no_frontmatter_returns_all(self, tmp_path: Path) -> None:
        p = tmp_path / "s.md"
        p.write_text("Just a body.\n", encoding="utf-8")
        assert _read_skill_body(str(p)) == "Just a body."

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _read_skill_body(str(tmp_path / "missing.md")) == ""


class TestBuildChildSystemPrompt:
    def test_includes_task(self) -> None:
        p = _build_child_system_prompt("skill body", "do X", None)
        assert "TASK:" in p
        assert "do X" in p
        assert "skill body" in p
        assert "CONTEXT:" not in p

    def test_includes_context_when_given(self) -> None:
        p = _build_child_system_prompt("body", "goal", "extra info")
        assert "CONTEXT:" in p
        assert "extra info" in p


class TestBuildChildConfig:
    def test_child_resolves_credentials_as_the_parent(self, base_config):
        child = _build_child_config(base_config, {"name": "research"}, "prompt")
        assert child.credential_scope == base_config.name
        assert "credential_scope" not in child.model_dump()

    def test_filters_blocked_tools(
        self, parent_config: AgentConfig, sample_skill: dict[str, Any]
    ) -> None:
        child = _build_child_config(parent_config, sample_skill, "child prompt")
        # Default blocked tools + skill-specific blocked tools excluded.
        for banned in DEFAULT_BLOCKED_TOOLS:
            assert banned not in child.tools
        assert "memory_write" not in child.tools
        # Read-only tools pass through.
        assert "read_file" in child.tools

    def test_child_name_is_unique(
        self, parent_config: AgentConfig, sample_skill: dict[str, Any]
    ) -> None:
        a = _build_child_config(parent_config, sample_skill, "p")
        b = _build_child_config(parent_config, sample_skill, "p")
        assert a.name != b.name
        assert "parent-agent" in a.name
        assert "researcher" in a.name

    def test_child_strips_nested_spawn_surface(
        self, parent_config: AgentConfig, sample_skill: dict[str, Any]
    ) -> None:
        child = _build_child_config(parent_config, sample_skill, "p")
        assert child.skills == []
        assert child.subagents == []
        assert child.builtin_subagents == []
        assert child.channels == []
        assert child.schedule == []


class TestExtractSummary:
    def test_pulls_last_string_message(self) -> None:
        msg1 = MagicMock()
        msg1.content = "first"
        msg2 = MagicMock()
        msg2.content = "final answer"
        assert _extract_summary({"messages": [msg1, msg2]}) == "final answer"

    def test_handles_list_content_parts(self) -> None:
        msg = MagicMock()
        msg.content = [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}]
        out = _extract_summary({"messages": [msg]})
        assert "part one" in out
        assert "part two" in out

    def test_empty_result(self) -> None:
        assert _extract_summary({"messages": []}) == ""
        assert _extract_summary(None) == ""


# ── factory ──────────────────────────────────────────────────────────────────


class TestFactory:
    def test_returns_none_when_registry_empty(
        self, parent_config: AgentConfig, tmp_path: Path
    ) -> None:
        from olano.config.workspace import Workspace

        ws = Workspace(root=tmp_path)
        tool = make_delegate_task_tool(
            parent_config=parent_config,
            workspace=ws,
            subagent_registry={},
        )
        assert tool is None

    def test_returns_structured_tool(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        from olano.config.workspace import Workspace

        ws = Workspace(root=tmp_path)
        tool = make_delegate_task_tool(
            parent_config=parent_config,
            workspace=ws,
            subagent_registry=registry,
        )
        assert isinstance(tool, StructuredTool)
        assert tool.name == "delegate_task"
        assert "researcher" in tool.description


# ── end-to-end invocations with mocked builder ───────────────────────────────


def _make_child_with_content(content: str) -> MagicMock:
    """Return a mock child agent whose ainvoke yields one AI message."""
    msg = MagicMock()
    msg.content = content
    child = MagicMock()
    child.ainvoke = AsyncMock(return_value={"messages": [msg]})
    return child


def _make_tool(
    parent_config: AgentConfig,
    tmp_path: Path,
    registry: dict[str, dict[str, Any]],
    *,
    max_concurrent: int = 3,
) -> StructuredTool:
    from olano.config.workspace import Workspace

    ws = Workspace(root=tmp_path / "ws-alt")
    tool = make_delegate_task_tool(
        parent_config=parent_config,
        workspace=ws,
        subagent_registry=registry,
        max_concurrent=max_concurrent,
    )
    assert tool is not None
    return tool


class TestRunSingle:
    async def test_single_task_success(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry)
        child = _make_child_with_content("researched: the answer is 42.")
        with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
            raw = await tool.ainvoke({"skill": "researcher", "goal": "find the answer"})
        payload = json.loads(raw)
        assert payload["status"] == "completed"
        assert payload["skill"] == "researcher"
        assert "answer is 42" in payload["summary"]
        # Single-task shape: no task_index, no results array.
        assert "task_index" not in payload
        assert "results" not in payload

    async def test_unknown_skill_returns_error(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry)
        raw = await tool.ainvoke({"skill": "unknown", "goal": "x"})
        payload = json.loads(raw)
        assert payload["status"] == "error"
        assert "Unknown subagent skill" in payload["error"]
        assert "researcher" in payload["available_skills"]

    async def test_missing_goal_in_batch_returns_error(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        # In batch mode, each task runs through `_run_single` which validates
        # goal individually. Single-mode empty-goal is caught earlier as a
        # mode-resolution error and is covered by `test_missing_both_modes_errors`.
        tool = _make_tool(parent_config, tmp_path, registry)
        raw = await tool.ainvoke({"tasks": [{"skill": "researcher", "goal": ""}]})
        payload = json.loads(raw)
        # Single-task shape because normalized list has length 1.
        assert payload["status"] == "error"
        assert "goal is required" in payload["error"]

    async def test_missing_both_modes_errors(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry)
        raw = await tool.ainvoke({})
        payload = json.loads(raw)
        assert payload["status"] == "error"
        assert "single" in payload["error"] or "batch" in payload["error"]

    async def test_timeout_is_classified(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry)

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(5)
            return {"messages": []}

        child = MagicMock()
        child.ainvoke = hang
        # Shrink timeout so the test is fast.
        registry["researcher"]["timeout_seconds"] = 0.05
        with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
            raw = await tool.ainvoke({"skill": "researcher", "goal": "stall"})
        payload = json.loads(raw)
        assert payload["status"] == "timeout"
        assert "timeout" in payload["error"].lower()

    async def test_build_failure_captured(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry)
        with patch(
            "olano.core.builder.build_agent_async",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            raw = await tool.ainvoke({"skill": "researcher", "goal": "x"})
        payload = json.loads(raw)
        assert payload["status"] == "error"
        assert "boom" in payload["error"]


class TestDepthGuard:
    async def test_at_max_depth_rejects(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry)
        token = DEPTH_VAR.set(MAX_DEPTH)
        try:
            raw = await tool.ainvoke({"skill": "researcher", "goal": "x"})
        finally:
            DEPTH_VAR.reset(token)
        payload = json.loads(raw)
        assert payload["status"] == "rejected"
        assert "depth" in payload["error"].lower()

    async def test_depth_incremented_during_child_run(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry)
        seen_depths: list[int] = []

        async def capture_depth(*_args, **_kwargs):
            seen_depths.append(delegate_guards.current_depth())
            msg = MagicMock()
            msg.content = "done"
            return {"messages": [msg]}

        child = MagicMock()
        child.ainvoke = capture_depth
        with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
            raw = await tool.ainvoke({"skill": "researcher", "goal": "x"})
        assert json.loads(raw)["status"] == "completed"
        assert seen_depths == [1]
        # Restored to parent depth after return.
        assert current_depth() == 0


class TestBatchMode:
    async def test_batch_returns_results_array(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry, max_concurrent=2)
        child = _make_child_with_content("ok")
        with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
            raw = await tool.ainvoke(
                {
                    "tasks": [
                        {"skill": "researcher", "goal": "first"},
                        {"skill": "researcher", "goal": "second"},
                        {"skill": "researcher", "goal": "third"},
                    ]
                }
            )
        payload = json.loads(raw)
        assert "results" in payload
        assert payload["max_concurrent"] == 2
        assert len(payload["results"]) == 3
        for i, r in enumerate(payload["results"]):
            assert r["task_index"] == i
            assert r["status"] == "completed"

    async def test_batch_bounded_concurrency(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry, max_concurrent=2)
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def tracked_invoke(*_args, **_kwargs):
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            try:
                await asyncio.sleep(0.05)
                msg = MagicMock()
                msg.content = "done"
                return {"messages": [msg]}
            finally:
                async with lock:
                    active -= 1

        child = MagicMock()
        child.ainvoke = tracked_invoke
        with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
            await tool.ainvoke(
                {"tasks": [{"skill": "researcher", "goal": f"task-{i}"} for i in range(5)]}
            )
        # Semaphore limit is 2 — peak concurrency must respect it.
        assert peak <= 2

    async def test_batch_mixed_unknown_and_valid(
        self,
        parent_config: AgentConfig,
        tmp_path: Path,
        registry: dict[str, dict[str, Any]],
    ) -> None:
        tool = _make_tool(parent_config, tmp_path, registry)
        child = _make_child_with_content("ok")
        with patch("olano.core.builder.build_agent_async", new=AsyncMock(return_value=child)):
            raw = await tool.ainvoke(
                {
                    "tasks": [
                        {"skill": "researcher", "goal": "valid"},
                        {"skill": "unknown", "goal": "bad"},
                    ]
                }
            )
        payload = json.loads(raw)
        statuses = [r["status"] for r in payload["results"]]
        assert "completed" in statuses
        assert "error" in statuses
