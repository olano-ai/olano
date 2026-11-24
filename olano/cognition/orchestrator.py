"""CortexEngine — the always-on autonomous self-improvement engine.

This is the single code path for ALL of Olano's agentic cognition. The same
engine powers two surfaces:

- **Autonomous** (default): the scheduler registers one cron job per enabled
  phase, at a cadence derived from the home-wide *intensity* preset. No human
  click required — the agent learns, brainstorms, prototypes, optimizes, and
  self-improves on its own.
- **Manual override**: the dashboard "Think" button (and the per-phase buttons)
  call :func:`run_cycle` / :func:`run_phase` with ``force=True`` to run the same
  phases on demand.

Design guarantees (matching the product decisions):

- **Fully agentic — no ``model.ainvoke``.** Every phase runs a real
  :func:`~olano.cognition.worker.run_worker` reasoning agent (or a runner that
  does). The cheap single-shot paths are gone.
- **Deep-only, skip when capped.** Autonomous phases NEVER fall back to a shallow
  pass: when the daily deep-run cap or the budget is exhausted, the phase is
  *skipped* (recorded in the activity log) rather than run cheaply.
- **Auto-apply + one-click undo, guard identity.** Reversible phases apply their
  changes immediately behind a snapshot and surface a LOW-priority FINDING pulse
  carrying a one-click ``restore_backup`` undo. Phases that would change the
  agent's *identity/behaviour semantics* (self-improve → SOUL.md/AGENTS.md) are
  ``guarded``: they only ever write *proposals* and surface an APPROVAL pulse.
- **One cap, counted once.** The engine is the sole enforcer of the shared daily
  deep-run cap across every phase (the per-runner ``run_worker`` budget gate still
  applies as a hard floor, but the engine pre-gates and records exactly once).

The phase registry below is the source of truth; the scheduler and the API both
read it so there is never a second list to keep in sync.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olano.cognition.reliability import (
    bell_streak_threshold,
    clear_failures,
    holds_for_streak,
    record_failure,
)
from olano.config.system_settings import (
    DEFAULT_INTENSITY,
    effective_intensity,
    feature_enabled,
)
from olano.config.workspace import WORKSPACE_STATE_DIRNAME

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)


# ───────────────────────────── live "running" registry ──────────────────────
# Phases currently executing (agent_name → Counter of phase_id → in-flight count).
# The runtime is a single asyncio loop, so a plain dict is safe. A REFCOUNT (not a
# bare set) so two overlapping runs of the same phase — e.g. a scheduled run and a
# manual click — keep the flag lit until the LAST one finishes. Lets the dashboard
# show a live "running" indicator for scheduled/background, manual Run-now, and
# Optimize-button runs.
_RUNNING: dict[str, Counter[str]] = {}


def running_phases(agent_name: str) -> list[str]:
    """Phase ids currently executing for an agent (for the dashboard status)."""
    counts = _RUNNING.get(agent_name)
    return sorted(pid for pid, n in counts.items() if n > 0) if counts else []


@contextmanager
def phase_running(agent_name: str, phase_id: str) -> Iterator[None]:
    """Mark a phase as running for the duration of the block (refcounted)."""
    _RUNNING.setdefault(agent_name, Counter())[phase_id] += 1
    try:
        yield
    finally:
        counts = _RUNNING.get(agent_name)
        if counts is not None:
            counts[phase_id] -= 1
            if counts[phase_id] <= 0:
                del counts[phase_id]
            if not counts:
                _RUNNING.pop(agent_name, None)


def _phase_streak_label(phase: Phase) -> str:
    """Key for the per-phase failure streak (distinct from the worker's own)."""
    return f"phase:{phase.id}"


# ───────────────────────────── intensity → cadence ──────────────────────────

# Daily DEEP-run cap per intensity. The engine skips (never cheapens) any phase
# once today's count reaches the cap.
DEEP_CAP: dict[str, int] = {"relaxed": 6, "balanced": 12, "aggressive": 24}

# Background distillation interval (seconds) for the SelfLearningService, by
# intensity. "Aggressive" distills hourly; "relaxed" every few hours.
DISTILL_INTERVAL_SECONDS: dict[str, int] = {
    "relaxed": 14400,  # 4h
    "balanced": 7200,  # 2h
    "aggressive": 3600,  # 1h
}


# An unknown intensity reads as the default preset in every table below, the
# same fallback the settings model applies, so a typo costs the cheapest
# cadence rather than the busiest one.
def deep_cap(intensity: str) -> int:
    return DEEP_CAP.get(intensity, DEEP_CAP[DEFAULT_INTENSITY])


def distill_interval_seconds(intensity: str) -> int:
    return DISTILL_INTERVAL_SECONDS.get(intensity, DISTILL_INTERVAL_SECONDS[DEFAULT_INTENSITY])


# ────────────────────────────── phase registry ──────────────────────────────


@dataclass(frozen=True)
class Phase:
    """One cognition phase: how it's gated, scheduled, run, and surfaced."""

    id: str
    label: str
    running: str  # present-participle status verb for live progress
    feature: str  # system_settings feature gate
    policy: str  # "auto" (apply+undo) | "guarded" (propose) | "readonly"
    cadence: dict[str, str]  # intensity → cron string for autonomous scheduling
    in_think_cycle: bool = True  # part of the manual "Think" funnel
    posts_own_pulse: bool = False  # runner already surfaces its own pulse


# Cron cadences are staggered by minute/hour to avoid a thundering herd when many
# phases fire on the same day. "Aggressive" runs the creative funnel several times
# a day; "relaxed" keeps everything weekly-ish.
PHASES: list[Phase] = [
    Phase(
        id="pulse",
        label="Pulse",
        running="Curating fresh Pulse from the web…",
        feature="pulse",
        policy="auto",
        posts_own_pulse=True,  # pulse items ARE the output
        cadence={"relaxed": "0 9 * * *", "balanced": "0 */12 * * *", "aggressive": "0 */6 * * *"},
    ),
    Phase(
        id="brainstorm",
        label="Brainstorming",
        running="Brainstorming…",
        feature="brainstorming",
        policy="auto",
        cadence={
            "relaxed": "0 10 * * 1,4",
            "balanced": "0 10 * * *",
            "aggressive": "0 2,10,18 * * *",
        },
    ),
    Phase(
        id="ideas",
        label="Ideas",
        running="Generating new ideas…",
        feature="idea_generation",
        policy="auto",
        cadence={
            "relaxed": "30 10 * * 1,4",
            "balanced": "30 10 * * *",
            "aggressive": "30 3,11,19 * * *",
        },
    ),
    Phase(
        id="prototype",
        label="Prototyping",
        running="Scaffolding a prototype…",
        feature="prototyping",
        policy="auto",
        cadence={
            "relaxed": "0 11 * * 1",
            "balanced": "0 11 * * 1,4",
            "aggressive": "0 12 * * *",
        },
    ),
    Phase(
        id="triage",
        label="Rate & Promote",
        running="Rating items and promoting the strongest…",
        feature="idea_generation",  # the funnel's own review step
        policy="auto",
        # Runs an hour before Advance Work at every intensity so the execution
        # run picks up fresh ratings and freshly promoted items.
        cadence={
            "relaxed": "0 12 * * 0",
            "balanced": "0 12 * * 1,4",
            "aggressive": "0 13 * * *",
        },
    ),
    Phase(
        id="advance",
        label="Advance Work",
        running="Developing existing ideas & prototypes…",
        feature="idea_generation",  # convergence/development of captured items
        policy="auto",
        cadence={
            "relaxed": "0 13 * * 0",
            "balanced": "0 13 * * 1,4",
            "aggressive": "0 14 * * *",
        },
    ),
    Phase(
        id="skills",
        label="Learned Skills",
        running="Learning reusable skills…",
        feature="skills_optimization",
        policy="auto",
        posts_own_pulse=True,  # learned-skills pulse is posted by the service
        cadence={
            "relaxed": "0 5 * * 0",
            "balanced": "0 5 * * 1,4",
            "aggressive": "0 5 * * *",
        },
    ),
    Phase(
        id="improve",
        label="Self-Improve",
        running="Self-improving…",
        feature="workspace_optimization",
        policy="guarded",  # proposes SOUL.md/AGENTS.md edits → approval lane
        posts_own_pulse=True,  # the service posts approve-to-apply PROPOSAL items
        cadence={
            "relaxed": "0 6 * * 0",
            "balanced": "0 6 * * 0",
            "aggressive": "0 6 * * 1,4",
        },
    ),
    Phase(
        id="checks",
        label="Self-Checks",
        running="Running self-checks…",
        feature="self_learning",
        policy="readonly",
        cadence={
            "relaxed": "0 7 * * 0",
            "balanced": "0 7 * * *",
            "aggressive": "0 7 * * *",
        },
    ),
    # ── standalone optimizers (not part of the Think funnel) ──
    Phase(
        id="memory_garden",
        label="Memory",
        running="Gardening memory…",
        feature="memory_optimization",
        policy="auto",
        in_think_cycle=False,
        # Once a day at every intensity: the free deterministic dedupe already
        # runs on every distill cycle, so the semantic merge has little to do
        # twice in twelve hours — and each extra firing is one more DEEP run
        # competing for the agent's own provider quota.
        cadence={
            "relaxed": "30 4 * * *",
            "balanced": "30 4 * * *",
            "aggressive": "30 4 * * *",
        },
    ),
    Phase(
        id="workspace_optimize",
        label="Workspace",
        running="Optimizing workspace…",
        feature="workspace_optimization",
        policy="auto",  # lossless dedupe of identity/memory files (snapshot+undo)
        posts_own_pulse=True,
        in_think_cycle=False,
        cadence={
            "relaxed": "0 4 * * 0",
            "balanced": "0 4 * * 0",
            "aggressive": "0 4 */3 * *",
        },
    ),
    Phase(
        id="skill_librarian",
        label="Skill Library",
        running="Organizing skill library…",
        feature="skills_optimization",
        policy="auto",
        posts_own_pulse=True,
        in_think_cycle=False,
        cadence={
            "relaxed": "30 4 * * 0",
            "balanced": "30 4 * * 0",
            "aggressive": "30 4 */3 * *",
        },
    ),
]

PHASE_BY_ID: dict[str, Phase] = {p.id: p for p in PHASES}

# Order of the manual "Think" funnel (diverge → converge → build → learn → reflect).
THINK_CYCLE_ORDER: list[str] = [p.id for p in PHASES if p.in_think_cycle]

# Phases the autonomous scheduler registers cron jobs for (everything; distillation
# is driven separately by the SelfLearningService timer).
AUTONOMOUS_PHASE_IDS: list[str] = [p.id for p in PHASES]


def cadence_for(phase: Phase | str, intensity: str) -> str:
    """Cron string for a phase at a given intensity (falls back to the default preset)."""
    p = phase if isinstance(phase, Phase) else PHASE_BY_ID[phase]
    return p.cadence.get(intensity) or p.cadence.get(DEFAULT_INTENSITY) or "0 4 * * 0"


# ──────────────────────────────── deep gate ─────────────────────────────────


def _deep_model(config: AgentConfig) -> str | None:
    from olano.cognition.router import effective_deep_model

    try:
        return effective_deep_model(config.cognition, config.model)
    except Exception:
        return None


def deep_gate(config: AgentConfig, workspace_root: Path, *, force: bool) -> tuple[bool, str]:
    """Shared budget + daily-cap gate. Returns (ok, reason_if_skipped).

    Budget is always respected (even on manual ``force`` runs). The daily deep-run
    cap is enforced only for autonomous runs — a human pressing "Think now" can
    always override it.
    """
    from olano.cognition.router import deep_runs_today
    from olano.pulse.cost_tracker import budget_gate

    try:
        ok, msg = budget_gate(config.name, workspace_root / WORKSPACE_STATE_DIRNAME, config.budget)
        if not ok:
            return False, f"budget exhausted ({msg})"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("cortex: budget_gate failed (allowing): %s", exc)

    if not force:
        intensity = effective_intensity(config)
        cap = deep_cap(intensity)
        if deep_runs_today(workspace_root) >= cap:
            return False, f"daily deep-run cap reached ({cap}/{cap}, intensity={intensity})"
    return True, ""


# ──────────────────────────────── runners ───────────────────────────────────


@dataclass
class _RunnerResult:
    status: str  # completed | empty | skipped | error | budget_blocked
    count: int = 0
    summary: str = ""
    backup_job: str = ""
    artifacts: list[str] = field(default_factory=list)
    error: str = ""  # raw worker error message when status == "error"


def classify_cognition_error(msg: str) -> tuple[str, str]:
    """Turn a raw worker/model error into ``(code, human_message)``.

    Codes: ``vault_locked`` | ``missing_secret`` | ``bad_api_key`` |
    ``model_unavailable`` | ``error``. The human message is what the user sees in
    the Cortex UI so a failed phase explains *why* instead of a bare "error".
    """
    low = (msg or "").lower()
    # Locked/missing credential — the most common cause for a fresh agent.
    secret = ""
    import re as _re

    m = _re.search(r"\b([A-Z][A-Z0-9_]{2,})\b(?=.*(?:key|token|secret|credential|env))", msg or "")
    if m:
        secret = m.group(1)
    if "vault" in low and ("lock" in low or "unlock" in low):
        return "vault_locked", (
            f"The vault is locked — unlock it so Cortex can use {secret or 'this credential'}."
        )
    if any(
        n in low
        for n in (
            "api key",
            "api_key",
            "apikey",
            "unauthorized",
            "401",
            "invalid key",
            "incorrect api key",
        )
    ):
        return (
            "bad_api_key",
            "The model's API key is missing or invalid. Check it in the Vault / agent config.",
        )
    if any(n in low for n in ("not found in vault", "missing secret", "no secret", "${vault:")):
        return "missing_secret", (
            f"A required credential{f' ({secret})' if secret else ''} isn't in the vault."
        )
    if any(
        n in low
        for n in (
            "not found",
            "does not exist",
            "unknown model",
            "unsupported",
            "no such model",
            "model_not_found",
        )
    ):
        return (
            "model_unavailable",
            "The configured model is unavailable or unsupported. Check the agent's model + provider.",
        )
    if any(
        n in low
        for n in ("connection", "timed out", "timeout", "network", "getaddrinfo", "503", "502")
    ):
        return (
            "model_unavailable",
            "Couldn't reach the model provider (network/timeout). Try again shortly.",
        )
    short = (msg or "Unknown error").strip().splitlines()[0][:200]
    return "error", short


# Cognition-local codes that carry their OWN remediation (the unified
# actionable-errors classifier doesn't know about the credentials vault).
def _vault_remediation(secret: str) -> dict[str, Any]:
    return {
        "kind": "unlock_vault",
        "summary": (
            f"Unlock the credentials vault so Cortex can use {secret}."
            if secret
            else "Unlock the credentials vault so Cortex can use this credential."
        ),
        "action": {"type": "unlock_vault", "label": "Unlock vault"},
    }


def classify_cognition_error_full(
    msg: str, config: AgentConfig | None = None
) -> tuple[str, str, dict[str, Any] | None]:
    """``(code, human_message, remediation|None)`` for a worker/model error.

    Vault/secret problems are cognition-specific (the vault keeps the ``code``
    stable at ``vault_locked``/``missing_secret`` so the dashboard's per-row
    "Unlock vault" affordance keeps working). Everything else is routed through
    the unified :mod:`olano.core.actionable_errors` classifier — passing the
    agent's model provider — so an expired ``openai-codex`` sign-in, a bad API
    key, low credits, or a rate limit all come back with the SAME one-click
    remediation the chat surface uses (reconnect / set key / top up / retry).
    """
    code, human = classify_cognition_error(msg)

    # Keep vault-specific codes + remediation as-is (frontend keys off these).
    if code in ("vault_locked", "missing_secret"):
        import re as _re

        m = _re.search(
            r"\b([A-Z][A-Z0-9_]{2,})\b(?=.*(?:key|token|secret|credential|env))", msg or ""
        )
        secret = m.group(1) if m else ""
        return code, human, _vault_remediation(secret)

    # Route the rest through the unified classifier for rich remediation.
    try:
        from olano.core.actionable_errors import classify_exception

        provider = (config.model.partition(":")[0] or None) if config else None
        actionable = classify_exception(Exception(msg or ""), provider)
        if actionable is not None:
            rem = actionable.remediation.to_dict() if actionable.remediation else None
            return actionable.code, actionable.message, rem
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("cortex: actionable_errors classify failed: %s", exc)

    return code, human, None


async def _run_worker_deep(
    config: AgentConfig,
    workspace_root: Path,
    *,
    playbook: str,
    goal: str,
    tools: list[str],
    label: str,
    context: str | None = None,
) -> Any:
    """Run a headless deepagent (budget already gated by the engine)."""
    from olano.cognition.worker import run_worker

    return await run_worker(
        parent_config=config,
        workspace_root=workspace_root,
        playbook=playbook,
        goal=goal,
        context=context,
        tools_allow=tools,
        model=_deep_model(config),
        budget_check=False,  # engine pre-gated budget + cap
        worker_label=label,
    )


async def _dispatch(phase: Phase, config: AgentConfig, workspace_root: Path) -> _RunnerResult:
    """Invoke the phase's underlying agentic runner; normalize the result."""
    pid = phase.id

    if pid == "pulse":
        from olano.pulse.generators import PulseGenerator

        res = await PulseGenerator(config, workspace_root).capture_pulse_agent(
            force=True, bypass_gates=True
        )
        if res.status == "error":
            # A real worker/model failure — propagate so run_phase classifies it
            # into a code + remediation the user can act on.
            return _RunnerResult(
                status="error",
                summary=res.reason or "Pulse curation failed.",
                error=res.error or res.reason,
            )
        if res.status in ("skipped", "budget_blocked"):
            return _RunnerResult(status="skipped", summary=res.reason)
        # completed / empty — surface the count, and the reason when empty so the
        # user knows it ran but found nothing (vs. silently "0").
        if res.items:
            return _RunnerResult(
                status="completed",
                count=len(res.items),
                summary=f"Curated {len(res.items)} pulse item(s).",
            )
        return _RunnerResult(
            status="empty",
            count=0,
            summary=res.reason or "Ran, but found nothing new worth surfacing.",
        )

    if pid == "brainstorm":
        from olano.cognition.ideation import run_brainstorm

        outcome, count, backup = await run_brainstorm(config, workspace_root, budget_check=False)
        return _RunnerResult(
            outcome.status,
            count,
            outcome.summary or "Brainstormed.",
            backup,
            outcome.artifacts,
            error=outcome.error or "",
        )

    if pid == "ideas":
        from olano.cognition.ideation import run_idea_generation

        outcome, count, backup = await run_idea_generation(
            config, workspace_root, budget_check=False
        )
        return _RunnerResult(
            outcome.status,
            count,
            outcome.summary or f"Added {count} idea(s).",
            backup,
            outcome.artifacts,
            error=outcome.error or "",
        )

    if pid == "prototype":
        # Only build when there's a fresh idea worth building (avoid burning the
        # scaffolding worker on nothing).
        from olano.cognition import cortex_items

        if cortex_items.counts_by_kind(workspace_root).get("idea", 0) <= 0:
            return _RunnerResult(status="skipped", summary="No fresh idea to build.")
        from olano.cognition.ideation import run_prototype

        outcome, count, backup = await run_prototype(config, workspace_root, budget_check=False)
        return _RunnerResult(
            outcome.status,
            count,
            outcome.summary or f"Scaffolded {count} prototype(s).",
            backup,
            outcome.artifacts,
            error=outcome.error or "",
        )

    if pid == "triage":
        from olano.cognition.triage import run_triage

        outcome, count, _ = await run_triage(config, workspace_root, budget_check=False)
        return _RunnerResult(
            outcome.status,
            count,
            outcome.summary or "Rated active items.",
            "",
            outcome.artifacts,
            error=outcome.error or "",
        )

    if pid == "advance":
        from olano.cognition.ideation import run_advance_items

        outcome, count, _ = await run_advance_items(config, workspace_root, budget_check=False)
        return _RunnerResult(
            outcome.status,
            count,
            outcome.summary or "Reviewed active items.",
            "",
            outcome.artifacts,
            error=outcome.error or "",
        )

    if pid == "skills":
        count = await _learn_skills(config, workspace_root)
        return _RunnerResult(
            status="completed", count=count, summary=f"{count} new skill(s) drafted."
        )

    if pid == "improve":
        # The service owns the proposal-generation + approve-to-apply pulse routing
        # (SOUL.md/AGENTS.md are never edited directly). It posts its own APPROVAL
        # items, so this phase has posts_own_pulse=True.
        from olano.memory.markdown_memory import MarkdownMemory
        from olano.memory.self_learning import SelfLearningService

        svc = SelfLearningService(
            agent_config=config,
            workspace_root=workspace_root,
            markdown_memory=MarkdownMemory(workspace_root),
        )
        summary = await svc._run_self_improve_worker()
        return _RunnerResult(
            status="completed",
            count=0,
            summary=summary,
            artifacts=["memories/improvement-proposals.md"],
        )

    if pid == "checks":
        from olano.cognition.ideation import run_self_check

        outcome, count, _ = await run_self_check(config, workspace_root, budget_check=False)
        return _RunnerResult(
            outcome.status,
            count,
            outcome.summary or "No issues found.",
            "",
            outcome.artifacts,
            error=outcome.error or "",
        )

    if pid == "memory_garden":
        from olano.cognition.memory_gardener import run_memory_gardener
        from olano.memory.markdown_memory import MarkdownMemory

        outcome = await run_memory_gardener(
            config, workspace_root, MarkdownMemory(workspace_root), budget_check=False
        )
        return _RunnerResult(
            outcome.status,
            0,
            outcome.summary or "Memory gardened.",
            "",
            outcome.artifacts,
            error=outcome.error or "",
        )

    if pid == "workspace_optimize":
        from olano.cognition.workspace_optimizer import run_workspace_optimizer

        outcome, backup = await run_workspace_optimizer(config, workspace_root, budget_check=False)
        return _RunnerResult(
            outcome.status,
            0,
            outcome.summary or "Workspace optimized.",
            backup,
            outcome.artifacts,
            error=outcome.error or "",
        )

    if pid == "skill_librarian":
        from olano.cognition.skill_librarian import run_skill_librarian

        outcome, backup = await run_skill_librarian(config, workspace_root, budget_check=False)
        return _RunnerResult(
            outcome.status,
            0,
            outcome.summary or "Skill library organized.",
            backup,
            outcome.artifacts,
            error=outcome.error or "",
        )

    return _RunnerResult(status="error", summary=f"Unknown phase '{pid}'.")


async def _learn_skills(config: AgentConfig, workspace_root: Path) -> int:
    """Run the agentic skill-extraction pass over recent sessions."""
    from olano.memory.markdown_memory import MarkdownMemory
    from olano.memory.self_learning import SelfLearningService

    svc = SelfLearningService(
        agent_config=config,
        workspace_root=workspace_root,
        markdown_memory=MarkdownMemory(workspace_root),
    )
    return await svc.learn_skills_now()


# ──────────────────────────── public: run a phase ───────────────────────────


async def run_phase(
    phase_id: str,
    config: AgentConfig,
    workspace_root: Path,
    *,
    source: str = "autonomous",
    force: bool = False,
) -> dict[str, Any]:
    """Run one cognition phase end-to-end: gate → deepagent → audit → surface.

    ``source`` is recorded in the activity log ("autonomous" | "manual"). ``force``
    (manual "run now") bypasses the daily deep-run cap but still respects budget.

    Returns a result dict: ``{id, label, status, count, summary, backup_job, artifacts}``.
    """
    phase = PHASE_BY_ID.get(phase_id)
    if phase is None:
        return {"id": phase_id, "status": "error", "summary": f"Unknown phase '{phase_id}'."}

    def _result(status: str, **kw: Any) -> dict[str, Any]:
        return {
            "id": phase.id,
            "label": phase.label,
            "status": status,
            "count": 0,
            "summary": "",
            "backup_job": "",
            "artifacts": [],
            "code": "",
            "remediation": None,
            **kw,
        }

    # 1. Feature gate (per-agent + global). force still respects feature off.
    if not feature_enabled(phase.feature, config):
        return _result("disabled", summary=f"{phase.label} is disabled.")

    # 1b. Autonomy gate — an autonomous (scheduled) run is skipped the moment the
    # master autonomy switch is flipped off, even if its cron job is still
    # registered (so the toggle takes effect without a restart). Manual runs
    # (source != "autonomous") ignore this.
    if source == "autonomous":
        from olano.config.system_settings import autonomy_enabled

        if not autonomy_enabled(config):
            return _result("disabled", summary="Autonomy is off.")

    # 2. Budget + daily-cap gate (deep-only: skip, never cheapen).
    ok, reason = deep_gate(config, workspace_root, force=force)
    if not ok:
        _audit(workspace_root, phase, source=source, status="skipped", summary=reason)
        return _result("skipped", summary=reason)

    # 3. Record the deep run BEFORE invoking (so a crash still counts against the cap).
    from olano.cognition.router import record_deep_run

    record_deep_run(workspace_root)

    # 4. Run the agentic runner (tracked as "running" for the live dashboard,
    # and as a Cortex job so the Runs panel lists it with every step its worker
    # took; inside a Think-now or a manual run it is a step of that job).
    from olano.cognition import jobs as cortex_jobs

    async with cortex_jobs.REGISTRY.track(
        agent=config.name,
        workspace_root=workspace_root,
        kind="phase",
        target=phase.id,
        title=phase.label,
        source=source,
    ) as job_scope:
        try:
            with phase_running(config.name, phase.id):
                rr = await _dispatch(phase, config, workspace_root)
        except Exception as exc:  # one phase failing must never break the engine
            logger.exception("cortex: phase %s failed", phase.id)
            rr = _RunnerResult(status="error", error=str(exc))

        # 4b. On error, classify into a human-readable reason + code + remediation so
        # the UI can explain *why* (locked vault, expired sign-in, bad key, model
        # unavailable, …) and offer a one-click fix instead of a bare "error".
        code = ""
        remediation: dict[str, Any] | None = None
        streak = 0
        if rr.status == "error":
            code, human, remediation = classify_cognition_error_full(rr.error or rr.summary, config)
            rr.summary = human
            # Consecutive failures of THIS phase, persisted per workspace. The OPS
            # alert below rings on a streak, not on a single run: a transient
            # refusal is re-fired by the scheduler and should heal silently.
            streak = record_failure(workspace_root, _phase_streak_label(phase))
        elif rr.status in ("completed", "empty"):
            clear_failures(workspace_root, _phase_streak_label(phase))
        job_scope.complete(
            outcome=rr.status,
            summary=rr.summary or "",
            error=(rr.summary or rr.error or "") if rr.status == "error" else "",
            result={"count": rr.count, "artifacts": rr.artifacts, "backup_job": rr.backup_job},
        )

    # 5. Audit (the dashboard Activity feed reads this).
    _audit(
        workspace_root,
        phase,
        source=source,
        status=rr.status,
        summary=rr.summary,
        count=rr.count,
        artifacts=rr.artifacts,
        backup_job=rr.backup_job,
        code=code,
        remediation=remediation,
    )

    # 6. Surface the outcome (errors are surfaced for every phase; successes skip
    # phases that already post their own pulse).
    _surface(
        config,
        workspace_root,
        phase,
        rr,
        source=source,
        code=code,
        remediation=remediation,
        streak=streak,
    )

    # 6b. Fully-Autonomous: approve anything this phase just queued for a human
    # (learned skills, self-improve proposals, actionable pulse items). No-op when
    # the auto-approve switch is off — then the items wait for a dashboard click.
    auto_approve_cognition_items(config, workspace_root)

    return _result(
        rr.status,
        count=rr.count,
        summary=rr.summary,
        backup_job=rr.backup_job,
        artifacts=rr.artifacts,
        error=rr.error,
        code=code,
        remediation=remediation,
    )


def _audit(
    workspace_root: Path,
    phase: Phase,
    *,
    source: str,
    status: str,
    summary: str = "",
    count: int = 0,
    artifacts: list[str] | None = None,
    backup_job: str = "",
    code: str = "",
    remediation: dict[str, Any] | None = None,
) -> None:
    from olano.cognition.audit import log_action

    log_action(
        workspace_root,
        f"cortex_{phase.id}",
        summary=summary,
        detail={
            "phase": phase.id,
            "label": phase.label,
            "feature": phase.feature,
            "source": source,
            "tier": "deep",
            "status": status,
            "count": count,
            "artifacts": artifacts or [],
            "backup_job": backup_job,
            "code": code,
            "remediation": remediation,
        },
    )


def _surface(
    config: AgentConfig,
    workspace_root: Path,
    phase: Phase,
    rr: _RunnerResult,
    *,
    source: str,
    code: str = "",
    remediation: dict[str, Any] | None = None,
    streak: int = 0,
) -> None:
    """Post the right pulse for a phase result (skip if the runner self-posts).

    Failures are surfaced for EVERY phase — even ones that normally post their
    own pulse — so a failed background run is visible in the OPS lane with a
    one-click fix, rather than vanishing. ``streak`` is the run's consecutive
    failure count: a failure nobody has to act on (a rate limit the scheduler
    is about to retry, a step budget a longer run blew through) stays off the
    inbox until it has failed ``bell_streak_threshold()`` runs in a row — the
    same gate the operator bell applies — so a one-off never surfaces and a
    persistent problem still does. One whose remediation needs a human (an
    expired sign-in, a locked vault, an empty balance) surfaces on the first
    run: waiting would only delay the person who has to act.
    """
    if rr.status == "error":
        if holds_for_streak(rr.status, remediation, streak):
            logger.info(
                "cortex: %s failed (%s, streak=%d/%d) — holding the OPS alert for a repeat",
                phase.id,
                code or "error",
                streak,
                bell_streak_threshold(),
            )
            return
        _surface_error(
            config, workspace_root, phase, rr, source=source, code=code, remediation=remediation
        )
        return
    if phase.posts_own_pulse:
        return
    if rr.status not in ("completed", "empty"):
        return
    try:
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane, PulsePriority, PulseType

        engine = PulseEngine(config, workspace_root / WORKSPACE_STATE_DIRNAME)
        why = (
            "Cortex ran this on its own in the background."
            if source == "autonomous"
            else "You triggered this from Cortex."
        )

        if phase.policy == "guarded":
            # Identity/behaviour changes only ever land as PROPOSALS for approval.
            engine.add(
                content=rr.summary or f"{phase.label} proposals ready for review.",
                source="cognition",
                lane=PulseLane.APPROVAL,
                type=PulseType.PROPOSAL,
                priority=PulsePriority.NORMAL,
                title=f"{phase.label}: proposals to review",
                why_now=why,
                suggested_next_step="Review the proposals; approve to apply.",
                references=rr.artifacts or ["memories/improvement-proposals.md"],
                metadata={"kind": "cortex_proposal", "phase": phase.id, "auto_applied": False},
                dedup_window_hours=12.0,
            )
            return

        if phase.policy == "readonly":
            if rr.count <= 0:
                return  # healthy → nothing to surface
            engine.add(
                content=rr.summary or f"{phase.label} found {rr.count} item(s).",
                source="cognition",
                lane=PulseLane.OPS,
                type=PulseType.FINDING,
                priority=PulsePriority.LOW,
                title=f"{phase.label}: {rr.count} finding(s)",
                why_now=why,
                suggested_next_step="Review the findings.",
                metadata={"kind": "cortex_check", "phase": phase.id},
                dedup_window_hours=12.0,
            )
            return

        # auto: only surface when something actually changed.
        if rr.count <= 0 and not rr.artifacts:
            return
        action = {"type": "restore_backup", "job_id": rr.backup_job} if rr.backup_job else None
        engine.add(
            content=rr.summary or f"{phase.label} updated.",
            source="cognition",
            lane=PulseLane.PULSE,
            type=PulseType.FINDING,
            priority=PulsePriority.LOW,
            title=f"Cortex: {phase.label}",
            why_now=why,
            suggested_next_step="Review the change; use Undo to revert if needed.",
            references=rr.artifacts,
            action=action,
            metadata={"kind": "cortex_auto", "phase": phase.id, "auto_applied": True},
            dedup_window_hours=12.0,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("cortex: surface pulse failed for %s: %s", phase.id, exc)


def _surface_error(
    config: AgentConfig,
    workspace_root: Path,
    phase: Phase,
    rr: _RunnerResult,
    *,
    source: str,
    code: str,
    remediation: dict[str, Any] | None,
) -> None:
    """Surface a failed phase as an OPS alert carrying the fix-it remediation.

    Deduped over a long window so a recurring scheduled failure doesn't spam the
    inbox; the carried ``code``/``remediation`` let the dashboard render the same
    one-click fix (reconnect / set key / unlock vault / top up) as chat."""
    try:
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane, PulsePriority, PulseType

        engine = PulseEngine(config, workspace_root / WORKSPACE_STATE_DIRNAME)
        next_step = (remediation or {}).get("summary") or "Open Cortex to retry once resolved."
        engine.add(
            content=rr.summary or f"{phase.label} could not complete.",
            source="cognition",
            lane=PulseLane.OPS,
            type=PulseType.ALERT,
            priority=PulsePriority.NORMAL,
            title=f"Cortex: {phase.label} needs attention",
            why_now=(
                "A background Cortex run failed."
                if source == "autonomous"
                else "The Cortex run you triggered failed."
            ),
            suggested_next_step=next_step,
            metadata={
                "kind": "cortex_error",
                "phase": phase.id,
                "code": code,
                "remediation": remediation,
            },
            # One alert per phase+failure-class per 6h — enough to notice, not spam.
            dedup_window_hours=6.0,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("cortex: surface error pulse failed for %s: %s", phase.id, exc)


# ─────────────────────────── fully-autonomous approval ──────────────────────

# Cognition pulse sources/kinds whose pending items "Fully Autonomous" mode
# approves automatically (skills you'd otherwise "Keep", proposals you'd "Apply").
# Subject to ``cognition.approval_required`` (see ``approval_guard``): an item
# whose approval would write SOUL.md / AGENTS.md / IDENTITY.md is held for a
# human by default even here, so "Fully Autonomous" never rewrites who the agent
# is unless the owner removes ``identity_edit`` from that list.
_AUTO_APPROVE_SOURCES: frozenset[str] = frozenset({"cognition", "self_learning"})
_AUTO_APPROVE_KINDS: frozenset[str] = frozenset(
    {"learned_skill", "self_improvement", "cortex_proposal"}
)
# Executable PULSE-lane actions we auto-run (NOT restore_backup — that's an undo,
# and NOT a bare informational finding with no action).
_AUTO_APPLY_ACTION_TYPES: frozenset[str] = frozenset(
    {"run_worker", "append_lines", "append_text", "composite"}
)


def _eligible_for_auto_approve(
    item: Any, lane: Any, required: frozenset[str] = frozenset()
) -> bool:
    """Whether the Fully Autonomous sweep may approve ``item`` on ``lane``.

    ``required`` is the agent's resolved ``cognition.approval_required``; an item
    that falls into any of those categories is never eligible, whatever its
    kind — that list is the documented "always needs a human" contract.
    """
    from olano.cognition.approval_guard import held_by, item_categories
    from olano.pulse.models import PulseLane

    if required and held_by(item_categories(item), required):
        return False
    src = str(getattr(item, "source", "") or "")
    kind = str((getattr(item, "metadata", None) or {}).get("kind") or "")
    if lane == PulseLane.APPROVAL:
        # ONLY the known-safe kinds — never silently consume an arbitrary proposal
        # from a cognition source (e.g. a `memory_reorganization` suggestion, which
        # has no executable action and is meant for a human to read & act on).
        return kind in _AUTO_APPROVE_KINDS
    # PULSE lane: a cognition-sourced item carrying an executable action — never
    # auto-dismiss a purely informational finding or re-run an already-applied
    # undo card (restore_backup).
    if src not in _AUTO_APPROVE_SOURCES:
        return False
    action_type = str((getattr(item, "action", None) or {}).get("type") or "")
    return action_type in _AUTO_APPLY_ACTION_TYPES


def auto_approve_cognition_items(config: AgentConfig, workspace_root: Path) -> int:
    """ "Fully Autonomous": approve cognition outputs that would otherwise wait for a
    human click — APPROVAL-lane learned skills + self-improve proposals, and
    PULSE-lane items carrying an executable action. Informational findings and
    already-applied undo cards are left alone. No-op (returns 0) when the
    auto-approve switch is off. Safe to call repeatedly (a sweep)."""
    from olano.config.system_settings import auto_approve_enabled

    if not auto_approve_enabled(config):
        return 0
    try:
        from olano.cognition.approval_guard import held_by, item_categories
        from olano.config.system_settings import approval_required_categories
        from olano.pulse.engine import PulseEngine
        from olano.pulse.models import PulseLane

        required = approval_required_categories(config)
        engine = PulseEngine(config, workspace_root / WORKSPACE_STATE_DIRNAME)
        approved = 0
        held = 0
        for lane in (PulseLane.APPROVAL, PulseLane.PULSE):
            for item in engine.list_pending(lane=lane, max_items=200):
                if not _eligible_for_auto_approve(item, lane):
                    continue
                # Eligible by kind — now the owner's hard stop. Counted so the
                # log says how many the sweep *would* have applied.
                if required and held_by(item_categories(item), required):
                    held += 1
                    continue
                # Mark BEFORE approve() so the flag is persisted by its _save().
                item.metadata["auto_approved"] = True
                ok, _msg = engine.approve(item.id)
                if ok:
                    approved += 1
        if approved:
            logger.info("cortex: auto-approved %d pending item(s) for %s", approved, config.name)
        if held:
            logger.info(
                "cortex: held %d pending item(s) for a human under approval_required=%s (%s)",
                held,
                sorted(required),
                config.name,
            )
        return approved
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("cortex: auto-approve sweep failed: %s", exc)
        return 0


# ─────────────────────────── public: run the funnel ─────────────────────────


async def run_cycle(
    config: AgentConfig,
    workspace_root: Path,
    *,
    phase_ids: list[str] | None = None,
    source: str = "manual",
    force: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Run the Think funnel, yielding live progress events.

    Shared by the dashboard "Think" button (``force=True``) and any caller that
    wants the full diverge→converge→build→learn→reflect sequence. Event shape:
      {"event": "phase_start", id, label, running, index, total}
      {"event": "phase_done", id, label, count, summary, backup_job, status}
      {"event": "all_done", total_artifacts}
    """
    ids = [pid for pid in (phase_ids or THINK_CYCLE_ORDER) if pid in PHASE_BY_ID]
    total = len(ids)
    total_artifacts = 0

    for index, pid in enumerate(ids):
        phase = PHASE_BY_ID[pid]
        yield {
            "event": "phase_start",
            "id": pid,
            "label": phase.label,
            "running": phase.running,
            "index": index,
            "total": total,
        }
        # Each phase is fully isolated: a failure (even an unexpected raise) is
        # turned into an "error" phase_done event and the funnel CONTINUES to the
        # next phase. One broken phase never blocks the rest.
        try:
            result = await run_phase(pid, config, workspace_root, source=source, force=force)
        except Exception as exc:  # pragma: no cover - run_phase already guards
            logger.exception("cortex: run_cycle phase %s raised", pid)
            code, human, remediation = classify_cognition_error_full(str(exc), config)
            result = {
                "status": "error",
                "summary": human,
                "code": code,
                "count": 0,
                "remediation": remediation,
            }
        total_artifacts += int(result.get("count", 0) or 0)
        yield {
            "event": "phase_done",
            "id": pid,
            "label": phase.label,
            "count": result.get("count", 0),
            "summary": result.get("summary", ""),
            "backup_job": result.get("backup_job", ""),
            "status": result.get("status", "completed"),
            "code": result.get("code", ""),
            "remediation": result.get("remediation"),
        }

    yield {"event": "all_done", "total_artifacts": total_artifacts}
