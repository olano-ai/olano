"""Keep a skill's ``name`` and the folder holding its ``SKILL.md`` in agreement.

The Agent Skills spec requires them to be equal (``loader.skill_name_violation``
is the predicate, pinned to deepagents' own). Olano's code writers already
comply by construction (``admin.skills.create_skill`` / ``install_skill``, the
self-learning writer), but most skills on a busy box are written by a **model**:
an agent following the skill-creator skill in chat, the Skillsmith, the skill
librarian, a Cortex execution run. Each of them composes the frontmatter itself,
and the shape they get wrong is always the same one: a human title in ``name:``
(``Olano Professional HTML Email Formatting``) inside a folder that is its slug
(``olano-professional-html-email-formatting/``). The repair could not fix that
shape (it only ever renamed the folder to the name, and an illegal name has no
legal folder), so the boot alert came back on every restart for months.

This module is the one place that decides what a mismatch means and what the
fix is, so the four callers cannot disagree:

* ``admin.skills.repair_skill_names`` (the Repair button) applies every fix;
* the security middleware applies it as the file is written (``write_file`` is
  checked before the write, ``edit_file`` after, a shell command that names
  ``SKILL.md`` gets a sweep);
* the agent boot and every cognition worker run sweep with
  :func:`align_skill_names`, which applies **only** :data:`REWRITE_NAME`, the
  fix that moves no path.

The fixes, by case:

* the name is legal and differs from the folder: **rename the folder** to the
  name. The name is what the prompt, ``delegate_task`` and the shadow dedupe
  refer to; the folder is re-resolved on every boot.
* the name is illegal and the folder is legal: **rewrite the name** to the
  folder. The illegal name was never usable as an identity, and the folder is
  almost always its slug already.
* both are illegal: **rename the folder and rewrite the name** to the slug of
  the name.

A learned fork (``.lc-provenance.json``) is never touched by any of them: its
name must stay the original's to shadow it, and its manifest records absolute
paths a folder rename would strand.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from olano.skills.loader import (
    MAX_SKILL_NAME_LENGTH,
    _read_frontmatter,
    discover_skills,
    skill_name_violation,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
FORK_MANIFEST = ".lc-provenance.json"

RENAME_FOLDER = "rename_folder"
REWRITE_NAME = "rewrite_name"
RENAME_BOTH = "rename_both"


def skill_slug(text: str) -> str:
    """A spec-legal skill name derived from *text* (``""`` when nothing survives).

    Lowercase ASCII letters and digits joined by single hyphens, capped at the
    spec's 64 characters: ``Olano Professional HTML Email Formatting`` becomes
    ``olano-professional-html-email-formatting``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:MAX_SKILL_NAME_LENGTH].rstrip("-")


def is_legal_name(name: str) -> bool:
    """True when *name* has the shape the spec allows (folder aside)."""
    return skill_name_violation(name, name) is None


@dataclass(frozen=True)
class NameFix:
    """What it takes to make one skill comply.

    ``action`` is :data:`RENAME_FOLDER`, :data:`REWRITE_NAME` or
    :data:`RENAME_BOTH`; ``target`` is the compliant name (and folder) after it.
    """

    action: str
    name: str
    folder: str
    target: str


def plan_name_fix(name: str, folder: str) -> NameFix | None:
    """The fix for a skill named *name* in a folder called *folder*.

    ``None`` when it already complies, or when nothing legal can be derived
    from either string (a name of only punctuation in a folder of the same).
    """
    if skill_name_violation(name, folder) is None:
        return None
    if is_legal_name(name):
        return NameFix(RENAME_FOLDER, name, folder, name)
    if is_legal_name(folder):
        return NameFix(REWRITE_NAME, name, folder, folder)
    slug = skill_slug(name) or skill_slug(folder)
    if not slug:
        return None
    return NameFix(RENAME_BOTH, name, folder, slug)


