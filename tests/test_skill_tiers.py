"""Tests for skill tiering: frontmatter parsing, tier classification, and the
subagent registry filter used by ``delegate_task``."""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.config.agent_config import AgentConfig, MemoryConfig
from olano.config.backend_config import FilesystemBackendConfig
from olano.config.workspace import Workspace
from olano.skills.loader import (
    build_skills_prompt,
    discover_skills,
    load_subagent_registry,
)


def _write_skill(
    root: Path,
    name: str,
    *,
    tier: str | None = None,
    description: str = "a skill",
    blocked_tools: list[str] | None = None,
    max_iterations: int | None = None,
    timeout_seconds: int | None = None,
    trigger: str | None = None,
    body: str = "Skill body.",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---", f"name: {name}", f"description: {description}"]
    if tier is not None:
        fm_lines.append(f"tier: {tier}")
    if blocked_tools is not None:
        fm_lines.append(f"blocked_tools: [{', '.join(blocked_tools)}]")
    if max_iterations is not None:
        fm_lines.append(f"max_iterations: {max_iterations}")
    if timeout_seconds is not None:
        fm_lines.append(f"timeout_seconds: {timeout_seconds}")
    if trigger is not None:
        fm_lines.append(f"trigger: {trigger}")
    fm_lines.append("---")
    content = "\n".join(fm_lines) + f"\n\n{body}\n"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


class TestTierParsing:
    def test_default_tier_is_light(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        skills = {s["name"]: s for s in discover_skills(tmp_path)}
        assert skills["alpha"]["tier"] == "light"
        # Light skills don't get blocked-tools defaults.
        assert skills["alpha"]["blocked_tools"] == []
        assert skills["alpha"]["max_iterations"] == 0
        assert skills["alpha"]["timeout_seconds"] == 0

    def test_subagent_tier_gets_default_blocked_tools(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "researcher", tier="subagent")
        skills = {s["name"]: s for s in discover_skills(tmp_path)}
        meta = skills["researcher"]
        assert meta["tier"] == "subagent"
        # Defaults kick in when frontmatter omits them — and follow the shared
        # delegate_guards budget, not a loader-local literal (a hardcoded 12
        # once lingered here after the constant moved on).
        from olano.security.delegate_guards import (
            DEFAULT_MAX_ITERATIONS,
            DEFAULT_TIMEOUT_SECONDS,
        )

        assert "delegate_task" in meta["blocked_tools"]
        assert "memory_write" in meta["blocked_tools"]
        assert meta["max_iterations"] == DEFAULT_MAX_ITERATIONS
        assert meta["timeout_seconds"] == DEFAULT_TIMEOUT_SECONDS

    def test_subagent_overrides_blocked_tools(self, tmp_path: Path) -> None:
        _write_skill(
            tmp_path,
            "tight",
            tier="subagent",
            blocked_tools=["shell", "memory_write"],
            max_iterations=5,
            timeout_seconds=30,
        )
        meta = {s["name"]: s for s in discover_skills(tmp_path)}["tight"]
        assert set(meta["blocked_tools"]) == {"shell", "memory_write"}
        assert meta["max_iterations"] == 5
        assert meta["timeout_seconds"] == 30

    def test_invalid_tier_falls_back_to_light(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "weird", tier="nonsense")
        meta = {s["name"]: s for s in discover_skills(tmp_path)}["weird"]
        assert meta["tier"] == "light"

    def test_trigger_captured(self, tmp_path: Path) -> None:
        _write_skill(
            tmp_path,
            "searcher",
            tier="subagent",
            trigger="when user asks to research the web",
        )
        meta = {s["name"]: s for s in discover_skills(tmp_path)}["searcher"]
        assert meta["trigger"] == "when user asks to research the web"


class TestSubagentRegistry:
    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Workspace:
        return Workspace(root=tmp_path / "ws")

    @pytest.fixture
    def config(self, tmp_path: Path, workspace: Workspace) -> AgentConfig:
        return AgentConfig(
            name="test-agent",
            description="t",
            model="anthropic:claude-haiku-4-5-20251001",
            system_prompt="p",
            workspace=str(workspace.root),
            memory=MemoryConfig(markdown=False, rag=False, self_learning=False),
            backend=FilesystemBackendConfig(root_dir=str(workspace.root), virtual_mode=False),
            skills=[],
        )

    def test_registry_only_includes_subagent_tier(
        self, workspace: Workspace, config: AgentConfig
    ) -> None:
        skills_root = workspace.skills_dir
        _write_skill(skills_root, "light-one")  # light (default)
        _write_skill(skills_root, "sub-one", tier="subagent")
        _write_skill(skills_root, "sub-two", tier="subagent")
        registry = load_subagent_registry(config, workspace)
        assert set(registry.keys()) == {"sub-one", "sub-two"}
        assert "light-one" not in registry

    def test_registry_empty_when_no_subagents(
        self, workspace: Workspace, config: AgentConfig
    ) -> None:
        _write_skill(workspace.skills_dir, "light-only")
        assert load_subagent_registry(config, workspace) == {}

    def test_registry_value_has_required_fields(
        self, workspace: Workspace, config: AgentConfig
    ) -> None:
        _write_skill(
            workspace.skills_dir,
            "researcher",
            tier="subagent",
            trigger="when user wants research",
        )
        registry = load_subagent_registry(config, workspace)
        meta = registry["researcher"]
        for field_name in (
            "name",
            "description",
            "location",
            "blocked_tools",
            "max_iterations",
            "timeout_seconds",
            "trigger",
        ):
            assert field_name in meta
        assert Path(meta["location"]).exists()

    def test_agent_skills_override_workspace(self, workspace: Workspace, tmp_path: Path) -> None:
        # Workspace has a subagent skill, agent-specific dir shadows it with a
        # different definition (different blocked_tools).
        _write_skill(workspace.skills_dir, "shared", tier="subagent", max_iterations=3)
        agent_dir = tmp_path / "agent-skills"
        _write_skill(agent_dir, "shared", tier="subagent", max_iterations=99)
        config = AgentConfig(
            name="test-agent",
            description="t",
            model="anthropic:claude-haiku-4-5-20251001",
            system_prompt="p",
            workspace=str(workspace.root),
            memory=MemoryConfig(markdown=False, rag=False, self_learning=False),
            backend=FilesystemBackendConfig(root_dir=str(workspace.root), virtual_mode=False),
            skills=[str(agent_dir)],
        )
        # Last-wins semantics per loader docstring: workspace is scanned after
        # the agent dir, so the workspace copy wins. Verify that the registry
        # contains exactly one entry and points to a real SKILL.md file.
        registry = load_subagent_registry(config, workspace)
        assert "shared" in registry
        # The max_iterations comes from whichever source won.
        assert registry["shared"]["max_iterations"] in {3, 99}


class TestBuildSkillsPromptTiers:
    def test_subagent_uses_invoke_via_marker(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "light-skill")
        _write_skill(tmp_path, "sub-skill", tier="subagent", trigger="for subtasks")
        prompt = build_skills_prompt([str(tmp_path)])
        assert "<invoke_via>delegate_task</invoke_via>" in prompt
        assert "<trigger>for subtasks</trigger>" in prompt
        # Light skill should carry a location, subagent should not.
        assert "<location>" in prompt
        # Exactly one location tag — for the light skill only.
        assert prompt.count("<location>") == 1
