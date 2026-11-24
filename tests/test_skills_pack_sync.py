"""Tests for the external skill-pack fetch (olano-ai/skills) and its coexistence
with the package-bundled built-in skill sync."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from olano.skills.built_in_sync import _PINNED, _SENTINEL, _SKILLPACK, sync_builtin_skills
from olano.skills.sync import (
    _copy_pack_skills,
    common_skills_dir,
    fetch_olano_skills,
    olano_skills_pack_dir,
)

SKILL_A = "---\nname: skill-a\ncategory: workflow\ndescription: A skill.\n---\n# Skill A\nBody."
SKILL_B = "---\nname: skill-b\ncategory: research\ndescription: B skill.\n---\n# Skill B\nBody."

_GIT = shutil.which("git")
requires_git = pytest.mark.skipif(_GIT is None, reason="git not available")


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _make_pack_skills_dir(tmp_path: Path, skills: dict[str, str], *, name: str = "pack") -> Path:
    """Create a repo-style ``skills/`` dir with {skill_name: SKILL.md content}."""
    skills_dir = tmp_path / name / "skills"
    skills_dir.mkdir(parents=True)
    for skill_name, content in skills.items():
        d = skills_dir / skill_name
        d.mkdir()
        (d / "SKILL.md").write_text(content)
    return skills_dir


def _make_git_skills_repo(tmp_path: Path, skills: dict[str, str]) -> Path:
    """Create a git repo whose ``skills/`` dir holds the given skills."""
    repo = tmp_path / "remote-skills"
    skills_dir = repo / "skills"
    skills_dir.mkdir(parents=True)
    (repo / "README.md").write_text("# skills\n")
    for skill_name, content in skills.items():
        d = skills_dir / skill_name
        d.mkdir()
        (d / "SKILL.md").write_text(content)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    return repo


# ──────────────────────────────────────────────────────────────────
# _copy_pack_skills
# ──────────────────────────────────────────────────────────────────


def test_copy_pack_skills_fresh(tmp_path: Path):
    pack = _make_pack_skills_dir(tmp_path, {"skill-a": SKILL_A, "skill-b": SKILL_B})
    target = tmp_path / "home" / "skills"
    report = _copy_pack_skills(pack, target)
    assert report.new == 2
    assert report.updated == 0
    assert report.unchanged == 0
    assert (target / "skill-a" / "SKILL.md").exists()
    assert (target / "skill-a" / _SKILLPACK).exists()
    assert (target / "skill-b" / _SKILLPACK).exists()


def test_copy_pack_skills_idempotent(tmp_path: Path):
    pack = _make_pack_skills_dir(tmp_path, {"skill-a": SKILL_A})
    target = tmp_path / "home" / "skills"
    _copy_pack_skills(pack, target)
    report = _copy_pack_skills(pack, target)
    assert report.new == 0
    assert report.updated == 0
    assert report.unchanged == 1


def test_copy_pack_skills_updates_changed(tmp_path: Path):
    pack = _make_pack_skills_dir(tmp_path, {"skill-a": SKILL_A})
    target = tmp_path / "home" / "skills"
    _copy_pack_skills(pack, target)
    (pack / "skill-a" / "SKILL.md").write_text(SKILL_A + "\nExtra.")
    report = _copy_pack_skills(pack, target)
    assert report.updated == 1
    assert "Extra." in (target / "skill-a" / "SKILL.md").read_text()


def test_copy_pack_skills_prunes_removed(tmp_path: Path):
    pack = _make_pack_skills_dir(tmp_path, {"skill-a": SKILL_A, "skill-b": SKILL_B})
    target = tmp_path / "home" / "skills"
    _copy_pack_skills(pack, target)
    shutil.rmtree(pack / "skill-b")
    report = _copy_pack_skills(pack, target)
    assert report.removed == 1
    assert not (target / "skill-b").exists()


def test_copy_pack_skills_respects_pinned(tmp_path: Path):
    pack = _make_pack_skills_dir(tmp_path, {"skill-a": SKILL_A})
    target = tmp_path / "home" / "skills"
    _copy_pack_skills(pack, target)
    (target / "skill-a" / _PINNED).touch()
    (pack / "skill-a" / "SKILL.md").write_text(SKILL_A + "\nChanged.")
    report = _copy_pack_skills(pack, target)
    assert report.pinned == 1
    assert "Changed." not in (target / "skill-a" / "SKILL.md").read_text()


def test_copy_pack_skills_preserves_user_authored(tmp_path: Path):
    pack = _make_pack_skills_dir(tmp_path, {"skill-a": SKILL_A})
    target = tmp_path / "home" / "skills"
    user = target / "user-custom"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text("# Custom\nMine.")
    report = _copy_pack_skills(pack, target)
    # User dir has no skill-pack sentinel → never pruned.
    assert (user / "SKILL.md").exists()
    assert report.removed == 0


# ──────────────────────────────────────────────────────────────────
# Coexistence with sync_builtin_skills
# ──────────────────────────────────────────────────────────────────


def test_builtin_sync_leaves_skillpack_untouched(tmp_path: Path):
    """A skill-pack overlay must survive a subsequent built-in sync unchanged."""
    # Built-in source has skill-a; the pack overlays skill-a (richer copy) + skill-x.
    builtin_src = tmp_path / "built_in"
    (builtin_src / "skill-a").mkdir(parents=True)
    (builtin_src / "skill-a" / "SKILL.md").write_text(SKILL_A + "\nBUILTIN")

    home = tmp_path / "home"
    pack = _make_pack_skills_dir(tmp_path, {"skill-a": SKILL_A + "\nPACK", "skill-x": SKILL_B})
    _copy_pack_skills(pack, home / "skills")

    with patch("olano.skills.built_in_sync._builtin_source_dir", return_value=builtin_src):
        report = sync_builtin_skills(home)

    # skill-a stays the pack copy (not clobbered by the built-in version)...
    assert "PACK" in (home / "skills" / "skill-a" / "SKILL.md").read_text()
    assert "BUILTIN" not in (home / "skills" / "skill-a" / "SKILL.md").read_text()
    assert (home / "skills" / "skill-a" / _SKILLPACK).exists()
    assert not (home / "skills" / "skill-a" / _SENTINEL).exists()
    # ...skill-x (pack-only, not a built-in) is not pruned by the built-in sync.
    assert (home / "skills" / "skill-x").exists()
    # The built-in sync only iterates its own source skills, so it counts the
    # overlapping skill-a as "pinned" (hands-off); skill-x is silently preserved
    # by the removal pass (no built-in sentinel → never pruned).
    assert report.pinned == 1
    assert report.removed == 0


# ──────────────────────────────────────────────────────────────────
# fetch_olano_skills (git clone → copy)
# ──────────────────────────────────────────────────────────────────


def test_fetch_olano_skills_no_url(tmp_path: Path):
    # A disabled/blank repo URL is a deliberate skip (ok=True), never a fetch.
    with patch("olano.skills.sync._resolve_repo_url", return_value=None):
        ok, msg = fetch_olano_skills(tmp_path / "home")
    assert ok is True
    assert "skipping" in msg.lower()


@requires_git
def test_fetch_olano_skills_clone_and_copy(tmp_path: Path):
    repo = _make_git_skills_repo(tmp_path, {"skill-a": SKILL_A, "skill-b": SKILL_B})
    home = tmp_path / "home"
    ok, msg = fetch_olano_skills(home, url=str(repo))
    assert ok is True, msg
    # Clone cached under skill-packs/olano-skills; skills copied into common dir.
    assert (olano_skills_pack_dir(home) / ".git").exists()
    assert (common_skills_dir(home) / "skill-a" / "SKILL.md").exists()
    assert (common_skills_dir(home) / "skill-a" / _SKILLPACK).exists()
    assert (common_skills_dir(home) / "skill-b" / _SKILLPACK).exists()


@requires_git
def test_fetch_olano_skills_idempotent_pull(tmp_path: Path):
    repo = _make_git_skills_repo(tmp_path, {"skill-a": SKILL_A})
    home = tmp_path / "home"
    ok1, _ = fetch_olano_skills(home, url=str(repo))
    ok2, msg2 = fetch_olano_skills(home, url=str(repo))
    assert ok1 and ok2
    # Second run pulls (already cloned) and re-copies with no new skills.
    assert "unchanged" in msg2
    assert (common_skills_dir(home) / "skill-a" / _SKILLPACK).exists()


@requires_git
def test_fetch_olano_skills_full_stack_coexists(tmp_path: Path):
    """Built-in baseline then external overlay: overlap wins for the pack, extras kept."""
    builtin_src = tmp_path / "built_in"
    for name in ("skill-a", "shared"):
        (builtin_src / name).mkdir(parents=True)
        (builtin_src / name / "SKILL.md").write_text(SKILL_A + f"\nBUILTIN-{name}")

    repo = _make_git_skills_repo(
        tmp_path, {"shared": SKILL_A + "\nPACK-shared", "pack-only": SKILL_B}
    )
    home = tmp_path / "home"

    with patch("olano.skills.built_in_sync._builtin_source_dir", return_value=builtin_src):
        sync_builtin_skills(home)  # baseline: skill-a + shared (built-in)
    fetch_olano_skills(home, url=str(repo))  # overlay: shared (pack) + pack-only

    skills = common_skills_dir(home)
    assert (skills / "skill-a" / _SENTINEL).exists()  # built-in-only, kept
    assert "PACK-shared" in (skills / "shared" / "SKILL.md").read_text()  # pack wins
    assert (skills / "shared" / _SKILLPACK).exists()
    assert (skills / "pack-only" / _SKILLPACK).exists()  # pack extra present

    # Re-running the built-in sync must not clobber or prune the pack copies.
    with patch("olano.skills.built_in_sync._builtin_source_dir", return_value=builtin_src):
        sync_builtin_skills(home)
    assert "PACK-shared" in (skills / "shared" / "SKILL.md").read_text()
    assert (skills / "pack-only").exists()
