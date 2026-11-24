"""Read-only browsing of a skill's own folder — SKILL.md plus the scripts,
references, forms and assets that ship beside it.

The dashboard's skills list only ever carried a name and a one-line
description, so reading what a skill actually tells the agent to do meant SSH.
The Files view cannot close that gap either: **managed skills live in
``<home>/skills``, outside every agent workspace**, and ``/api/files`` refuses
anything resolving out of the workspace root — deliberately, since that check
is what stops it enumerating ``/app``, ``/opt/olano`` and the home tree.

So this module answers the three questions the Files endpoints cannot, and
answers them only for paths the *agent itself* may already read
(:func:`olano.skills.loader.skill_read_roots` — the same dirs deepagents is
handed, whose ``SKILL.md`` locations the system prompt advertises):

- which skill does this ``location`` name, and may this caller reach it
  (:func:`resolve_skill`);
- which files does it bundle (:func:`list_skill_files`, :func:`read_skill_file`);
- and can the dashboard edit it — i.e. does it sit inside the workspace the
  Files endpoints govern (``SkillRef.workspace_path`` / ``editable``).

**Nothing here writes.** Editing stays with ``/api/files``, which owns the write
boundary; a skill outside the workspace is reported read-only rather than handed
a second, weaker write path.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace

# Where a skill lives, which is also what decides whether it can be edited.
# `managed` is the common pack every agent shares (``<home>/skills``);
# `workspace` and `learned` belong to one agent's workspace; `configured` is an
# extra directory named by ``agent.json5 → skills``, which may be anywhere.
SCOPE_MANAGED = "managed"
SCOPE_WORKSPACE = "workspace"
SCOPE_LEARNED = "learned"
SCOPE_CONFIGURED = "configured"

# A skill folder is a handful of files; these caps exist so a folder that is
# secretly a checkout (someone dropped a repo under ``skills/``) cannot turn one
# dashboard click into an unbounded walk or response.
MAX_ENTRIES = 400
MAX_DEPTH = 6
# Text preview cap. Bigger files still open — the response is marked truncated
# and the UI says so — but the browser is never handed megabytes of a payload.
MAX_TEXT_BYTES = 512 * 1024

# Pruned from the walk: caches and vendored trees are never part of what a
# skill teaches, and they are exactly what would blow the entry cap.
_SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", ".mypy_cache", ".pytest_cache"}

_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
# Everything a browser can render as text in the preview pane. Anything else is
# reported `binary` and offered through the raw endpoint instead.
_TEXT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".json",
    ".json5",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".tsv",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".html",
    ".htm",
    ".css",
    ".xml",
    ".rst",
    ".env",
    ".gitignore",
    ".olanoignore",
    ".lionclawignore",
}


class SkillAccessError(Exception):
    """The requested path is not a skill this caller may read."""


class SkillNotFoundError(Exception):
    """The skill (or a file inside it) is not on disk."""


@dataclass(frozen=True)
class SkillRef:
    """A resolved, access-checked skill folder."""

    name: str
    directory: Path
    skill_md: Path
    scope: str
    source_dir: Path
    # Workspace-relative path of the folder, or ``None`` when the skill lives
    # outside the workspace — which is also the Files view's deep link, since
    # that view addresses everything workspace-relative.
    workspace_path: str | None

    @property
    def editable(self) -> bool:
        return is_editable(self.scope, self.workspace_path)


def is_editable(scope: str | None, workspace_path: str | None) -> bool:
    """Whether the dashboard offers to edit a skill in *scope* at *workspace_path*.

    One definition, asked by both the list badges and the detail view, so the
    two can never disagree about a skill. Two conditions, and the second is a
    policy rather than a mechanism:

    - the skill must sit inside the workspace, because ``/api/files`` — the one
      write path the dashboard has — refuses everything that resolves outside
      it;
    - and it must not belong to the **managed pack**, which every agent on the
      box shares: an agent-scoped page must not offer a fleet-wide edit under
      one agent's name.

    On a normal install the two agree anyway (``<home>/skills`` is a sibling of
    the workspace, not a child). Stating the pack rule outright is what keeps
    the badge from flipping to "editable" on a box whose home happens to sit
    inside a workspace.
    """
    return workspace_path is not None and scope != SCOPE_MANAGED


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - defensive (broken symlink chain)
        return path


def browse_roots(config: AgentConfig | None, workspace: Workspace) -> list[tuple[Path, str]]:
    """The ordered ``(directory, scope)`` pairs a caller may browse.

    Mirrors :func:`olano.skills.loader._skill_source_dirs` — the dirs handed to
    deepagents — so the dashboard can never open a folder the agent itself is
    not already reading from. ``config`` is ``None`` for the unscoped listing
    (no ``?agent=``), which sees only the managed pack and the default
    workspace, exactly like ``GET /api/catalog/skills`` without an agent.

    Duplicates collapse to their **last** occurrence so a ``config.skills``
    entry that merely re-names ``<workspace>/skills`` is still labelled
    ``workspace``, matching the loader's last-source-wins ordering.
    """
    ordered: list[tuple[Path, str]] = []
    if config is not None:
        for entry in config.skills:
            candidate = Path(entry)
            if not candidate.is_absolute():
                candidate = workspace.root / candidate
            ordered.append((candidate, SCOPE_CONFIGURED))
    ordered.append((workspace.managed_skills_dir, SCOPE_MANAGED))
    ordered.append((workspace.skills_dir, SCOPE_WORKSPACE))
    ordered.append((workspace.learned_skills_dir, SCOPE_LEARNED))

    by_resolved: dict[Path, tuple[Path, str]] = {}
    for directory, scope in ordered:
        if not directory.exists():
            continue
        by_resolved[_resolved(directory)] = (directory, scope)
    return list(by_resolved.values())


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def classify(skill_dir: Path, roots: list[tuple[Path, str]]) -> tuple[Path, str] | None:
    """The ``(source_dir, scope)`` owning *skill_dir*, or ``None`` if outside all.

    The **deepest** containing root wins, which is what tells a learned skill
    from a plain workspace one: ``<ws>/skills/learned`` is nested inside
    ``<ws>/skills``, so a shallowest-wins match would label every learned skill
    ``workspace``.
    """
    resolved = _resolved(skill_dir)
    best: tuple[Path, str] | None = None
    best_depth = -1
    for directory, scope in roots:
        root = _resolved(directory)
        if not _contains(root, resolved):
            continue
        depth = len(root.parts)
        if depth > best_depth:
            best, best_depth = (directory, scope), depth
    return best


def workspace_relative(directory: Path, workspace: Workspace) -> str | None:
    """*directory* as a workspace-relative POSIX path, or ``None`` if outside.

    Tried lexically first: a ``shared/<name>`` projection is a symlink that
    resolves out of the workspace, yet the Files view addresses it by the
    workspace-relative path it was reached through, so resolving first would
    call an editable skill read-only.
    """
    for base, target in (
        (workspace.root, directory),
        (_resolved(workspace.root), _resolved(directory)),
    ):
        try:
            rel = target.relative_to(base)
        except ValueError:
            continue
        return rel.as_posix()
    return None


def resolve_skill(location: str, config: AgentConfig | None, workspace: Workspace) -> SkillRef:
    """Resolve a ``SKILL.md`` *location* to an access-checked :class:`SkillRef`.

    *location* is the absolute path ``GET /api/catalog/skills`` hands out. It is
    caller-supplied, so it is checked, not trusted: it must name a file called
    ``SKILL.md`` (nothing else is a skill, and accepting any filename would turn
    this into a general file reader) sitting under one of :func:`browse_roots`.

    Raises :class:`SkillAccessError` outside those roots and
    :class:`SkillNotFoundError` when the folder or its SKILL.md is gone.
    """
    raw = (location or "").strip()
    if not raw:
        raise SkillAccessError("location is required")
    skill_md = Path(raw)
    if skill_md.name != "SKILL.md":
        raise SkillAccessError("location must point at a SKILL.md file")

    skill_dir = skill_md.parent
    roots = browse_roots(config, workspace)
    owner = classify(skill_dir, roots)
    if owner is None:
        raise SkillAccessError("skill is outside this agent's skill directories")
    source_dir, scope = owner
    # A root is browsable, but the root *itself* is not a skill: refusing this
    # keeps `location=<root>/SKILL.md` from listing every skill's files at once.
    if _resolved(skill_dir) == _resolved(source_dir):
        raise SkillAccessError("location must point inside a skill folder")
    if not skill_md.is_file():
        raise SkillNotFoundError(f"SKILL.md not found: {raw}")

    from olano.skills.loader import _read_frontmatter

    name = skill_dir.name
    try:
        fm = _read_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
        name = str(fm.get("name") or skill_dir.name).strip() or skill_dir.name
    except OSError:  # pragma: no cover - defensive (race with a delete)
        pass

    return SkillRef(
        name=name,
        directory=skill_dir,
        skill_md=skill_md,
        scope=scope,
        source_dir=source_dir,
        workspace_path=workspace_relative(skill_dir, workspace),
    )


def annotate(
    skills: list[dict[str, Any]], config: AgentConfig | None, workspace: Workspace
) -> list[dict[str, Any]]:
    """Stamp ``scope`` / ``editable`` / ``workspace_path`` / ``enabled`` onto rows.

    Pure path arithmetic over the roots already computed — no extra disk reads —
    so the list view can badge every row ("common skill", "read-only") and draw
    its on/off switch without a detail request per skill.

    ``enabled`` is the agent's own answer (``agent.json5 → disabled_skills``),
    so it is ``True`` for every row in the unscoped listing, which has no agent
    to ask. ``gated`` is the separate answer "this skill needs tools the agent
    lacks" (``requires_tools``) — a reason string, or ``None``; kept apart from
    ``enabled`` because a switch cannot fix it, granting the tools can.
    """
    from olano.skills.loader import (
        disabled_skill_names,
        effective_tool_names,
        gated_skill_reason,
    )

    roots = browse_roots(config, workspace)
    disabled = disabled_skill_names(config)
    effective = effective_tool_names(config)
    out: list[dict[str, Any]] = []
    for skill in skills:
        location = str(skill.get("location") or "")
        entry = dict(skill)
        entry["enabled"] = str(skill.get("name") or "").strip().lower() not in disabled
        entry["gated"] = gated_skill_reason(skill, effective)
        if location:
            directory = Path(location).parent
            owner = classify(directory, roots)
            scope = owner[1] if owner else None
            rel = workspace_relative(directory, workspace)
            entry["scope"] = scope
            entry["workspace_path"] = rel
            entry["editable"] = is_editable(scope, rel)
        out.append(entry)
    return out


def file_kind(path: Path) -> str:
    """Which viewer the dashboard should use: markdown / image / text / binary."""
    suffix = path.suffix.lower()
    if suffix in _MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _TEXT_SUFFIXES or not suffix:
        return "text"
    return "binary"


def list_skill_files(ref: SkillRef) -> dict[str, Any]:
    """Every file and folder bundled with the skill, depth- and count-capped.

    Flat, sorted by path, directories included so the UI can render an empty
    ``assets/`` rather than silently dropping it. ``truncated`` reports the cap
    being hit instead of quietly showing a partial folder as complete.
    """
    root = ref.directory
    entries: list[dict[str, Any]] = []
    truncated = False

    def walk(directory: Path, depth: int) -> None:
        nonlocal truncated
        if depth > MAX_DEPTH:
            truncated = True
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:  # pragma: no cover - defensive (permissions, races)
            return
        for child in children:
            if child.name in _SKIP_DIRS:
                continue
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                return
            try:
                stat = child.stat()
                is_dir = child.is_dir()
            except OSError:
                # Broken symlink or an entry that vanished mid-walk — skip it
                # rather than failing the whole listing.
                continue
            rel = child.relative_to(root).as_posix()
            entry: dict[str, Any] = {
                "name": child.name,
                "path": rel,
                "type": "directory" if is_dir else "file",
                "size": 0 if is_dir else stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            if not is_dir:
                entry["kind"] = file_kind(child)
            entries.append(entry)
            if is_dir and not child.is_symlink():
                walk(child, depth + 1)

    walk(root, 1)
    return {"files": entries, "truncated": truncated}


def skill_file_path(ref: SkillRef, rel: str) -> Path:
    """Resolve *rel* inside the skill folder, refusing anything that escapes it.

    ``resolve()`` collapses ``..`` and follows symlinks, so both a crafted
    ``../../../etc/passwd`` and a symlink planted inside the folder land outside
    and are refused.
    """
    cleaned = (rel or "").strip().lstrip("/")
    if not cleaned:
        raise SkillAccessError("path is required")
    target = _resolved(ref.directory / cleaned)
    if not _contains(_resolved(ref.directory), target):
        raise SkillAccessError("path escapes the skill folder")
    return target


def read_skill_file(ref: SkillRef, rel: str) -> dict[str, Any]:
    """Read one file from the skill folder for preview.

    Text comes back decoded (``errors="replace"``, so a stray byte shows a
    replacement char instead of failing the preview) and capped at
    :data:`MAX_TEXT_BYTES`. Binary comes back as metadata only — the raw
    endpoint serves the bytes for images and downloads.
    """
    target = skill_file_path(ref, rel)
    if not target.exists():
        raise SkillNotFoundError(f"File not found: {rel}")
    if not target.is_file():
        raise SkillAccessError(f"Not a file: {rel}")

    size = target.stat().st_size
    kind = file_kind(target)
    mime, _ = mimetypes.guess_type(str(target))
    payload: dict[str, Any] = {
        "path": Path(rel).as_posix().lstrip("/"),
        "name": target.name,
        "size": size,
        "kind": kind,
        "mime": mime,
        "truncated": False,
    }
    if kind in ("image", "binary"):
        payload["content"] = None
        return payload

    with target.open("rb") as fh:
        raw = fh.read(MAX_TEXT_BYTES + 1)
    if b"\0" in raw[:8192]:
        # Text by extension, binary in fact (a .txt that is really a blob).
        payload["kind"] = "binary"
        payload["content"] = None
        return payload
    if len(raw) > MAX_TEXT_BYTES:
        raw = raw[:MAX_TEXT_BYTES]
        payload["truncated"] = True
    payload["content"] = raw.decode("utf-8", errors="replace")
    return payload
