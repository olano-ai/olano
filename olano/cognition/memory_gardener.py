"""MemoryGardener — keep MEMORY.md and daily logs clean.

Two passes:
1. A free, deterministic pass (``dedupe_facts`` + ``roll_up_daily_logs``) that
   always runs.
2. A DEEP reasoning worker that merges near-duplicate/semantically-equivalent
   facts a regex cannot catch and rewrites MEMORY.md cleanly.

A pre-rewrite snapshot (undoable via Pulse) and a ``.garden.lock`` protect
against data loss and concurrent writers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from olano.cognition.backups import snapshot_backup
from olano.cognition.playbooks import MEMORY_GARDENER_PLAYBOOK
from olano.cognition.worker import WorkerOutcome, run_worker

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.memory.markdown_memory import MarkdownMemory

logger = logging.getLogger(__name__)


def _garden_lock_path(workspace_root: Path) -> Path:
    from olano.config.workspace import WORKSPACE_STATE_DIRNAME

    return workspace_root / WORKSPACE_STATE_DIRNAME / "state" / "memory" / ".garden.lock"


def is_garden_locked(workspace_root: Path) -> bool:
    """True while a memory-gardening rewrite is in progress."""
    return _garden_lock_path(workspace_root).exists()


async def run_memory_gardener(
    parent_config: AgentConfig,
    workspace_root: Path,
    markdown_memory: MarkdownMemory,
    *,
    budget_check: bool = True,
) -> WorkerOutcome:
    """Run the deterministic dedup pass, then the DEEP semantic-merge worker."""
    # 1. Free deterministic pass — always runs, even if the worker is budget-blocked.
    removed = markdown_memory.dedupe_facts()
    rolled = markdown_memory.roll_up_daily_logs()
    det_note = f"deterministic: removed {removed} dupe(s), archived {rolled} log(s)"

    # 2. Lock so the per-cycle distill backs off while we rewrite.
    lock = _garden_lock_path(workspace_root)
    if lock.exists():
        return WorkerOutcome(status="rejected", error="memory garden lock held", summary=det_note)

    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("locked", encoding="utf-8")

    try:
        # 3. Snapshot MEMORY.md so the rewrite is undoable.
        snapshot_backup(workspace_root, ["MEMORY.md"], "memory_garden")

        # 4. DEEP semantic merge.
        outcome = await run_worker(
            parent_config=parent_config,
            workspace_root=workspace_root,
            playbook=MEMORY_GARDENER_PLAYBOOK,
            goal=(
                "Merge near-duplicate and semantically-equivalent facts in MEMORY.md "
                "(keep the newest-dated form), fix obvious inconsistencies, and rewrite "
                "it cleanly grouped by section. Never delete a unique fact. Preserve the "
                "'## Knowledge Graph' section verbatim."
            ),
            tools_allow=["memory_read", "memory_search", "memory_compact"],
            budget_check=budget_check,
            worker_label="memory_garden",
        )
    finally:
        lock.unlink(missing_ok=True)

    # Fold the deterministic counts into the outcome summary.
    outcome.summary = (outcome.summary + f"\n({det_note})").strip() if outcome.summary else det_note
    if "MEMORY.md" not in outcome.artifacts:
        outcome.artifacts.append("MEMORY.md")
    return outcome
