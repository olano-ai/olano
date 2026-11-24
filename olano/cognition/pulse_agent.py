"""PulseAgent glue — execute Pulse ``run_worker`` actions and DEEP curation.

Two responsibilities:
- :func:`run_pulse_worker_action` — snapshot-then-run a worker from a pulse
  ``run_worker`` action dict, returning the outcome + a backup id for undo. Used
  by the engine's async path and the cognition drainer.
- :func:`curate_pulse` — DEEP, grounded curation that produces actionable pulse
  items (each carrying a concrete ``action``), with a FAST fallback.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from olano.cognition.backups import snapshot_backup
from olano.cognition.playbooks import PLAYBOOKS, PULSE_AGENT_PLAYBOOK
from olano.cognition.worker import WorkerOutcome, run_worker
from olano.config.workspace import WORKSPACE_STATE_DIRNAME
from olano.pulse.cost_tracker import budget_gate
from olano.pulse.models import CurationResult

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)


def _resolve_playbook(action: dict) -> str:
    key = str(action.get("playbook") or "").strip()
    return PLAYBOOKS.get(key, key)


async def run_pulse_worker_action(
    config: AgentConfig,
    workspace_root: Path,
    action: dict,
) -> tuple[WorkerOutcome, str]:
    """Snapshot the action's target file(s), then run the worker.

    Returns ``(outcome, backup_job_id)``. ``backup_job_id`` is "" when there was
    nothing to snapshot (so undo is a no-op).

    A ``cortex_promote`` action (the approval card the triage phase posts when
    Fully Autonomous is off) is run here too, so the dashboard's Approve, the
    chat's ``approvals_approve`` and the cognition drainer all take the same
    path: :func:`olano.cognition.ideation.run_promote` on the item named by
    ``item_id``, into ``target`` (or the funnel's next stage).
    """
    if str(action.get("type") or "") == "cortex_promote":
        from olano.cognition import cortex_items
        from olano.cognition.ideation import run_promote

        item_id = str(action.get("item_id") or "").strip()
        item = cortex_items.get_item(workspace_root, item_id) if item_id else None
        if item is None:
            return WorkerOutcome(status="error", error=f"Cortex item {item_id!r} not found."), ""
        if item.get("status") == "promoted":
            return (
                WorkerOutcome(
                    status="empty", summary=f"'{item.get('title')}' was already promoted."
                ),
                "",
            )
        target = str(action.get("target") or "").strip().lower() or None
        outcome, created = await run_promote(
            config, workspace_root, item, target=target, source="manual"
        )
        if created is not None:
            outcome.artifacts = outcome.artifacts or [str(created.get("path") or "")]
        return outcome, ""

    playbook = _resolve_playbook(action)
    goal = str(action.get("goal") or "").strip()
    context = action.get("context")
    tools = action.get("tools")
    model = action.get("model")

    output_hint = action.get("output_hint")
    snapshot_paths: list[str] = []
    if isinstance(output_hint, str) and output_hint.strip():
        snapshot_paths = [output_hint.strip()]
    elif isinstance(output_hint, list):
        snapshot_paths = [str(p).strip() for p in output_hint if str(p).strip()]

    backup_job = ""
    if snapshot_paths:
        backup_job = snapshot_backup(
            workspace_root, snapshot_paths, str(action.get("playbook") or "pulse_action")
        )

    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=playbook,
        goal=goal,
        context=context if isinstance(context, str) else None,
        tools_allow=list(tools) if isinstance(tools, list) else None,
        model=model if isinstance(model, str) else None,
        worker_label=str(action.get("playbook") or "pulse_action"),
    )
    return outcome, backup_job


def _parse_pulse_items(summary: str) -> list[dict]:
    """Extract the ``{"items": [...]}`` JSON object from a worker summary."""
    if not summary:
        return []
    match = re.search(r"\{.*\}", summary, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


async def curate_pulse(
    generator,
    *,
    force: bool = False,
) -> CurationResult:
    """DEEP pulse curation via a reasoning worker.

    ``generator`` is a :class:`~olano.pulse.generators.PulseGenerator`. Falls
    back to the FAST single-prompt curation when over budget or on failure.
    Returns a :class:`~olano.pulse.models.CurationResult` carrying the items
    AND why there were none, so callers can show an actionable status.
    """
    config = generator.config
    workspace_root = generator.workspace_root

    # Model readiness → don't attempt curation at all when there's no provider
    # key / no model configured (a fresh deployment). Both the deep worker AND
    # the FAST fallback need the model, so gate here and report an actionable
    # reason the dashboard can show, instead of a crash or a silent empty.
    from olano.core.model_readiness import check_model_ready

    readiness = check_model_ready(config)
    if not readiness.ok:
        reason = (
            readiness.error.message
            if readiness.error
            else "No model is configured for this agent yet."
        )
        logger.info("curate_pulse: model not ready — %s", reason)
        return CurationResult([], status="needs_setup", reason=reason)

    # Budget gate → FAST fallback. If the fallback also surfaces nothing, report
    # that the deep pass was skipped for budget (not a silent empty).
    ok, budget_msg = budget_gate(
        config.name, workspace_root / WORKSPACE_STATE_DIRNAME, config.budget
    )
    if not ok:
        logger.info("curate_pulse: over budget, using FAST curation")
        items = await generator.capture_agent_curated_pulse(force=force)
        if items:
            return CurationResult(items, status="completed")
        return CurationResult(
            [],
            status="budget_blocked",
            reason=(
                "Skipped deep web curation — the agent is over its spend budget"
                + (f" ({budget_msg})" if budget_msg else "")
                + ", and the quick fallback found nothing to surface. Raise the agent's "
                "budget (Cortex → budget) or wait for the daily reset."
            ),
        )

    goal = (
        "Produce 1-3 grounded, actionable PULSE items for the user based on their "
        "identity, interests, memory, recent activity, and ideas. Return ONLY the JSON "
        "described in your instructions."
    )
    # Show prior approved/rejected items so we don't repeat them, while still
    # exploring fresh angles on the same interests.
    try:
        from olano.cognition.ideation import history_digest

        history = history_digest(config, workspace_root)
    except Exception:
        history = None
    # Steer curation with the user's optional CORTEX.md creative direction.
    from olano.cognition.guidance import prepend_guidance

    history = prepend_guidance(workspace_root, history) or None
    outcome = await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=PULSE_AGENT_PLAYBOOK,
        goal=goal,
        context=history,
        tools_allow=[
            "web_search",
            "web_fetch",
            "arxiv_search",
            "memory_read",
            "memory_search",
        ],
        worker_label="pulse_agent",
    )
    if outcome.status == "needs_setup":
        # The model became unreachable between the preflight and the run — skip
        # the FAST fallback (it needs the model too) and surface the reason.
        return CurationResult(
            [],
            status="needs_setup",
            reason=outcome.error or "The model is not configured for this agent yet.",
        )

    if outcome.status not in ("completed", "empty"):
        logger.info("curate_pulse: worker %s, using FAST curation", outcome.status)
        items = await generator.capture_agent_curated_pulse(force=force)
        if items:
            return CurationResult(items, status="completed")
        # The deep worker failed AND the fallback surfaced nothing: propagate the
        # real failure (with the raw error) so it can be classified into guidance.
        if outcome.status == "budget_blocked":
            return CurationResult(
                [],
                status="budget_blocked",
                reason=outcome.error or "The agent is over its spend budget.",
            )
        return CurationResult(
            [],
            status="error",
            reason="The pulse curation worker could not complete.",
            error=outcome.error or outcome.status,
        )

    raw_items = _parse_pulse_items(outcome.summary)
    if not raw_items:
        return CurationResult(
            [],
            status="empty",
            reason="The curation worker ran but found nothing worth surfacing this time.",
        )

    items = generator.add_curated_items(raw_items)
    if not items:
        return CurationResult(
            [],
            status="empty",
            reason="The curation worker returned items, but none were usable after parsing.",
        )
    return CurationResult(items, status="completed")