def frontmatter_name(text: str) -> str | None:
    """The ``name`` a SKILL.md's frontmatter declares, or ``None`` when it has none.

    Parsed the way ``discover_skills`` parses it, so a quoted value reads the
    same here as it does to the loader.
    """
    fm = _read_frontmatter(text.strip())
    if not fm or "name" not in fm or fm.get("name") is None:
        return None
    return str(fm["name"]).strip() or None


def with_frontmatter_name(text: str, new_name: str) -> str | None:
    """*text* with its top-level frontmatter ``name:`` line set to *new_name*.

    ``None`` when there is no frontmatter block or no top-level ``name:`` line
    in it. Only that one line changes: a ``name:`` in the body, or nested under
    another key (``metadata:``), is left alone.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return None
        if not line.startswith("name:"):
            continue
        ending = "\n" if line.endswith("\n") else ""
        lines[i] = f"name: {new_name}{ending}"
        return "".join(lines)
    return None


def set_frontmatter_name(skill_md: Path, new_name: str) -> str | None:
    """Rewrite *skill_md*'s frontmatter ``name`` to *new_name*; return the old name.

    ``None`` when nothing changed: the name already matched, the file has no
    ``name:`` line, or it could not be read or written (a cosmetic fix must
    never fail the caller). The file is rewritten in place, which keeps its
    owner and mode: on a managed box the sandbox user owns it, and a fresh
    inode written by the runtime would take it away from the agent.
    """
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    current = frontmatter_name(raw)
    if current is None or current == new_name:
        return None
    updated = with_frontmatter_name(raw, new_name)
    if updated is None:
        return None
    try:
        skill_md.write_text(updated, encoding="utf-8")
    except OSError:
        return None
    return current


def is_fork(skill_dir: Path) -> bool:
    return (skill_dir / FORK_MANIFEST).exists()


@dataclass(frozen=True)
class WriteCheck:
    """The verdict on a ``write_file`` of a SKILL.md, taken before the write.

    Exactly one of the three is meaningful: ``refusal`` (do not write; tell the
    model why and where to write instead), ``content`` (write this instead, the
    name aligned with the folder) with ``note`` saying so, or neither (write as
    asked).
    """

    refusal: str | None = None
    content: str | None = None
    note: str | None = None


def check_skill_write(path: Path, content: str, existing_name: str | None = None) -> WriteCheck:
    """Judge a ``write_file`` of *content* to *path* before it happens.

    Only a file named ``SKILL.md`` whose frontmatter declares a name is judged.
    A name the folder can simply absorb is rewritten in the content and the
    model is told; a mismatch that needs a different path is refused with the
    path to use, because the write has not happened yet and the model can make
    it correctly on the next step.

    *existing_name* is the name the file on disk declares now. A rewrite that
    keeps a mismatch the skill already had is not refused (refusing would stop
    an agent maintaining its own skill over a problem it did not cause this
    turn); it goes through with a note naming the fix, which Repair applies.
    """
    if path.name != SKILL_FILENAME or is_fork(path.parent):
        return WriteCheck()
    name = frontmatter_name(content)
    if name is None:
        return WriteCheck()
    fix = plan_name_fix(name, path.parent.name)
    if fix is None:
        return WriteCheck()
    if fix.action == REWRITE_NAME:
        updated = with_frontmatter_name(content, fix.target)
        if updated is None:
            return WriteCheck()
        return WriteCheck(content=updated, note=_rewrite_note(fix))
    if existing_name is not None and existing_name == name:
        return WriteCheck(note=_folder_move_note(fix, path))
    target_dir = path.parent.parent / fix.target
    target = target_dir / SKILL_FILENAME
    taken = target_dir.exists()
    if fix.action == RENAME_FOLDER:
        refusal = (
            f"A skill's `name` must equal the folder holding its SKILL.md (Agent Skills "
            f"spec). This writes `name: {fix.name}` into the folder `{fix.folder}/`. "
            + (
                f"A skill already lives in `{target_dir}/`, so keep this folder and set "
                f"`name: {fix.folder}`."
                if taken
                else f"Write it to {target} instead, or keep the folder and set "
                f"`name: {fix.folder}`."
            )
        )
    else:
        refusal = (
            f"`{fix.folder}` is not a legal skill folder name: a skill's `name` must be "
            f"lowercase letters, digits and single hyphens, and must equal its folder. "
            + (
                f"`{target_dir}/` is taken, so pick another such name and use it for both."
                if taken
                else f"Write this skill to {target} with `name: {fix.target}`."
            )
        )
    return WriteCheck(refusal=refusal)


def check_skill_file(skill_md: Path) -> str | None:
    """Align an existing SKILL.md after an edit; return a note for the model.

    The edit has already happened, so nothing can be refused: a name the folder
    can absorb is rewritten in place (and the note says so), and a mismatch that
    needs a folder rename is reported with the exact fix. ``None`` when the
    skill complies or the path is not a skill.
    """
    if skill_md.name != SKILL_FILENAME or is_fork(skill_md.parent):
        return None
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    name = frontmatter_name(raw)
    if name is None:
        return None
    fix = plan_name_fix(name, skill_md.parent.name)
    if fix is None:
        return None
    if fix.action == REWRITE_NAME:
        if set_frontmatter_name(skill_md, fix.target) is None:
            return None
        return _rewrite_note(fix)
    return _folder_move_note(fix, skill_md)


def _folder_move_note(fix: NameFix, skill_md: Path) -> str:
    target_dir = skill_md.parent.parent / fix.target
    if fix.action == RENAME_FOLDER:
        remedy = f"Move the folder to `{target_dir}/`, or set `name: {fix.folder}`."
    else:
        remedy = f"Move the folder to `{target_dir}/` and set `name: {fix.target}`."
    return (
        f"[skill-name] This skill's `name: {fix.name}` does not match its folder "
        f"`{fix.folder}/`, which the Agent Skills spec requires. {remedy}"
    )


def _rewrite_note(fix: NameFix) -> str:
    return (
        f"[skill-name] Set this skill's `name` to `{fix.target}` (was `{fix.name}`): a "
        f"skill's name must equal its folder and be lowercase letters, digits and single "
        f"hyphens. Refer to the skill as `{fix.target}` from now on."
    )


def align_skill_names(roots: Iterable[Path | str]) -> list[dict[str, str]]:
    """Rewrite every illegal skill name under *roots* to its (legal) folder name.

    The backstop for writes no tool sees (a shell heredoc, a script), run at
    agent boot and after every cognition worker. It applies **only**
    :data:`REWRITE_NAME`: no path moves, so nothing a running agent or a
    script holds can go stale. Folder renames stay with the Repair action,
    where an operator asked for them. Forks are skipped. Never raises.

    Returns one ``{"path", "from", "to"}`` row per skill it rewrote.
    """
    rewritten: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        try:
            skills = discover_skills(Path(root))
        except Exception:
            logger.debug("Skill name sweep could not read %s", root, exc_info=True)
            continue
        for skill in skills:
            location = str(skill.get("location") or "")
            if not skill.get("spec_error") or location in seen:
                continue
            seen.add(location)
            skill_md = Path(location)
            if is_fork(skill_md.parent):
                continue
            fix = plan_name_fix(str(skill.get("name") or ""), skill_md.parent.name)
            if fix is None or fix.action != REWRITE_NAME:
                continue
            old = set_frontmatter_name(skill_md, fix.target)
            if old is None:
                continue
            logger.info(
                "Skill name aligned with its folder: %r -> %r (%s)", old, fix.target, skill_md
            )
            rewritten.append({"path": str(skill_md.parent), "from": old, "to": fix.target})
    return rewritten
