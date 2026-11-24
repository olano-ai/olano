"""Sync built-in skills from the package into <home>/skills/ (managed_skills_dir)."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

_SENTINEL = ".olano-builtin"
_PINNED = ".olano-skill-pinned"
# Marks a skill copied from an external skill pack (olano-ai/skills) by
# olano/skills/sync.py. Built-in sync treats these dirs as hands-off (like
# pinned) so the package baseline and the richer external overlay coexist
# regardless of call order. See sync.py:fetch_olano_skills.
_SKILLPACK = ".olano-skillpack"
# The names the same three markers had before the rename. Every reader accepts
# them: a box upgraded in place has every synced skill marked with the old
# names, and a sync that stopped recognising them would treat pack skills as
# the owner's (never updated again) and pinned skills as the pack's
# (overwritten). A built-in or pack marker is re-written under the current
# name the next time a sync touches its directory; a pin is the owner's file
# and is never rewritten, only read.
_LEGACY_SENTINELS = (".lionclaw-builtin", ".lionclaw-bundled")
_LEGACY_SKILLPACKS = (".lionclaw-skillpack",)
_LEGACY_PINNED = (".lionclaw-skill-pinned",)
# Older callers import the single legacy built-in name.
_LEGACY_SENTINEL = _LEGACY_SENTINELS[0]
# Every ownership/sync marker, current and legacy: excluded from content hashes
# and never copied into a fork.
_ALL_SENTINEL_NAMES = frozenset(
    {_SENTINEL, _SKILLPACK, _PINNED, *_LEGACY_SENTINELS, *_LEGACY_SKILLPACKS, *_LEGACY_PINNED}
)

# Until 0.3.1 the package bundled Anthropic's four document skills under these
# names, licensed proprietary ("(c) 2025 Anthropic, PBC. All rights reserved.")
# with no right to redistribute. They left `built_in/`, and the catalog
# (olano-ai/skills) now serves Olano-authored skills under the same names. The
# removal pass below deletes the built-in copies like any withdrawn built-in,
# but a copy can also come back from a pack on disk that has not refreshed (a
# clone whose pull fails, a catalog cache while the cloud is unreachable), so
# the pack overlay and the startup hook ask `is_retired_proprietary_copy` too.
# Matched by name AND license, so an operator's own skill called `pdf`, or
# the Olano-authored replacement, is never caught by it.
_RETIRED_PROPRIETARY_NAMES = frozenset({"docx", "pdf", "pptx", "xlsx"})
_RETIRED_PROPRIETARY_LICENSE_MARK = "Anthropic, PBC. All rights reserved."


def _any_present(path: Path, names: tuple[str, ...]) -> bool:
    return any((path / name).exists() for name in names)


def is_pinned(path: Path) -> bool:
    """The owner pinned this skill directory: no sync may touch it."""
    return _any_present(path, (_PINNED, *_LEGACY_PINNED))


def is_skillpack(path: Path) -> bool:
    """This skill directory was copied from an external skill pack."""
    return _any_present(path, (_SKILLPACK, *_LEGACY_SKILLPACKS))


def _has_managed_sentinel(path: Path) -> bool:
    return _any_present(path, (_SENTINEL, *_LEGACY_SENTINELS))


def is_retired_proprietary_copy(path: Path) -> bool:
    """``path`` is one of Anthropic's proprietary document skills Olano once shipped."""
    if path.name not in _RETIRED_PROPRIETARY_NAMES:
        return False
    try:
        head = (path / "LICENSE.txt").read_text(encoding="utf-8", errors="replace")[:500]
    except OSError:
        return False
    return _RETIRED_PROPRIETARY_LICENSE_MARK in head


def _mark(path: Path, current: str, legacy: tuple[str, ...]) -> None:
    (path / current).touch()
    for name in legacy:
        try:
            (path / name).unlink(missing_ok=True)
        except OSError:
            pass


def mark_builtin(path: Path) -> None:
    """Mark ``path`` as the package's built-in copy, replacing a legacy marker."""
    _mark(path, _SENTINEL, _LEGACY_SENTINELS)


def mark_skillpack(path: Path) -> None:
    """Mark ``path`` as owned by the external skill pack, replacing a legacy marker."""
    _mark(path, _SKILLPACK, _LEGACY_SKILLPACKS)


