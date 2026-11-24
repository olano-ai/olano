"""Copy-on-write skill forks — full-folder learned copies with provenance.

When self-learning improves an existing skill, it must never squeeze the skill
through a summary: the original may be a multi-file runbook (SKILL.md plus
``scripts/``, ``references/``, forms…) that a marketplace/pack refresh will
later replace wholesale (see ``skills/sync.py`` — updates are rmtree+copytree).
So the first improvement FORKS the entire skill folder into
``skills/learned/<slug>/`` and all improvements are applied to that fork:

- the fork starts byte-identical to the original (path refs rewritten), so
  "an improvement must not lose content" becomes checkable against the fork's
  own recorded base instead of a fuzzy diff between two unrelated documents;
- a ``.lc-provenance.json`` sidecar records the source per-file manifest (for
  upstream-drift detection in any file, not just SKILL.md) and the fork's base
  state (per-file sizes + SKILL.md headings) used by the preservation gate;
- a pristine base snapshot is kept under ``<ws>/.olano/skill_bases/<slug>/``
  as the merge base for future three-way re-merges when the source refreshes.

The preservation gate (:func:`fork_violations` / :func:`shadow_violations_vs_source`)
is the invariant that makes degradation structurally impossible: a learned copy
that deleted files, hollowed out prose, or dropped the original's section
structure is quarantined by the loader (the original serves again) instead of
silently shadowing the original.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olano.config.workspace import WORKSPACE_STATE_DIRNAME
from olano.skills.built_in_sync import _ALL_SENTINEL_NAMES

logger = logging.getLogger(__name__)

PROVENANCE_FILE = ".lc-provenance.json"
PROVENANCE_VERSION = 1

# Ownership/sync sentinels that must NOT travel into a fork — they would
# transfer pack/built-in ownership semantics onto the learned copy. Current and
# pre-rename names alike; olano/skills/built_in_sync.py owns the list.
_SENTINEL_NAMES = _ALL_SENTINEL_NAMES

# Files whose drastic shrinkage indicates content loss (prose, not code).
_PROSE_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst"})

# Preservation-gate thresholds. Deliberately constants, not config: they encode
# the invariant "a learned copy must not lose the original's content", which is
# not a per-deployment preference. Tune here if real skills hit false positives.
MIN_PROSE_SIZE_RATIO = 0.5
MAX_MISSING_HEADING_RATIO = 0.34
MIN_GATED_SIZE = 512  # bytes — files smaller than this may shrink freely

_HEADING_RE = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.MULTILINE)


def skill_bases_dir(workspace_root: Path) -> Path:
    """Directory holding pristine fork bases (merge bases for re-merge)."""
    return workspace_root / WORKSPACE_STATE_DIRNAME / "skill_bases"


def fork_base_dir(workspace_root: Path, slug: str) -> Path:
    return skill_bases_dir(workspace_root) / slug


def _iter_skill_files(skill_dir: Path) -> list[Path]:
    """Every regular file in a skill folder, minus sentinels + the sidecar."""
    out: list[Path] = []
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.name in _SENTINEL_NAMES or p.name == PROVENANCE_FILE:
            continue
        out.append(p)
    return out


def build_file_manifest(skill_dir: Path) -> dict[str, dict[str, Any]]:
    """Per-file provenance manifest: relpath → {sha256, size}.

    Extends the tree-hash idea of ``built_in_sync._dir_content_hash`` with
    per-file granularity so drift in ANY file (a script, a reference) is
    detectable, and so the preservation gate can check sizes without needing
    the base bytes on disk.
    """
    manifest: dict[str, dict[str, Any]] = {}
    for p in _iter_skill_files(skill_dir):
        rel = p.relative_to(skill_dir).as_posix()
        data = p.read_bytes()
        manifest[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    return manifest


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :])
    return text


def skill_md_headings(text: str) -> list[str]:
    """H2/H3 headings of a SKILL.md body — the section skeleton the gate protects."""
    return [m.group(1).strip() for m in _HEADING_RE.finditer(_strip_frontmatter(text))]


def read_provenance(skill_dir: Path) -> dict[str, Any] | None:
    """Parse the fork sidecar; ``None`` when absent or unreadable."""
    path = skill_dir / PROVENANCE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def is_fork(skill_dir: Path) -> bool:
    return read_provenance(skill_dir) is not None


def is_prose_file(rel: str) -> bool:
    """True for files the gate/merge policy treats as prose (vs code/binary)."""
    return Path(rel).suffix.lower() in _PROSE_SUFFIXES


def rewrite_refs_text(text: str, old_dirname: str, slug: str) -> str:
    """Rewrite workspace-relative self-references in a text file's content.

    ``skills/<original>/scripts/x.py`` written inside the original's files would
    keep pointing at the vendor copy — which a later pack refresh silently swaps
    to a different version than the fork expects. Folder-relative refs
    (``scripts/x.py``) already travel correctly and are left alone.
    """
    old = f"skills/{old_dirname}/"
    new = f"skills/learned/{slug}/"
    if old == new:
        return text
    return text.replace(old, new)


def _rewrite_workspace_paths(dest: Path, old_dirname: str, slug: str) -> None:
    """Apply :func:`rewrite_refs_text` to every text file in a fork."""
    for p in _iter_skill_files(dest):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable — never rewrite
        rewritten = rewrite_refs_text(text, old_dirname, slug)
        if rewritten != text:
            p.write_text(rewritten, encoding="utf-8")


def _relative_to_workspace(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace_root.resolve()))
    except (ValueError, OSError):
        return str(path)  # managed skill outside the workspace — keep absolute


def stamp_frontmatter_text(text: str, *, source_name: str, src_rel: str, source_hash: str) -> str:
    """Insert ``lc_improves`` / ``lc_source_path`` / ``lc_source_sha256`` into a
    SKILL.md's frontmatter (same keys the historical SKILL.md-only shadows used,
    so existing tooling and greps keep working). Creates a minimal frontmatter
    block when the original had none, so the fork still shadows by exact name."""
    stamps = [
        f"lc_improves: {source_name}",
        f"lc_source_path: {src_rel}",
        f"lc_source_sha256: {source_hash}",
    ]
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                existing = "\n".join(lines[1:i])
                fresh = [s for s in stamps if s.partition(":")[0] not in existing]
                new_lines = lines[:i] + fresh + lines[i:]
                return "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
    # No frontmatter block — prepend one that preserves the shadow-by-name contract.
    return "\n".join(["---", f"name: {source_name}", *stamps, "---", ""]) + text


def _stamp_frontmatter(
    skill_md: Path, *, source_name: str, source_md: Path, workspace_root: Path
) -> None:
    try:
        source_hash = hashlib.sha256(source_md.read_bytes()).hexdigest()
    except OSError:
        source_hash = ""
    src_rel = _relative_to_workspace(source_md, workspace_root)
    text = skill_md.read_text(encoding="utf-8") if skill_md.is_file() else ""
    skill_md.write_text(
        stamp_frontmatter_text(
            text, source_name=source_name, src_rel=src_rel, source_hash=source_hash
        ),
        encoding="utf-8",
    )


def write_provenance(fork_dir: Path, provenance: dict[str, Any]) -> None:
    """Write the fork sidecar (pretty JSON, stable for diffs)."""
    (fork_dir / PROVENANCE_FILE).write_text(json.dumps(provenance, indent=2), encoding="utf-8")


def refresh_fork_base(workspace_root: Path, slug: str, fork_dir: Path) -> None:
    """Re-snapshot the merge base to the fork's CURRENT state.

    Called at fork creation and after each re-merge: the base is always "the
    last state that incorporated both upstream and learned changes", which is
    what ours-changed detection and the preservation gate compare against.
    """
    base = fork_base_dir(workspace_root, slug)
    if base.exists():
        shutil.rmtree(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fork_dir, base)


def fork_skill(
    source_skill_md: Path,
    workspace_root: Path,
    slug: str,
    *,
    source_name: str,
) -> Path:
    """Fork a full skill folder into ``skills/learned/<slug>/``.

    Copies every file (sentinels excluded), rewrites workspace-relative
    self-references, stamps provenance frontmatter into SKILL.md, writes the
    ``.lc-provenance.json`` sidecar (source manifest + fork base manifest +
    SKILL.md headings), and snapshots the pristine fork as the merge base.

    The destination must not already exist — callers decide what to do with a
    pre-existing learned folder (see ``SelfLearningService._ensure_fork``).
    """
    source_dir = source_skill_md.parent
    dest = workspace_root / "skills" / "learned" / slug
    if dest.exists():
        raise FileExistsError(f"fork destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    source_manifest = build_file_manifest(source_dir)
    shutil.copytree(source_dir, dest, ignore=shutil.ignore_patterns(*_SENTINEL_NAMES))
    _rewrite_workspace_paths(dest, source_dir.name, slug)
    _stamp_frontmatter(
        dest / "SKILL.md",
        source_name=source_name,
        source_md=source_skill_md,
        workspace_root=workspace_root,
    )

    base_manifest = build_file_manifest(dest)
    try:
        headings = skill_md_headings((dest / "SKILL.md").read_text(encoding="utf-8"))
    except OSError:
        headings = []
    provenance = {
        "version": PROVENANCE_VERSION,
        "source_name": source_name,
        "source_path": _relative_to_workspace(source_skill_md, workspace_root),
        "source_dir": _relative_to_workspace(source_dir, workspace_root),
        "forked_at": datetime.now(timezone.utc).isoformat(),
        "source_files": source_manifest,
        "base_files": base_manifest,
        "skill_md_headings": headings,
    }
    write_provenance(dest, provenance)

    # Pristine merge base for future three-way re-merges (and restores). Kept
    # outside the live tree so discovery/heartbeat/audit never see it and
    # workers' editable view can't touch it.
    refresh_fork_base(workspace_root, slug, dest)

    logger.info(
        "skill fork: %s → skills/learned/%s (%d files)", source_dir, slug, len(base_manifest)
    )
    return dest


# ------------------------------------------------------------------ gate


def fork_violations(fork_dir: Path) -> list[str]:
    """Preservation-gate check of a fork against its own recorded base.

    Empty list = pass. A non-fork dir (no sidecar) trivially passes — use
    :func:`shadow_violations_vs_source` for legacy SKILL.md-only shadows.
    """
    prov = read_provenance(fork_dir)
    if prov is None:
        return []
    violations: list[str] = []
    base_files = prov.get("base_files") or {}
    for rel, meta in base_files.items():
        cur = fork_dir / rel
        if not cur.is_file():
            violations.append(f"file removed vs fork base: {rel}")
            continue
        size = int(meta.get("size") or 0)
        if (
            Path(rel).suffix.lower() in _PROSE_SUFFIXES
            and size >= MIN_GATED_SIZE
            and cur.stat().st_size < size * MIN_PROSE_SIZE_RATIO
        ):
            violations.append(
                f"{rel} shrank to {cur.stat().st_size}B (<{int(MIN_PROSE_SIZE_RATIO * 100)}% of {size}B base)"
            )
    base_headings = [str(h) for h in (prov.get("skill_md_headings") or [])]
    if base_headings:
        try:
            current = skill_md_headings((fork_dir / "SKILL.md").read_text(encoding="utf-8"))
        except OSError:
            current = []
        missing = [h for h in base_headings if h not in current]
        if len(missing) > int(len(base_headings) * MAX_MISSING_HEADING_RATIO):
            shown = "; ".join(missing[:3])
            violations.append(
                f"SKILL.md lost {len(missing)}/{len(base_headings)} section headings (e.g. {shown})"
            )
    return violations


def _body_size(skill_md: Path) -> int:
    try:
        return len(_strip_frontmatter(skill_md.read_text(encoding="utf-8")).encode("utf-8"))
    except OSError:
        return 0


def is_machine_written(skill_md: Path) -> bool:
    """True for auto-generated learned files (``auto_learned: true`` or ``lc_``
    provenance keys). Hand-written learned overrides return False and are never
    quarantined — deliberate user shadowing stays authoritative."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return False
    head = "\n".join(text.splitlines()[:40])
    return bool(
        re.search(r"^(auto_learned:\s*true|lc_improves:|lc_source_path:)", head, re.MULTILINE)
    )


