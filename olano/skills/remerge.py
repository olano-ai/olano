"""Three-way re-merge of skill forks after their source refreshes.

A fork (``skills/learned/<slug>/`` with a ``.lc-provenance.json`` sidecar) is a
full copy of an original skill carrying learned improvements. When the original
is refreshed — a skill-pack update (rmtree+copytree, see ``skills/sync.py``), a
marketplace install, or a manual edit — the fork must absorb the upstream
changes without losing the learned ones. The sidecar's per-file manifests make
this a real three-way merge:

- ``source_files``  = the source at fork/last-merge time  → "did upstream change?"
- ``base_files``    = the fork at fork/last-merge time    → "did the fork change?"
- ``skill_bases/<slug>/`` = the base CONTENT              → merge base for git merge-file

Per-file resolution (deterministic first, model never required):

- upstream unchanged                     → keep fork file (no action)
- upstream changed,  fork unchanged      → take upstream (transformed: path refs
  rewritten, SKILL.md provenance restamped with the new source hash)
- upstream deleted,  fork unchanged      → delete from fork
- upstream deleted,  fork changed        → keep fork + conflict note
- upstream changed,  fork changed        → ``git merge-file`` three-way:
  clean → merged; conflicted prose → keep fork, hand to the skill-librarian
  worker for semantic reconciliation; conflicted scripts/binary → keep fork +
  conflict note (never auto-merge code — a silently broken script is worse
  than a stale one)

Afterwards the sidecar manifests and the base snapshot are refreshed to the
merged state, so the loop converges (the same drift is never re-flagged) and
the preservation gate keeps working against the new baseline. Forks currently
failing the preservation gate are skipped — they are quarantined at load time
and re-merging a degraded copy would launder it.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olano.skills.fork import (
    build_file_manifest,
    fork_base_dir,
    fork_violations,
    is_prose_file,
    read_provenance,
    refresh_fork_base,
    rewrite_refs_text,
    skill_md_headings,
    stamp_frontmatter_text,
    write_provenance,
)

logger = logging.getLogger(__name__)

_GIT_MERGE_TIMEOUT = 30.0


def resolve_source_dir(provenance: dict[str, Any], workspace_root: Path) -> Path | None:
    """The fork's source folder, or ``None`` when it no longer exists (the skill
    was uninstalled upstream — the fork is self-contained and keeps serving)."""
    raw = str(provenance.get("source_dir") or "")
    if not raw:
        return None
    src = Path(raw)
    if not src.is_absolute():
        src = workspace_root / src
    return src if src.is_dir() else None


def detect_stale_forks(workspace_root: Path) -> list[dict[str, Any]]:
    """Forks whose source drifted from the recorded per-file manifest.

    Unlike the legacy SKILL.md-hash check, this catches drift in ANY file —
    a pack update that only touches ``scripts/x.py`` flags the fork too.
    Returns ``[{slug, fork_dir, source_dir, provenance}]``.
    """
    learned = workspace_root / "skills" / "learned"
    if not learned.is_dir():
        return []
    stale: list[dict[str, Any]] = []
    for fork_dir in sorted(p for p in learned.iterdir() if p.is_dir()):
        prov = read_provenance(fork_dir)
        if prov is None:
            continue
        source_dir = resolve_source_dir(prov, workspace_root)
        if source_dir is None:
            continue
        if build_file_manifest(source_dir) != (prov.get("source_files") or {}):
            stale.append(
                {
                    "slug": fork_dir.name,
                    "fork_dir": fork_dir,
                    "source_dir": source_dir,
                    "provenance": prov,
                }
            )
    return stale


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_merge_file(ours: str, base: str, theirs: str) -> str | None:
    """Three-way text merge via ``git merge-file`` (works without a repo).

    Returns the merged text on a clean merge, ``None`` on conflicts, a missing
    git binary, or any error — callers treat ``None`` as "needs a human/worker".
    """
    if shutil.which("git") is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="lc-remerge-") as td:
            paths = []
            for name, content in (("ours", ours), ("base", base), ("theirs", theirs)):
                p = Path(td) / name
                p.write_text(content, encoding="utf-8")
                paths.append(str(p))
            result = subprocess.run(
                ["git", "merge-file", "-p", "-L", "fork", "-L", "base", "-L", "upstream", *paths],
                capture_output=True,
                text=True,
                timeout=_GIT_MERGE_TIMEOUT,
            )
        if result.returncode == 0:
            return result.stdout
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _transform_theirs(
    data: bytes, rel: str, *, provenance: dict[str, Any], slug: str, new_source_md_hash: str
) -> bytes:
    """Apply the fork-time transformation to fresh upstream content so it merges
    cleanly against the fork lineage: path refs rewritten to the fork, and
    SKILL.md provenance restamped (``lc_source_sha256`` gets the NEW source
    hash — the merge itself then propagates the restamp). Binary passes through."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    old_dirname = Path(str(provenance.get("source_dir") or "")).name
    if old_dirname:
        text = rewrite_refs_text(text, old_dirname, slug)
    if rel == "SKILL.md":
        # Strip any stale lc_* lines the upstream file itself carries, then stamp.
        text = stamp_frontmatter_text(
            "\n".join(
                ln
                for ln in text.splitlines()
                if not ln.startswith(("lc_improves:", "lc_source_path:", "lc_source_sha256:"))
            )
            + ("\n" if text.endswith("\n") else ""),
            source_name=str(provenance.get("source_name") or slug),
            src_rel=str(provenance.get("source_path") or ""),
            source_hash=new_source_md_hash,
        )
    return text.encode("utf-8")


