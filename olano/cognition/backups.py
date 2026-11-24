"""Snapshot/restore for autonomous worker artifacts.

In the "mostly autonomous" posture, background workers write artifacts directly
into the workspace. Before each such write we snapshot the target files so the
user can undo from a single Pulse action (``restore_backup``). This is the
safety net that makes proactive writes safe.

Layout: ``<workspace>/.olano/state/cognition/backups/<job_id>/`` holds a
``manifest.json`` plus copies of pre-existing files. Files that did NOT exist at
snapshot time are recorded so ``restore`` can delete them (undo of a creation).
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_KEEP_BACKUPS = 20


def _backups_dir(workspace_root: Path) -> Path:
    from olano.config.workspace import WORKSPACE_STATE_DIRNAME

    return workspace_root / WORKSPACE_STATE_DIRNAME / "state" / "cognition" / "backups"


def _safe_name(rel_path: str) -> str:
    return rel_path.replace("/", "__").replace("\\", "__")


def snapshot_backup(workspace_root: Path, paths: list[str], job_label: str) -> str:
    """Snapshot ``paths`` (workspace-relative) and return a ``job_id``.

    Records existing files (copied) and not-yet-existing files (so a later
    restore deletes them). Prunes to the most recent ``_KEEP_BACKUPS`` jobs.
    """
    job_id = f"{job_label}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    job_dir = _backups_dir(workspace_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    files: list[dict] = []
    for rel in paths:
        rel = str(rel).strip()
        if not rel:
            continue
        src = Path(rel)
        if not src.is_absolute():
            src = workspace_root / rel
        entry: dict = {"path": rel, "existed": src.exists() and src.is_file()}
        if entry["existed"]:
            stored = _safe_name(rel)
            shutil.copy2(src, job_dir / stored)
            entry["stored"] = stored
        files.append(entry)

    manifest = {
        "job_id": job_id,
        "label": job_label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workspace_root": str(workspace_root),
        "files": files,
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    _prune(workspace_root)
    return job_id


def _prune(workspace_root: Path) -> None:
    root = _backups_dir(workspace_root)
    if not root.exists():
        return
    jobs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in jobs[_KEEP_BACKUPS:]:
        shutil.rmtree(stale, ignore_errors=True)


def diff_against_backup(workspace_root: Path, job_id: str) -> dict:
    """Compare a snapshot's files against their current on-disk state.

    Deterministic ground-truth of what an autonomous run actually changed —
    independent of any LLM-reported summary. Returns a structured report the
    dashboard renders so the user can see the concrete outcome.
    """
    job_dir = _backups_dir(workspace_root) / job_id
    manifest_path = job_dir / "manifest.json"
    empty = {
        "files": [],
        "files_changed": 0,
        "files_removed": 0,
        "lines_before": 0,
        "lines_after": 0,
        "lines_delta": 0,
    }
    if not manifest_path.exists():
        return empty
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return empty

    def _count(text: str) -> int:
        return len([ln for ln in text.splitlines() if ln.strip()])

    files: list[dict] = []
    tot_before = tot_after = changed = removed = 0
    for entry in manifest.get("files", []):
        rel = entry["path"]
        before = ""
        if entry.get("existed"):
            stored = job_dir / entry.get("stored", _safe_name(rel))
            if stored.exists():
                before = stored.read_text(encoding="utf-8", errors="replace")
        cur = workspace_root / rel
        after = cur.read_text(encoding="utf-8", errors="replace") if cur.is_file() else None

        before_lines = _count(before)
        if after is None:
            status = "removed"
            after_lines = 0
            removed += 1
        elif after != before:
            status = "modified"
            after_lines = _count(after)
            changed += 1
        else:
            status = "unchanged"
            after_lines = _count(after)
        tot_before += before_lines
        tot_after += after_lines
        files.append(
            {
                "path": rel,
                "status": status,
                "lines_before": before_lines,
                "lines_after": after_lines,
                "lines_delta": after_lines - before_lines,
            }
        )

    # Surface changed/removed first, then unchanged.
    order = {"modified": 0, "removed": 1, "unchanged": 2}
    files.sort(key=lambda f: order.get(f["status"], 3))
    return {
        "files": files,
        "files_changed": changed,
        "files_removed": removed,
        "lines_before": tot_before,
        "lines_after": tot_after,
        "lines_delta": tot_after - tot_before,
    }


def restore(workspace_root: Path, job_id: str) -> tuple[bool, str]:
    """Restore a snapshot: revert edited files and delete newly-created ones."""
    job_dir = _backups_dir(workspace_root) / job_id
    manifest_path = job_dir / "manifest.json"
    if not manifest_path.exists():
        return False, f"No backup found for job '{job_id}'."

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"Corrupt backup manifest for '{job_id}': {e}"

    restored, deleted = 0, 0
    for entry in manifest.get("files", []):
        target = workspace_root / entry["path"]
        if entry.get("existed"):
            stored = job_dir / entry.get("stored", _safe_name(entry["path"]))
            if stored.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stored, target)
                restored += 1
        else:
            # File was created by the worker after the snapshot — undo = delete.
            if target.exists():
                try:
                    target.unlink()
                    deleted += 1
                except OSError as e:  # pragma: no cover - defensive
                    logger.debug("restore: could not delete %s: %s", target, e)

    return True, f"Restored {restored} file(s), removed {deleted} created file(s)."