def shadow_violations_vs_source(shadow_md: Path, source_md: Path) -> list[str]:
    """Preservation check for legacy SKILL.md-only shadows (no sidecar): compare
    the shadow against the CURRENT original it shadows by name."""
    violations: list[str] = []
    src_size = _body_size(source_md)
    shadow_size = _body_size(shadow_md)
    if src_size >= MIN_GATED_SIZE and shadow_size < src_size * MIN_PROSE_SIZE_RATIO:
        violations.append(
            f"shadow body is {shadow_size}B vs original {src_size}B "
            f"(<{int(MIN_PROSE_SIZE_RATIO * 100)}%)"
        )
    try:
        src_headings = skill_md_headings(source_md.read_text(encoding="utf-8"))
        shadow_headings = skill_md_headings(shadow_md.read_text(encoding="utf-8"))
    except OSError:
        src_headings, shadow_headings = [], []
    if src_headings:
        missing = [h for h in src_headings if h not in shadow_headings]
        if len(missing) > int(len(src_headings) * MAX_MISSING_HEADING_RATIO):
            violations.append(
                f"shadow lost {len(missing)}/{len(src_headings)} original section headings"
            )
    src_aux = {
        p.relative_to(source_md.parent).as_posix()
        for p in _iter_skill_files(source_md.parent)
        if p.name != "SKILL.md"
    }
    if src_aux:
        shadow_dir = shadow_md.parent
        missing_aux = sorted(rel for rel in src_aux if not (shadow_dir / rel).is_file())
        if missing_aux:
            violations.append(
                f"shadow is missing {len(missing_aux)} original file(s) "
                f"(e.g. {', '.join(missing_aux[:3])})"
            )
    return violations