def remerge_fork(workspace_root: Path, fork_dir: Path) -> dict[str, Any]:
    """Deterministically re-merge one stale fork; returns a summary dict.

    Summary keys: ``slug``, ``skipped`` (reason str or ""), ``taken_upstream``,
    ``merged``, ``deleted``, ``kept_ours`` (rel lists), ``conflicts``
    (``[{rel, kind, fork_path, source_path, base_path}]``) and ``errors``.
    Prose conflicts are the ones a deep worker may reconcile afterwards.
    """
    slug = fork_dir.name
    summary: dict[str, Any] = {
        "slug": slug,
        "skipped": "",
        "taken_upstream": [],
        "merged": [],
        "deleted": [],
        "kept_ours": [],
        "conflicts": [],
        "errors": [],
    }
    prov = read_provenance(fork_dir)
    if prov is None:
        summary["skipped"] = "no provenance sidecar"
        return summary
    violations = fork_violations(fork_dir)
    if violations:
        # Quarantined at load time; merging a degraded copy would launder it.
        summary["skipped"] = f"fork fails preservation gate: {violations[0]}"
        return summary
    source_dir = resolve_source_dir(prov, workspace_root)
    if source_dir is None:
        summary["skipped"] = "source no longer exists"
        return summary

    old_src: dict[str, Any] = prov.get("source_files") or {}
    base_files: dict[str, Any] = prov.get("base_files") or {}
    new_src = build_file_manifest(source_dir)
    if new_src == old_src:
        summary["skipped"] = "source unchanged"
        return summary

    try:
        new_source_md_hash = _sha256_bytes((source_dir / "SKILL.md").read_bytes())
    except OSError:
        new_source_md_hash = ""
    base_dir = fork_base_dir(workspace_root, slug)

    def conflict(rel: str, kind: str) -> None:
        summary["kept_ours"].append(rel)
        summary["conflicts"].append(
            {
                "rel": rel,
                "kind": kind,
                "fork_path": str(fork_dir / rel),
                "source_path": str(source_dir / rel),
                "base_path": str(base_dir / rel),
            }
        )

    for rel in sorted(set(old_src) | set(new_src)):
        old_meta = old_src.get(rel)
        new_meta = new_src.get(rel)
        if old_meta == new_meta:
            continue  # upstream did not change this file
        ours_path = fork_dir / rel
        base_meta = base_files.get(rel)
        try:
            ours_hash = _sha256_bytes(ours_path.read_bytes()) if ours_path.is_file() else None
        except OSError as e:
            summary["errors"].append(f"{rel}: {e}")
            continue
        ours_unchanged = (
            ours_hash is not None and base_meta is not None and ours_hash == base_meta.get("sha256")
        )

        try:
            if new_meta is None:
                # Upstream deleted the file.
                if ours_hash is None:
                    summary["deleted"].append(rel)
                elif ours_unchanged:
                    ours_path.unlink()
                    summary["deleted"].append(rel)
                else:
                    conflict(rel, "deleted-upstream")
                continue

            theirs = _transform_theirs(
                (source_dir / rel).read_bytes(),
                rel,
                provenance=prov,
                slug=slug,
                new_source_md_hash=new_source_md_hash,
            )
            if old_meta is None:
                # Upstream added a new file.
                if ours_hash is None:
                    ours_path.parent.mkdir(parents=True, exist_ok=True)
                    ours_path.write_bytes(theirs)
                    summary["taken_upstream"].append(rel)
                elif ours_hash == _sha256_bytes(theirs):
                    pass  # both added identical content
                else:
                    conflict(rel, "both-added")
                continue

            # Upstream modified an existing file.
            if ours_hash is None or ours_unchanged:
                ours_path.parent.mkdir(parents=True, exist_ok=True)
                ours_path.write_bytes(theirs)
                summary["taken_upstream"].append(rel)
                continue

            # Both sides changed → three-way merge against the base content.
            ours_text = _read_text(ours_path)
            base_text = _read_text(base_dir / rel) if (base_dir / rel).is_file() else None
            try:
                theirs_text: str | None = theirs.decode("utf-8")
            except UnicodeDecodeError:
                theirs_text = None
            if ours_text is None or base_text is None or theirs_text is None:
                conflict(rel, "binary")
                continue
            merged = _git_merge_file(ours_text, base_text, theirs_text)
            if merged is not None:
                ours_path.write_text(merged, encoding="utf-8")
                summary["merged"].append(rel)
            else:
                conflict(rel, "prose" if is_prose_file(rel) else "script")
        except OSError as e:
            summary["errors"].append(f"{rel}: {e}")

    # Converge: record the new source manifest and rebase both manifests + the
    # base snapshot on the merged state, so this drift is never re-flagged and
    # the preservation gate compares against the new baseline.
    prov["source_files"] = new_src
    prov["base_files"] = build_file_manifest(fork_dir)
    try:
        prov["skill_md_headings"] = skill_md_headings(
            (fork_dir / "SKILL.md").read_text(encoding="utf-8")
        )
    except OSError:
        pass
    prov["last_remerge"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "taken_upstream": summary["taken_upstream"],
        "merged": summary["merged"],
        "deleted": summary["deleted"],
        "kept_ours": summary["kept_ours"],
    }
    write_provenance(fork_dir, prov)
    refresh_fork_base(workspace_root, slug, fork_dir)
    logger.info(
        "skill remerge: %s — %d upstream, %d merged, %d deleted, %d conflicts kept ours",
        slug,
        len(summary["taken_upstream"]),
        len(summary["merged"]),
        len(summary["deleted"]),
        len(summary["conflicts"]),
    )
    return summary


