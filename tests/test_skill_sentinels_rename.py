"""Skill ownership markers are ``.olano-*``; the ``.lionclaw-*`` names stay readable.

A box upgraded in place has every synced skill marked with the old names. A
sync that stopped recognising them would treat pack skills as the owner's
(never updated again) and pinned skills as the pack's (overwritten), so both
generations are read everywhere, and a built-in or pack marker is re-written
under the current name the next time a sync touches its directory. A pin is
the owner's file and is only ever read.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from olano.skills.built_in_sync import (
    _ALL_SENTINEL_NAMES,
    _LEGACY_SENTINEL,
    _PINNED,
    _SENTINEL,
    _SKILLPACK,
    _dir_content_hash,
    is_pinned,
    is_skillpack,
    sync_builtin_skills,
)
from olano.skills.sync import _copy_pack_skills

SKILL = "---\nname: {n}\ncategory: workflow\ndescription: d.\n---\n# {n}\n{body}"


def _skill_dir(base: Path, name: str, body: str = "Body.") -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(SKILL.format(n=name, body=body))
    return d


def _patch_source(src: Path):
    return patch("olano.skills.built_in_sync._builtin_source_dir", return_value=src)


def test_current_names_and_the_full_set():
    assert _SENTINEL == ".olano-builtin"
    assert _SKILLPACK == ".olano-skillpack"
    assert _PINNED == ".olano-skill-pinned"
    assert _LEGACY_SENTINEL == ".lionclaw-builtin"
    assert {
        ".olano-builtin",
        ".olano-skillpack",
        ".olano-skill-pinned",
        ".lionclaw-builtin",
        ".lionclaw-bundled",
        ".lionclaw-skillpack",
        ".lionclaw-skill-pinned",
    } == set(_ALL_SENTINEL_NAMES)


def test_a_legacy_pin_keeps_both_syncs_out(tmp_path: Path):
    src = _skill_dir(tmp_path / "src", "skill-a", body="package copy")
    home = tmp_path / "home"
    mine = _skill_dir(home / "skills", "skill-a", body="my edits")
    (mine / ".lionclaw-skill-pinned").touch()
    assert is_pinned(mine)

    with _patch_source(src.parent):
        report = sync_builtin_skills(home)
    assert report.pinned == 1
    assert "my edits" in (mine / "SKILL.md").read_text()

    pack = _skill_dir(tmp_path / "pack", "skill-a", body="pack copy")
    report = _copy_pack_skills(pack.parent, home / "skills")
    assert report.pinned == 1
    assert "my edits" in (mine / "SKILL.md").read_text()
    # The owner's file is never rewritten, under either name.
    assert (mine / ".lionclaw-skill-pinned").exists()
    assert not (mine / _PINNED).exists()


def test_a_legacy_pack_marker_is_hands_off_for_builtin_sync_and_upgraded_by_pack_sync(
    tmp_path: Path,
):
    home = tmp_path / "home"
    synced = _skill_dir(home / "skills", "skill-a", body="pack copy")
    (synced / ".lionclaw-skillpack").touch()
    assert is_skillpack(synced)

    # Built-in sync sees a pack-owned dir and leaves it, whatever the package ships.
    src = _skill_dir(tmp_path / "src", "skill-a", body="package copy")
    with _patch_source(src.parent):
        report = sync_builtin_skills(home)
    assert report.pinned == 1
    assert "pack copy" in (synced / "SKILL.md").read_text()

    # Pack sync with identical content counts it unchanged and re-marks it.
    pack = _skill_dir(tmp_path / "pack", "skill-a", body="pack copy")
    report = _copy_pack_skills(pack.parent, home / "skills")
    assert report.unchanged == 1
    assert (synced / _SKILLPACK).exists()
    assert not (synced / ".lionclaw-skillpack").exists()

    # And a pack skill that left the pack is still pruned under the old name.
    stale = _skill_dir(home / "skills", "gone", body="old")
    (stale / ".lionclaw-skillpack").touch()
    report = _copy_pack_skills(pack.parent, home / "skills")
    assert report.removed == 1
    assert not stale.exists()


def test_a_legacy_builtin_marker_is_upgraded_and_still_counts_as_managed(tmp_path: Path):
    src_root = tmp_path / "src"
    _skill_dir(src_root, "skill-a")
    home = tmp_path / "home"
    same = _skill_dir(home / "skills", "skill-a")
    (same / ".lionclaw-bundled").touch()
    stale = _skill_dir(home / "skills", "retired")
    (stale / ".lionclaw-builtin").touch()
    theirs = _skill_dir(home / "skills", "theirs", body="hand-written")

    with _patch_source(src_root):
        report = sync_builtin_skills(home)

    assert report.unchanged == 1
    assert (same / _SENTINEL).exists()
    assert not (same / ".lionclaw-bundled").exists()
    assert report.removed == 1 and not stale.exists()
    # An unmarked directory is the owner's: untouched and never removed.
    assert theirs.is_dir() and not (theirs / _SENTINEL).exists()


def test_content_hash_ignores_every_marker_generation(tmp_path: Path):
    plain = _skill_dir(tmp_path / "plain", "s")
    marked = _skill_dir(tmp_path / "marked", "s")
    for name in _ALL_SENTINEL_NAMES:
        (marked / name).touch()
    assert _dir_content_hash(plain) == _dir_content_hash(marked)
