"""Tiered FAST/DEEP execution router for cognition tasks.

Keeps routine, high-frequency work cheap (a single ``init_chat_model`` call via a
caller-supplied ``fast_fn``) while escalating high-value work to a full
:func:`~olano.cognition.worker.run_worker` reasoning run. Escalation is
budget-aware and capped per day so autonomous background work can never run away.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olano.cognition.worker import run_worker
from olano.config.system_settings import SystemSettings, cortex_enabled, load_system_settings
from olano.config.workspace import WORKSPACE_STATE_DIRNAME
from olano.pulse.cost_tracker import budget_gate

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig, CognitionConfig

logger = logging.getLogger(__name__)


class Tier(str, Enum):
    FAST = "fast"  # single cheap LLM call (caller's fast_fn)
    DEEP = "deep"  # full headless reasoning worker


@dataclass
class TaskSpec:
    name: str
    default_tier: Tier
    deep_cadence: str = ""  # informational cron hint


# Default policy table — callers may use these or build their own TaskSpec.
TASK_SPECS: dict[str, TaskSpec] = {
    "fact_distill": TaskSpec("fact_distill", Tier.FAST),
    "idea_capture": TaskSpec("idea_capture", Tier.FAST),
    "skill_extract": TaskSpec("skill_extract", Tier.FAST),
    "weekly_distillation": TaskSpec("weekly_distillation", Tier.DEEP, "weekly"),
    "memory_garden": TaskSpec("memory_garden", Tier.DEEP, "daily"),
    "pulse_curate": TaskSpec("pulse_curate", Tier.DEEP, "0 */6 * * *"),
    "idea_expand": TaskSpec("idea_expand", Tier.DEEP),
    "brainstorm": TaskSpec("brainstorm", Tier.DEEP),
    "prototype": TaskSpec("prototype", Tier.DEEP),
    "workspace_reorg": TaskSpec("workspace_reorg", Tier.DEEP),
    "skillsmith": TaskSpec("skillsmith", Tier.DEEP),
    "workspace_optimize": TaskSpec("workspace_optimize", Tier.DEEP, "weekly"),
    "skill_librarian": TaskSpec("skill_librarian", Tier.DEEP, "weekly"),
}


def effective_fast_model(
    cognition_cfg: CognitionConfig, settings: SystemSettings | None = None
) -> str:
    settings = settings or load_system_settings()
    return getattr(cognition_cfg, "fast_model", None) or settings.fast_model


def effective_deep_model(
    cognition_cfg: CognitionConfig,
    parent_model: str,
    settings: SystemSettings | None = None,
) -> str:
    settings = settings or load_system_settings()
    return getattr(cognition_cfg, "deep_model", None) or settings.deep_model or parent_model


def _counter_path(workspace_root: Path) -> Path:
    return workspace_root / WORKSPACE_STATE_DIRNAME / "state" / "cognition" / "deep_counter.json"


# Public aliases — the CortexEngine enforces the SAME daily deep-run cap across
# every autonomous phase (not just run_tiered callers), so it needs to read and
# bump the shared counter directly.
def deep_runs_today(workspace_root: Path) -> int:
    """Number of DEEP cognition runs recorded today (shared across all phases)."""
    return _deep_runs_today(workspace_root)


def record_deep_run(workspace_root: Path) -> None:
    """Increment today's DEEP run counter. Call once per deep phase invocation."""
    _record_deep_run(workspace_root)


def _deep_runs_today(workspace_root: Path) -> int:
    path = _counter_path(workspace_root)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get(date.today().isoformat(), 0))
    except Exception:
        return 0


def _record_deep_run(workspace_root: Path) -> None:
    path = _counter_path(workspace_root)
    today = date.today().isoformat()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    data = {today: int(data.get(today, 0)) + 1}  # keep only today's count
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("router: failed to record deep run: %s", e)


async def run_tiered(
    spec: TaskSpec,
    *,
    parent_config: AgentConfig,
    workspace_root: Path,
    cognition_cfg: CognitionConfig,
    fast_fn: Callable[[], Awaitable[Any]] | None = None,
    deep_playbook: str | None = None,
    deep_goal: str | None = None,
    deep_tools: list[str] | None = None,
    deep_context: str | None = None,
    deep_max_iterations: int | None = None,
    deep_timeout: float | None = None,
    budget_state_dir: Path | None = None,
) -> tuple[Tier, Any]:
    """Run ``spec`` at the appropriate tier and return ``(tier_used, result)``.

    DEEP result is a ``WorkerOutcome``; FAST result is whatever ``fast_fn``
    returns (or ``None`` when no fast path is available). If the DEEP worker
    fails (timeout/error/rejected), falls back to ``fast_fn`` when present.
    """
    budget_state_dir = budget_state_dir or (workspace_root / WORKSPACE_STATE_DIRNAME)

    want_deep = spec.default_tier == Tier.DEEP
    force = getattr(cognition_cfg, "force_tier", None)
    if force == "fast":
        want_deep = False
    elif force == "deep":
        want_deep = True

    # `cognition.enabled` is three-state (None ⇒ inherit the global), so it can
    # only be read through the resolver — a bare truthiness check would route
    # every inheriting agent down the FAST path.
    can_deep = bool(want_deep and cortex_enabled(parent_config) and deep_playbook and deep_goal)

    deep_model: str | None = None
    if can_deep:
        ok, msg = budget_gate(parent_config.name, budget_state_dir, parent_config.budget)
        if not ok:
            logger.info("router[%s]: over budget — FAST fallback (%s)", spec.name, msg)
            can_deep = False
        else:
            cap = int(getattr(cognition_cfg, "max_deep_runs_per_day", 12))
            if _deep_runs_today(workspace_root) >= cap:
                logger.info(
                    "router[%s]: daily deep cap (%d) reached — FAST fallback", spec.name, cap
                )
                can_deep = False
            else:
                deep_model = effective_deep_model(cognition_cfg, parent_config.model)
                # Near-budget warning ⇒ downgrade to the configured fallback model.
                fallback = getattr(parent_config.budget, "fallback_model", None)
                if msg and fallback:
                    deep_model = fallback

    if can_deep:
        _record_deep_run(workspace_root)
        outcome = await run_worker(
            parent_config=parent_config,
            workspace_root=workspace_root,
            playbook=deep_playbook or "",
            goal=deep_goal or "",
            context=deep_context,
            tools_allow=deep_tools,
            model=deep_model,
            # `None` lets run_worker resolve the two-tier budget itself
            # (effective_worker_budget); a spec that names its own wins.
            max_iterations=deep_max_iterations or None,
            timeout=deep_timeout or None,
            budget_check=False,  # already gated above
            worker_label=spec.name,
        )
        if outcome.status in ("completed", "empty"):
            return Tier.DEEP, outcome
        logger.warning(
            "router[%s]: deep worker status=%s — falling back to FAST", spec.name, outcome.status
        )

    if fast_fn is not None:
        return Tier.FAST, await fast_fn()
    return Tier.FAST, None