def snapshot_paths_for_forks(workspace_root: Path, stale: list[dict[str, Any]]) -> list[str]:
    """Workspace-relative paths of every file a re-merge may touch (fork files,
    sidecars, and base snapshots) — fed to ``snapshot_backup`` for one-click Undo."""
    rels: list[str] = []
    for entry in stale:
        for root in (entry["fork_dir"], fork_base_dir(workspace_root, entry["slug"])):
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    try:
                        rels.append(str(p.relative_to(workspace_root)))
                    except ValueError:  # pragma: no cover - defensive
                        continue
    return rels


def remerge_stale_forks(workspace_root: Path) -> tuple[list[dict[str, Any]], str]:
    """Detect + deterministically re-merge every stale fork.

    Returns ``(summaries, backup_job_id)`` — summaries only for forks that were
    actually processed (not skipped), backup_job_id "" when nothing was stale.
    A single snapshot covers all touched forks so one Undo reverts the run.
    """
    stale = detect_stale_forks(workspace_root)
    if not stale:
        return [], ""
    from olano.cognition.backups import snapshot_backup

    backup_job = snapshot_backup(
        workspace_root, snapshot_paths_for_forks(workspace_root, stale), "skill_remerge"
    )
    summaries: list[dict[str, Any]] = []
    for entry in stale:
        try:
            s = remerge_fork(workspace_root, entry["fork_dir"])
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("skill remerge failed for %s: %s", entry["slug"], e)
            s = {"slug": entry["slug"], "skipped": f"error: {e}", "conflicts": []}
        if not s.get("skipped"):
            summaries.append(s)
        else:
            logger.debug("skill remerge skipped %s: %s", entry["slug"], s["skipped"])
    return summaries, backup_job


def post_remerge_pulse(
    config: Any, workspace_root: Path, summaries: list[dict[str, Any]], backup_job: str
) -> None:
    """FINDING pulse summarizing the re-merge, with Undo — mirrors the skill
    librarian's result pulse. Conflict files are listed so the user knows which
    upstream changes were NOT absorbed (the fork's version was kept)."""
    if not summaries:
        return
    try:
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane, PulsePriority, PulseType

        lines: list[str] = []
        any_conflicts = False
        for s in summaries:
            lines.append(
                f"**{s['slug']}** — {len(s['taken_upstream'])} file(s) updated from source, "
                f"{len(s['merged'])} merged, {len(s['deleted'])} removed"
            )
            for c in s["conflicts"]:
                any_conflicts = True
                lines.append(
                    f"  - ⚠ `{c['rel']}` ({c['kind']}): kept the fork's version; review "
                    f"upstream at `{c['source_path']}`"
                )
        from olano.config.workspace import WORKSPACE_STATE_DIRNAME

        engine = PulseEngine(config, workspace_root / WORKSPACE_STATE_DIRNAME)
        engine.add(
            content="\n".join(lines),
            source="cognition",
            lane=PulseLane.PULSE,
            type=PulseType.FINDING,
            priority=PulsePriority.NORMAL if any_conflicts else PulsePriority.LOW,
            title="Skill source updated — learned copies re-merged",
            why_now="The original skill(s) changed; learned improvements were preserved.",
            suggested_next_step=(
                "Review conflict files (fork's version was kept) and use Undo to revert "
                "the whole re-merge if needed."
                if any_conflicts
                else "Nothing to do — upstream changes were absorbed cleanly."
            ),
            references=[f"skills/learned/{s['slug']}/SKILL.md" for s in summaries],
            action={"type": "restore_backup", "job_id": backup_job} if backup_job else None,
            metadata={"kind": "skill_remerge", "auto_applied": True},
            dedup_window_hours=0.0,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("skill remerge: result pulse failed: %s", e)
