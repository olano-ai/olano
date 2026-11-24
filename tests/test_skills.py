"""Tests for skills loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.config.agent_config import AgentConfig
from olano.config.workspace import Workspace
from olano.skills.loader import _skill_source_dirs, build_skills_prompt, discover_skills


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """Create a fake skills directory with two skills."""
    for name, desc in [("summarize", "Summarize text"), ("translate", "Translate text")]:
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"# {name.title()}\n\n{desc}\n\n## Usage\n\nCall with text input.\n",
            encoding="utf-8",
        )
    return tmp_path


class TestDiscoverSkills:
    def test_discovers_two_skills(self, skills_dir: Path) -> None:
        skills = discover_skills(skills_dir)
        assert len(skills) == 2

    def test_skill_has_name_and_description(self, skills_dir: Path) -> None:
        # discover_skills uses H1 title from SKILL.md as name
        skills = {s["name"]: s for s in discover_skills(skills_dir)}
        assert "Summarize" in skills
        assert "Summarize text" in skills["Summarize"]["description"]

    def test_discovers_nested_skills(self, tmp_path: Path) -> None:
        # Top-level skill plus a learned skill two levels deep.
        for sub in ["alpha", "learned/beta"]:
            d = tmp_path / sub
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"# {d.name}\n\nDoes {d.name}.\n", encoding="utf-8")

        names = {s["name"] for s in discover_skills(tmp_path)}
        assert names == {"alpha", "beta"}

    def test_prunes_skill_md_bundled_inside_a_skill(self, tmp_path: Path) -> None:
        # A skill that ships an example SKILL.md must still count as one skill.
        skill = tmp_path / "outer"
        (skill / "examples").mkdir(parents=True)
        (skill / "SKILL.md").write_text("# outer\n\nReal skill.\n", encoding="utf-8")
        (skill / "examples" / "SKILL.md").write_text("# inner\n\nExample.\n", encoding="utf-8")

        names = {s["name"] for s in discover_skills(tmp_path)}
        assert names == {"outer"}

    def test_requires_tools_parsed_from_list_and_inline_string(self, tmp_path: Path) -> None:
        for name, spec in (("as-list", "[olano_list_agents, whoami]"), ("inline", "a, b")):
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: d\nrequires_tools: {spec}\n---\nBody.\n",
                encoding="utf-8",
            )
        (tmp_path / "none").mkdir()
        (tmp_path / "none" / "SKILL.md").write_text(
            "---\nname: none\ndescription: d\n---\nBody.\n", encoding="utf-8"
        )
        skills = {s["name"]: s for s in discover_skills(tmp_path)}
        assert skills["as-list"]["requires_tools"] == ["olano_list_agents", "whoami"]
        assert skills["inline"]["requires_tools"] == ["a", "b"]
        assert skills["none"]["requires_tools"] == []

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert discover_skills(tmp_path) == []

    def test_missing_dir(self, tmp_path: Path) -> None:
        assert discover_skills(tmp_path / "nonexistent") == []


def _write_skill(skills_dir: Path, name: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"# {name}\n\nDoes {name}.\n", encoding="utf-8")


class TestSkillSourceDirs:
    def test_includes_learned_dir_ordered_last(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        workspace = Workspace(root=tmp_workspace)
        # workspace/skills/<a> and workspace/skills/learned/<b>
        _write_skill(workspace.skills_dir, "alpha")
        _write_skill(workspace.learned_skills_dir, "beta")
        # managed (~/.olano/skills) common skill
        _write_skill(workspace.managed_skills_dir, "gamma")

        dirs = _skill_source_dirs(base_config, workspace)

        managed = str(workspace.managed_skills_dir)
        ws_skills = str(workspace.skills_dir)
        learned = str(workspace.learned_skills_dir)

        assert learned in dirs
        # deepagents is last-wins, so priority learned > skills > common means
        # learned must come last and common first among the three.
        assert dirs.index(managed) < dirs.index(ws_skills) < dirs.index(learned)

    def test_learned_dir_skipped_when_absent(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        workspace = Workspace(root=tmp_workspace)
        _write_skill(workspace.skills_dir, "alpha")

        dirs = _skill_source_dirs(base_config, workspace)

        assert str(workspace.learned_skills_dir) not in dirs


class TestBuildSkillsPrompt:
    def test_prompt_contains_skill_names(self, skills_dir: Path) -> None:
        prompt = build_skills_prompt([str(skills_dir)])
        assert "summarize" in prompt.lower() or "summarize" in prompt

    def test_empty_dirs(self) -> None:
        prompt = build_skills_prompt([])
        assert prompt == "" or "no skills" in prompt.lower()