@dataclass(frozen=True)
class SyncReport:
    new: int
    updated: int
    unchanged: int
    removed: int
    pinned: int

    def __str__(self) -> str:
        return (
            f"Skills: {self.new} new, {self.updated} updated, "
            f"{self.unchanged} unchanged, {self.removed} removed, {self.pinned} pinned."
        )

    @property
    def total_builtin(self) -> int:
        return self.new + self.updated + self.unchanged + self.pinned

    @property
    def total_bundled(self) -> int:
        """Backward-compatible alias for older callers."""
        return self.total_builtin


def _builtin_source_dir() -> Path:
    """Return the built-in skills directory shipped with the package."""
    try:
        from importlib.resources import files

        ref = files("olano.skills.built_in")
        # files() may return a Traversable; convert to Path for fs ops.
        return Path(str(ref._path if hasattr(ref, "_path") else ref))
    except Exception:
        # Fallback for editable installs or older Python.
        return Path(__file__).parent / "built_in"


def _dir_content_hash(directory: Path) -> str:
    """Compute a stable hash over all files in a directory (sorted by relpath)."""
    h = hashlib.sha256()
    for fpath in sorted(directory.rglob("*")):
        if fpath.name in _ALL_SENTINEL_NAMES or not fpath.is_file():
            continue
        rel = fpath.relative_to(directory).as_posix()
        h.update(rel.encode())
        h.update(b"\x00")
        h.update(fpath.read_bytes())
    return h.hexdigest()


def _list_builtin_skills(source: Path) -> list[Path]:
    """Return subdirectories of source that contain a SKILL.md."""
    if not source.exists():
        return []
    return sorted(d for d in source.iterdir() if d.is_dir() and (d / "SKILL.md").exists())


def sync_builtin_skills(home: Path) -> SyncReport:
    """
    Copy built-in skills into <home>/skills/, skipping unchanged content.

    Removal only touches dirs that carry a Olano managed-skill sentinel.
    Pinned dirs (``.olano-skill-pinned``, or its pre-rename name) are left
    completely untouched, including by removal: a pin on a built-in copy the
    package no longer ships is the owner keeping it.
    """
    source = _builtin_source_dir()
    target_root = home / "skills"
    target_root.mkdir(parents=True, exist_ok=True)

    builtin_names: set[str] = set()
    new = updated = unchanged = pinned = 0

    for src_skill in _list_builtin_skills(source):
        name = src_skill.name
        builtin_names.add(name)
        dst_skill = target_root / name

        # Leave user-pinned dirs and externally-managed skill-pack dirs untouched.
        # A skill-pack overlay (olano-ai/skills) is the richer, canonical copy of a
        # built-in skill, so the package baseline must not clobber it here — nor may
        # the removal pass below delete it (skill-pack dirs carry no built-in sentinel).
        if dst_skill.exists() and (is_pinned(dst_skill) or is_skillpack(dst_skill)):
            pinned += 1
            continue

        src_hash = _dir_content_hash(src_skill)

        if not dst_skill.exists():
            shutil.copytree(src_skill, dst_skill)
            mark_builtin(dst_skill)
            new += 1
        else:
            dst_hash = _dir_content_hash(dst_skill)
            if src_hash == dst_hash:
                # An identical copy under a legacy marker is re-marked under the
                # current name; an identical copy with no marker at all is the
                # owner's and stays unmarked (so removal never touches it).
                if (
                    _any_present(dst_skill, _LEGACY_SENTINELS)
                    and not (dst_skill / _SENTINEL).exists()
                ):
                    mark_builtin(dst_skill)
                unchanged += 1
            else:
                shutil.rmtree(dst_skill)
                shutil.copytree(src_skill, dst_skill)
                mark_builtin(dst_skill)
                updated += 1

    # Remove stale built-in skills that were previously managed by Olano.
    removed = 0
    for dst_skill in sorted(target_root.iterdir()):
        if (
            dst_skill.is_dir()
            and dst_skill.name not in builtin_names
            and _has_managed_sentinel(dst_skill)
            and not is_pinned(dst_skill)
        ):
            shutil.rmtree(dst_skill)
            removed += 1

    return SyncReport(new=new, updated=updated, unchanged=unchanged, removed=removed, pinned=pinned)


sync_bundled_skills = sync_builtin_skills
