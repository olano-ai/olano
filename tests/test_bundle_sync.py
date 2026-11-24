"""Tests for built-in skill sync and frontmatter support in discover_skills."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

from olano.skills.built_in_sync import (
    _LEGACY_SENTINEL,
    _PINNED,
    _SENTINEL,
    SyncReport,
    sync_builtin_skills,
)
from olano.skills.loader import _read_frontmatter, discover_skills

# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _make_fake_source(tmp_path: Path, skills: dict[str, str]) -> Path:
    """Create a fake built-in skills source with {skill_name: SKILL.md content}."""
    src = tmp_path / "built_in"
    src.mkdir()
    for name, content in skills.items():
        skill_dir = src / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(content)
    return src


def _patch_source(src: Path):
    return patch("olano.skills.built_in_sync._builtin_source_dir", return_value=src)


SKILL_A = "---\nname: skill-a\ncategory: workflow\ndescription: A skill.\n---\n# Skill A\nBody."
SKILL_B = "---\nname: skill-b\ncategory: research\ndescription: B skill.\n---\n# Skill B\nBody."


# ──────────────────────────────────────────────────────────────────
# Tests: frontmatter parsing
# ──────────────────────────────────────────────────────────────────


def test_read_frontmatter_parses_keys():
    content = "---\nname: foo\ncategory: bar\ndescription: baz baz\n---\n# Body"
    fm = _read_frontmatter(content)
    assert fm["name"] == "foo"
    assert fm["category"] == "bar"
    assert fm["description"] == "baz baz"


def test_read_frontmatter_empty_on_no_fence():
    assert _read_frontmatter("# Just a heading\nBody.") == {}


def test_read_frontmatter_empty_on_unclosed_fence():
    assert _read_frontmatter("---\nname: foo\n# forgot to close") == {}


def test_discover_skills_parses_frontmatter(tmp_path: Path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ncategory: workflow\ndescription: Does X.\n---\n# my-skill\nBody."
    )
    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert s["name"] == "my-skill"
    assert s["category"] == "workflow"
    assert s["description"] == "Does X."


def test_discover_skills_falls_back_to_h1(tmp_path: Path):
    skill_dir = tmp_path / "legacy-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Legacy Title\nA description line.")
    skills = discover_skills(tmp_path)
    assert skills[0]["name"] == "Legacy Title"
    assert skills[0]["description"] == "A description line."
    assert skills[0]["category"] == "general"


# ──────────────────────────────────────────────────────────────────
# Tests: sync_builtin_skills
# ──────────────────────────────────────────────────────────────────


def test_fresh_sync_all_new(tmp_path: Path):
    src = _make_fake_source(tmp_path, {"skill-a": SKILL_A, "skill-b": SKILL_B})
    home = tmp_path / "home"
    with _patch_source(src):
        report = sync_builtin_skills(home)
    assert report.new == 2
    assert report.updated == 0
    assert report.unchanged == 0
    assert report.removed == 0
    assert report.pinned == 0
    assert (home / "skills" / "skill-a" / "SKILL.md").exists()
    assert (home / "skills" / "skill-a" / _SENTINEL).exists()
    assert (home / "skills" / "skill-b" / _SENTINEL).exists()


def test_idempotent_second_sync(tmp_path: Path):
    src = _make_fake_source(tmp_path, {"skill-a": SKILL_A, "skill-b": SKILL_B})
    home = tmp_path / "home"
    with _patch_source(src):
        sync_builtin_skills(home)
        report = sync_builtin_skills(home)
    assert report.new == 0
    assert report.updated == 0
    assert report.unchanged == 2


def test_mutated_bundle_forces_update(tmp_path: Path):
    src = _make_fake_source(tmp_path, {"skill-a": SKILL_A, "skill-b": SKILL_B})
    home = tmp_path / "home"
    with _patch_source(src):
        sync_builtin_skills(home)
        # mutate the source
        (src / "skill-a" / "SKILL.md").write_text(SKILL_A + "\nExtra content.")
        report = sync_builtin_skills(home)
    assert report.updated == 1
    assert report.unchanged == 1
    assert report.new == 0


def test_user_edit_on_builtin_is_clobbered(tmp_path: Path):
    src = _make_fake_source(tmp_path, {"skill-a": SKILL_A})
    home = tmp_path / "home"
    with _patch_source(src):
        sync_builtin_skills(home)
        # user adds a file inside the built-in skill dir
        (home / "skills" / "skill-a" / "EXTRA.md").write_text("user content")
        report = sync_builtin_skills(home)
    # the dir was replaced (content hash changed due to EXTRA.md)
    assert not (home / "skills" / "skill-a" / "EXTRA.md").exists()
    assert report.updated == 1


def test_pinned_skill_is_skipped(tmp_path: Path):
    src = _make_fake_source(tmp_path, {"skill-a": SKILL_A, "skill-b": SKILL_B})
    home = tmp_path / "home"
    with _patch_source(src):
        sync_builtin_skills(home)
        # pin skill-a and mutate its source
        (home / "skills" / "skill-a" / _PINNED).touch()
        (src / "skill-a" / "SKILL.md").write_text(SKILL_A + "\nChanged.")
        report = sync_builtin_skills(home)
    assert report.pinned == 1
    assert report.unchanged == 1  # skill-b unchanged
    # skill-a content was NOT updated
    content = (home / "skills" / "skill-a" / "SKILL.md").read_text()
    assert "Changed." not in content


def test_removed_builtin_deletes_synced_copy(tmp_path: Path):
    src = _make_fake_source(tmp_path, {"skill-a": SKILL_A, "skill-b": SKILL_B})
    home = tmp_path / "home"
    with _patch_source(src):
        sync_builtin_skills(home)
        # remove skill-b from source
        shutil.rmtree(src / "skill-b")
        report = sync_builtin_skills(home)
    assert report.removed == 1
    assert not (home / "skills" / "skill-b").exists()


def test_removed_builtin_preserves_user_authored_skill(tmp_path: Path):
    src = _make_fake_source(tmp_path, {"skill-a": SKILL_A})
    home = tmp_path / "home"
    # User-authored skill without sentinel
    user_skill = home / "skills" / "user-custom"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("# Custom\nMy skill.")
    with _patch_source(src):
        report = sync_builtin_skills(home)
    # user-custom was not touched
    assert (user_skill / "SKILL.md").exists()
    assert report.removed == 0


def test_user_skill_wins_in_loader(tmp_path: Path):
    """User skill under workspace/skills/ with same name wins over managed skill."""
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace
    from olano.skills.loader import load_skills_for_agent

    src = _make_fake_source(tmp_path, {"plan": SKILL_A})
    home = tmp_path / "home"
    with _patch_source(src):
        sync_builtin_skills(home)

    # User workspace skill with same name "skill-a" mapped to name "plan"
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    user_plan = ws_root / "skills" / "plan"
    user_plan.mkdir(parents=True)
    (user_plan / "SKILL.md").write_text("# plan\nUser version.")

    ws = Workspace(root=ws_root)
    # patch managed skills to our tmp home
    with patch.object(
        type(ws), "managed_skills_dir", new_callable=lambda: property(lambda self: home / "skills")
    ):
        config = AgentConfig(name="test", model="anthropic:claude-sonnet-4-6")
        paths = load_skills_for_agent(config, ws)

    # deepagents resolves duplicate skill names with last-source-wins, so for the
    # user's workspace skill to win, its dir must appear AFTER the managed dir.
    assert paths.index(str(ws_root / "skills")) > paths.index(str(home / "skills"))


def test_sync_report_str():
    r = SyncReport(new=3, updated=1, unchanged=5, removed=0, pinned=1)
    s = str(r)
    assert "3 new" in s
    assert "1 updated" in s
    assert "5 unchanged" in s
    assert "1 pinned" in s


def test_total_builtin():
    r = SyncReport(new=2, updated=1, unchanged=6, removed=0, pinned=0)
    assert r.total_builtin == 9


def test_total_bundled_alias():
    r = SyncReport(new=2, updated=1, unchanged=6, removed=0, pinned=0)
    assert r.total_bundled == 9


def test_legacy_sentinel_is_migrated_to_builtin(tmp_path: Path):
    src = _make_fake_source(tmp_path, {"skill-a": SKILL_A})
    home = tmp_path / "home"
    skill_dir = home / "skills" / "skill-a"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_A)
    (skill_dir / _LEGACY_SENTINEL).touch()

    with _patch_source(src):
        report = sync_builtin_skills(home)

    assert report.unchanged == 1
    assert (skill_dir / _SENTINEL).exists()
