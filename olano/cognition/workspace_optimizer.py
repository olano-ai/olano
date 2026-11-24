"""Autonomous workspace optimizer — keeps SOUL/AGENTS/IDENTITY/MEMORY clean.

Reorganizes and deduplicates the agent's core identity/behavior/memory files in
place. A snapshot is taken first (so the user can Undo from a single Pulse
action), then a DEEP reasoning worker rewrites the files, then a FINDING Pulse
summarizes what changed and carries a ``restore_backup`` action for one-click undo.

Mirrors the pattern of :mod:`olano.cognition.memory_gardener`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from olano.cognition.backups import snapshot_backup
from olano.cognition.playbooks import WORKSPACE_OPTIMIZER_PLAYBOOK
from olano.cognition.worker import WorkerOutcome, run_worker
from olano.config.workspace import WORKSPACE_STATE_DIRNAME

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)

_TARGET_FILES = ["IDENTITY.md", "SOUL.md", "AGENTS.md", "MEMORY.md"]

# Daily-log memory files frequently accumulate repetitive/duplicated entries; the
# optimizer dedupes the most recent ones too (cap to keep the snapshot bounded).
_MAX_MEMORY_LOGS = 14

_GOAL = (
    "Read IDENTITY.md, SOUL.md, AGENTS.md, MEMORY.md AND the recent memories/*.md "
    "daily logs listed in your context. For each: merge reworded duplicates, remove "
    "contradictions, group related content under clear headings, and rewrite the "
    "improved file in place. The memories/*.md daily logs in particular tend to hold "
    "lots of repetitive/duplicated entries — dedupe them aggressively while keeping "
    "every unique fact. Move content to the file where it belongs. Preserve the "
    "agent's voice and every unique instruction; consolidate, never erase. End with "
    "an ARTIFACTS: line listing the files you changed."
)


def _recent_memory_logs(workspace_root: Path) -> list[str]:
    """Workspace-relative paths of the most recent memories/*.md daily logs."""
    mem_dir = workspace_root / "memories"
    if not mem_dir.exists():
        return []
    logs = sorted(
        (p for p in mem_dir.glob("*.md") if p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )
    return [str(p.relative_to(workspace_root)) for p in logs[:_MAX_MEMORY_LOGS]]


async def run_workspace_optimizer(
    parent_config: AgentConfig,
    workspace_root: Path,
    *,
    budget_check: bool = True,
) -> tuple[WorkerOutcome, str]:
    """Optimize the agent's identity/behavior/memory files in place.

    Returns ``(outcome, backup_job_id)``. ``backup_job_id`` is "" when there was
    nothing to snapshot.
    """
    memory_logs = _recent_memory_logs(workspace_root)
    present = [name for name in _TARGET_FILES if (workspace_root / name).exists()]
    snapshot_targets = present + memory_logs
    backup_job = (
        snapshot_backup(workspace_root, snapshot_targets, "workspace_optimize")
        if snapshot_targets
        else ""
    )

    # Use the cognition DEEP model — consolidating identity/memory files is heavy
    # reasoning. Falls back to the agent's own model when none is configured.
    from olano.cognition.router import effective_deep_model

    deep_model = effective_deep_model(parent_config.cognition, parent_config.model)

    context = (
        "Recent memories/*.md daily logs to also dedupe (workspace-relative):\n"
        + "\n".join(f"- {p}" for p in memory_logs)
        if memory_logs
        else None
    )

    outcome = await run_worker(
        parent_config=parent_config,
        workspace_root=workspace_root,
        playbook=WORKSPACE_OPTIMIZER_PLAYBOOK,
        goal=_GOAL,
        context=context,
        tools_allow=["read_file", "write_file", "ls", "memory_read", "memory_search"],
        model=deep_model,
        budget_check=budget_check,
        worker_label="workspace_optimize",
    )
    _post_result_pulse(
        parent_config,
        workspace_root,
        title="Optimized identity & memory files",
        outcome=outcome,
        references=outcome.artifacts or snapshot_targets,
        backup_job=backup_job,
    )
    return outcome, backup_job


def _post_result_pulse(
    config: AgentConfig,
    workspace_root: Path,
    *,
    title: str,
    outcome: WorkerOutcome,
    references: list[str],
    backup_job: str,
) -> None:
    """Post a FINDING Pulse summarizing an autonomous optimization, with Undo."""
    if outcome.status not in ("completed", "empty"):
        return
    try:
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane, PulsePriority, PulseType

        engine = PulseEngine(config, workspace_root / WORKSPACE_STATE_DIRNAME)
        action = {"type": "restore_backup", "job_id": backup_job} if backup_job else None
        engine.add(
            content=outcome.summary or "Workspace files optimized.",
            source="cognition",
            lane=PulseLane.PULSE,
            type=PulseType.FINDING,
            priority=PulsePriority.LOW,
            title=title,
            why_now="Automatic self-optimization ran in the background.",
            suggested_next_step="Review the changes; use Undo to revert if needed.",
            references=references,
            action=action,
            metadata={"kind": "auto_optimization", "auto_applied": True},
            dedup_window_hours=0.0,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("workspace_optimizer: result pulse failed: %s", e)
