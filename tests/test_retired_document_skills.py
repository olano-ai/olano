"""The four document skills Olano used to bundle were Anthropic's, licensed
proprietary. They left ``olano/skills/built_in/`` in 0.3.1; the catalog
(olano-ai/skills) serves Olano-authored skills under the same names.

What these tests pin, on a box that already holds the old copies:

* the package no longer ships them, and no built-in skill carries a license
  that forbids redistribution;
* every sync-managed Anthropic copy goes (built-in sync, the pack overlay, the
  startup sweep), including one a stale pack on disk would put back;
* nothing the owner holds is touched: a skill with no sync marker, a pinned
  copy, or the Olano-authored replacement under the same name.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from olano.skills import built_in_sync
from olano.skills.built_in_sync import (
    _LEGACY_SENTINELS,
    _PINNED,
    _SENTINEL,
    _SKILLPACK,
    _builtin_source_dir,
    is_retired_proprietary_copy,
    sync_builtin_skills,
)
from olano.skills.sync import _copy_pack_skills, retire_proprietary_skill_copies

DOC_SKILLS = ("docx", "pdf", "pptx", "xlsx")
ANTHROPIC_LICENSE = (
    "© 2025 Anthropic, PBC. All rights reserved.\n\n"
    "LICENSE: Use of these materials ... is governed by your agreement with Anthropic.\n"
)
OLANO_LICENSE = "Olano AI Copyrights\nCopyright (c) 2026 Olano AI. All rights reserved.\n"


def _skill(root: Path, name: str, *, license: str | None = None, markers: tuple = ()) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}.\n---\n# {name}\n")
    if license is not None:
        (d / "LICENSE.txt").write_text(license)
    for marker in markers:
        (d / marker).touch()
    return d


def _names(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def test_the_package_no_longer_ships_the_document_skills() -> None:
    source = _builtin_source_dir()
    for name in DOC_SKILLS:
        assert not (source / name).exists(), name


def test_no_built_in_skill_carries_a_license_that_forbids_redistribution() -> None:
    """The repository is MIT; a bundled skill ships in the wheel and the images."""
    for license_file in sorted(_builtin_source_dir().glob("*/LICENSE*")):
        text = license_file.read_text(encoding="utf-8", errors="replace")
        assert "Anthropic, PBC. All rights reserved" not in text, license_file
        assert "Olano AI Copyrights" not in text, license_file


@pytest.mark.parametrize(
    ("name", "license", "expected"),
    [
        ("docx", ANTHROPIC_LICENSE, True),
        ("xlsx", ANTHROPIC_LICENSE, True),
        ("docx", OLANO_LICENSE, False),  # the replacement
        ("pdf", None, False),  # an operator's own skill
        ("canvas-design", ANTHROPIC_LICENSE, False),  # only the four retired names
    ],
)
def test_retired_copy_is_recognized_by_name_and_license(
    tmp_path: Path, name: str, license: str | None, expected: bool
) -> None:
    assert is_retired_proprietary_copy(_skill(tmp_path, name, license=license)) is expected


def test_built_in_sync_removes_its_old_copies_and_keeps_the_owners(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skills = home / "skills"
    source = tmp_path / "built_in"
    _skill(source, "plan")
    _skill(skills, "plan", markers=(_SENTINEL,))
    _skill(skills, "docx", license=ANTHROPIC_LICENSE, markers=(_SENTINEL,))
    _skill(skills, "pptx", license=ANTHROPIC_LICENSE, markers=(_LEGACY_SENTINELS[0],))
    _skill(skills, "xlsx", license=ANTHROPIC_LICENSE, markers=(_SENTINEL, _PINNED))
    _skill(skills, "pdf", license="mine\n")  # no marker: the owner's

    with patch.object(built_in_sync, "_builtin_source_dir", return_value=source):
        report = sync_builtin_skills(home)

    assert report.removed == 2
    assert _names(skills) == ["pdf", "plan", "xlsx"]
    assert (skills / "pdf" / "LICENSE.txt").read_text() == "mine\n"


def test_a_pinned_skill_survives_its_removal_from_the_package(tmp_path: Path) -> None:
    """Before: the removal pass deleted a pinned dir that still carried a
    built-in marker, though the pin promises "no sync may touch it"."""
    home = tmp_path / "home"
    source = tmp_path / "built_in"
    source.mkdir()
    _skill(home / "skills", "old-skill", markers=(_SENTINEL, _PINNED))

    with patch.object(built_in_sync, "_builtin_source_dir", return_value=source):
        report = sync_builtin_skills(home)

    assert report.removed == 0
    assert (home / "skills" / "old-skill" / "SKILL.md").exists()


def test_pack_overlay_replaces_the_old_copy_with_the_catalogs(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    skills = tmp_path / "skills"
    _skill(pack, "docx", license=OLANO_LICENSE)
    _skill(skills, "docx", license=ANTHROPIC_LICENSE, markers=(_SKILLPACK,))
    (skills / "docx" / "ooxml.xsd").write_text("<old/>")

    report = _copy_pack_skills(pack, skills)

    assert report.updated == 1
    assert (skills / "docx" / "LICENSE.txt").read_text() == OLANO_LICENSE
    assert not (skills / "docx" / "ooxml.xsd").exists()
    assert (skills / "docx" / _SKILLPACK).exists()


def test_a_stale_pack_on_disk_cannot_put_the_old_copy_back(tmp_path: Path) -> None:
    """A clone whose pull fails, or a catalog cache while the cloud is down, is
    still overlaid; before, it reinstalled the Anthropic copy every time."""
    pack = tmp_path / "pack"
    skills = tmp_path / "skills"
    _skill(pack, "docx", license=ANTHROPIC_LICENSE)
    _skill(pack, "charts")
    _skill(skills, "docx", license=ANTHROPIC_LICENSE, markers=(_SKILLPACK,))

    report = _copy_pack_skills(pack, skills)

    assert report.removed == 1
    assert _names(skills) == ["charts"]

    # And on a box that never had it, it is not installed in the first place.
    fresh = tmp_path / "fresh"
    _copy_pack_skills(pack, fresh)
    assert _names(fresh) == ["charts"]


def test_pack_prune_leaves_a_pinned_skill_alone(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    skills = tmp_path / "skills"
    _skill(pack, "charts")
    _skill(skills, "withdrawn", markers=(_SKILLPACK, _PINNED))

    report = _copy_pack_skills(pack, skills)

    assert report.removed == 0
    assert "withdrawn" in _names(skills)


def test_startup_sweep_removes_managed_copies_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skills = home / "skills"
    _skill(skills, "docx", license=ANTHROPIC_LICENSE, markers=(_SKILLPACK,))
    _skill(skills, "pdf", license=ANTHROPIC_LICENSE, markers=(_SENTINEL,))
    _skill(skills, "pptx", license=ANTHROPIC_LICENSE, markers=(_SKILLPACK, _PINNED))
    _skill(skills, "xlsx", license=ANTHROPIC_LICENSE)  # unmarked: the owner's copy
    _skill(skills, "charts", markers=(_SKILLPACK,))

    assert retire_proprietary_skill_copies(home) == ["docx", "pdf"]
    assert _names(skills) == ["charts", "pptx", "xlsx"]
    # Idempotent, and quiet on a home with no skills dir.
    assert retire_proprietary_skill_copies(home) == []
    assert retire_proprietary_skill_copies(tmp_path / "nowhere") == []


def test_startup_upgrade_runs_the_sweep(tmp_path: Path) -> None:
    from olano.update import auto

    home = tmp_path / "home"
    _skill(home / "skills", "docx", license=ANTHROPIC_LICENSE, markers=(_SKILLPACK,))
    with (
        patch.object(auto, "run_pending_migrations", return_value=[]),
        patch.object(auto, "refresh_all_agents", return_value=0),
    ):
        auto.run_startup_upgrades(home)
    assert not (home / "skills" / "docx").exists()
