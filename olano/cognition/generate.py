"""Manual "Think" orchestrator — thin shim over the CortexEngine.

Historically this module owned the phase list and the per-phase runners. That
logic now lives in :mod:`olano.cognition.orchestrator` (the CortexEngine), so
the autonomous scheduler and the dashboard "Think" button share ONE code path.

``generate_all`` simply drives the engine's Think funnel with ``force=True`` (a
manual run bypasses the daily deep-run cap but still respects the budget) and
yields the same live progress events the dashboard already consumes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olano.cognition.orchestrator import THINK_CYCLE_ORDER, run_cycle

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

# Re-exported for callers that introspect the funnel order (e.g. the dashboard).
PHASE_IDS: list[str] = THINK_CYCLE_ORDER


async def generate_all(
    config: AgentConfig,
    workspace_root: Path,
    *,
    phases: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run the Think funnel on demand, yielding progress events.

    Event shape (unchanged):
      {"event": "phase_start", "id", "label", "running", "index", "total"}
      {"event": "phase_done", "id", "label", "count", "summary", "backup_job", "status"}
      {"event": "all_done", "total_artifacts"}
    """
    async for event in run_cycle(
        config, workspace_root, phase_ids=phases, source="manual", force=True
    ):
        yield event
