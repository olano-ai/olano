"""WorkspaceCartographer — orchestrates the 6-phase workspace analysis."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, cast

from olano.cartography.classifier import classify_file
from olano.cartography.map_generator import generate_workspace_map, save_workspace_map
from olano.cartography.walker import EXTERNAL_REPO_LABEL, iter_workspace

if TYPE_CHECKING:
    from olano.config.workspace import Workspace

logger = logging.getLogger(__name__)


class WorkspaceCartographer:
    """
    Six-phase workspace analysis:

    Phase 1: Full-tree scan (enumerate all files)
    Phase 2: Per-file classification (category + label)
    Phase 3: Workspace map generation (<3k tokens, always-on L0)
    Phase 4: Workspace bridge generation (organizational guide for L1)
    Phase 5: Deep file index (per-file summaries, stored in .olano/)
    Phase 6: Health scoring (missing files, stale memory, etc.)
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._index: dict[str, dict] = {}  # rel_path → {category, label, size, mtime}

    # ──────────────────────────────────── Public API ──────────────────────────

    async def run(self, full: bool = False) -> dict:
        """Run all cartography phases. Returns health report."""
        logger.info("Cartography: starting workspace analysis at %s", self.workspace.root)

        # Phase 1 + 2: Scan + classify
        await asyncio.get_event_loop().run_in_executor(None, self._phase_scan_and_classify)

        # Phase 3: Map (from the index phase 1+2 just built — no second walk)
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: save_workspace_map(self.workspace.root, index=self._index)
        )

        # Phase 4: Bridge
        await asyncio.get_event_loop().run_in_executor(None, self._phase_bridge)

        # Phase 5: Index (only if full=True to avoid slowness)
        if full:
            await asyncio.get_event_loop().run_in_executor(None, self._phase_index)

        # Phase 6: Health
        health = self._phase_health()

        logger.info("Cartography: done. %d files, health=%d%%", len(self._index), health["score"])
        return health

    def run_incremental(self) -> None:
        """Incremental update: one pruned walk, re-classifying only changed files.

        The map and bridge are regenerated from the resulting index — no second
        tree walk. Files deleted since the last run drop out of the index (the
        old mtime-merge only ever added).
        """
        index_path = self.workspace.workspace_index_path
        if index_path.exists():
            try:
                self._index = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                self._index = {}

        self._index = self._scan(previous=self._index)
        self._save_index()
        save_workspace_map(self.workspace.root, index=self._index)
        self._phase_bridge()

    def get_map(self) -> str:
        """Return the current workspace map (regenerating if missing)."""
        path = self.workspace.workspace_map_path
        if not path.exists():
            return generate_workspace_map(self.workspace.root)
        return path.read_text(encoding="utf-8")

    def health_score(self) -> int:
        """Return a 0–100 health score."""
        return cast(int, self._phase_health()["score"])

    # ──────────────────────────────────── Phases ─────────────────────────────

    def _load_ignore_patterns(self) -> list[str]:
        """Load patterns from the workspace's ignore file (gitignore syntax).

        ``.olanoignore``, or the pre-rename ``.lionclawignore`` when only that
        one exists (``config.workspace.workspace_ignore_file``).
        """
        from olano.config.workspace import workspace_ignore_file

        patterns: list[str] = []
        ignore_file = workspace_ignore_file(self.workspace.root)
        if ignore_file is not None:
            for line in ignore_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        return patterns

    def _scan(self, previous: dict[str, dict]) -> dict[str, dict]:
        """One pruned walk → fresh index, reusing *previous* classifications.

        A file whose mtime is unchanged keeps its cached category/label — the
        walk touches each directory once and classifies only what changed.
        Embedded repositories surface as a single ``<dir>/`` entry (see
        ``walker.iter_workspace``); everything prunable never gets stat-ed at
        all, which is what turned a 130s boot-time scan of an 11 GB workspace
        into milliseconds.
        """
        root = self.workspace.root
        ignore_patterns = self._load_ignore_patterns()
        new_index: dict[str, dict] = {}
        for kind, rel, stat in iter_workspace(root, ignore_patterns):
            if kind == "repo":
                new_index[rel] = {
                    "category": "project",
                    "label": EXTERNAL_REPO_LABEL,
                    "size": 0,
                    "mtime": 0.0,
                }
                continue
            assert stat is not None
            existing = previous.get(rel)
            if existing is not None and existing.get("mtime") == stat.st_mtime:
                new_index[rel] = existing
                continue
            try:
                category, label = classify_file(root / rel, root)
            except Exception:
                continue
            new_index[rel] = {
                "category": category,
                "label": label,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        return new_index

    def _phase_scan_and_classify(self) -> None:
        self._index = self._scan(previous={})
        self._save_index()

    def _phase_bridge(self) -> None:
        """Generate the workspace bridge — a human-readable org guide."""
        categories: dict[str, list[str]] = {}
        for rel, meta in self._index.items():
            cat = meta.get("category", "other")
            categories.setdefault(cat, []).append(rel)

        lines = ["# Workspace Organization Guide\n"]
        lines.append(
            "This workspace follows a three-zone model:\n"
            "- **User Zone**: Your files — AGENTS.md, SOUL.md, USER.md, MEMORY.md, skills/, projects/\n"
            "- **System Zone**: system/ — auto-updated by Olano, do not edit manually\n"
            "- **Extension Zone**: extensions/ — your overrides that survive system updates\n"
            "- **State Zone**: .olano/ — generated artifacts, always regeneratable\n"
        )

        memory_files = categories.get("memory:daily", [])
        if memory_files:
            lines.append(
                f"\n## Memory\n{len(memory_files)} daily logs in memories/. "
                "Today's and yesterday's are loaded automatically at session start."
            )

        skill_files = categories.get("skill", [])
        if skill_files:
            lines.append(
                f"\n## Skills\n{len(skill_files)} skill(s) in skills/. "
                "Each skill has its own directory with a SKILL.md definition file."
            )

        project_files = categories.get("project", [])
        if project_files:
            lines.append(f"\n## Projects\n{len(project_files)} project file(s) in projects/.")

        bridge = "\n".join(lines)
        self.workspace.workspace_bridge_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace.workspace_bridge_path.write_text(bridge, encoding="utf-8")

    def _phase_index(self) -> None:
        """Create per-file summaries (first 200 chars) for deep retrieval."""
        summaries: dict[str, str] = {}
        root = self.workspace.root
        for rel, meta in self._index.items():
            path = root / rel
            if meta.get("category", "").startswith("memory") or meta.get("category") in (
                "governance",
                "identity",
                "skill",
                "project",
            ):
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                    summaries[rel] = content[:200].replace("\n", " ").strip()
                except Exception:
                    pass

        index_dir = self.workspace.state_dir / "file_summaries"
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "summaries.json").write_text(
            json.dumps(summaries, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _phase_health(self) -> dict:
        """Score workspace health 0–100."""
        score = 100
        issues: list[str] = []

        required = ["AGENTS.md", "SOUL.md", "MEMORY.md", "USER.md"]
        for fname in required:
            if not (self.workspace.root / fname).exists():
                score -= 15
                issues.append(f"Missing {fname}")

        # Check memory is not stale (no logs in 7 days)
        from datetime import date, timedelta

        recent_log = False
        for i in range(7):
            d = date.today() - timedelta(days=i)
            if (self.workspace.root / "memories" / f"{d.isoformat()}.md").exists():
                recent_log = True
                break
        if not recent_log:
            score -= 10
            issues.append("No recent memory logs (last 7 days)")

        # Check MEMORY.md is not empty
        mem_path = self.workspace.root / "MEMORY.md"
        if mem_path.exists() and mem_path.stat().st_size < 50:
            score -= 5
            issues.append("MEMORY.md is nearly empty")

        score = max(0, min(100, score))
        return {
            "score": score,
            "issues": issues,
            "total_files": len(self._index),
            "timestamp": datetime.utcnow().isoformat(),
        }

    def _save_index(self) -> None:
        try:
            self.workspace.workspace_index_path.parent.mkdir(parents=True, exist_ok=True)
            self.workspace.workspace_index_path.write_text(
                json.dumps(self._index, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("Cartography: failed to save index: %s", e)
