"""Reusable background reasoning worker — a headless deepagent that runs a
playbook to produce an OUTCOME (a summary + the artifacts it touched).

This generalizes the ``delegate_task._run_single`` pattern so it can be driven
by background services (self-learning, memory gardening), Pulse action
execution, and scheduled cognition jobs — all on the same guard rails:

- **budget gate** (``cost_tracker.budget_gate``) — no spend when over limit
- **depth guard** (``DEPTH_VAR``/``at_limit``) — a worker can't recurse forever
- **recursion limit** + **timeout** — bounded steps and wall-clock per run

Workers run as ephemeral child configs cloned from the parent agent. They are
NEVER persisted as agents (no ``~/.olano/agents/`` entry, never in
``list_agents()`` / team / dashboard) — the user does nothing to "add" them.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olano.cognition.reliability import (
    background_slot,
    bell_streak_threshold,
    clear_failures,
    failure_since,
    holds_for_streak,
    record_failure,
)
from olano.config.ops_defaults import OPS_MUTATING_TOOL_NAMES
from olano.config.workspace import WORKSPACE_STATE_DIRNAME
from olano.pulse.cost_tracker import budget_gate
from olano.security.delegate_guards import (
    DEPTH_VAR,
    at_limit,
    current_depth,
)

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

logger = logging.getLogger(__name__)

# Tools a background worker must never have — prevents recursion, self-triggering
# more background work, and user-facing side effects. Applied on top of any
# caller-supplied ``extra_blocked_tools``. Deliberately MINIMAL: unlike
# ``delegate_task``'s blocklist, this does NOT deny ``safe_shell`` / file writes,
# which the prototyper and memory gardener legitimately need.
_WORKER_HARD_DENY: frozenset[str] = frozenset(
    {
        "delegate_task",
        "send_message",
        # Each of these spawns another deep worker — a background worker must
        # never fan out more deep runs.
        "cortex_promote",
        "cortex_explore",
        # The state-changing ops tools (book, edit or fire a job, approve and
        # run a held action, rewrite heartbeat config, ...). Universal defaults
        # now, so a worker would otherwise inherit them.
        *OPS_MUTATING_TOOL_NAMES,
    }
)


@dataclass
class WorkerOutcome:
    """Structured result of a single :func:`run_worker` invocation."""

    status: str  # completed|empty|timeout|error|rejected|budget_blocked|needs_setup
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)  # workspace-relative paths
    duration_seconds: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "duration_seconds": self.duration_seconds,
        }
        if self.summary:
            d["summary"] = self.summary
        if self.artifacts:
            d["artifacts"] = self.artifacts
        if self.error is not None:
            d["error"] = self.error
        return d


def _read_skill_body(skill_md_path: str) -> str:
    """Return SKILL.md content with the YAML frontmatter stripped."""
    try:
        raw = Path(skill_md_path).read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read skill file %s: %s", skill_md_path, exc)
        return ""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return raw.strip()
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end == -1:
        return raw.strip()
    return "\n".join(lines[end + 1 :]).strip()


def _extract_summary(result: Any) -> str:
    """Pull the final text from a LangGraph ``ainvoke`` / ``invoke`` result."""
    messages = None
    if isinstance(result, dict):
        messages = result.get("messages")
    if not messages:
        return ""
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            joined = "\n".join(t for t in text_parts if t).strip()
            if joined:
                return joined
    return ""


def _parse_artifacts(summary: str) -> list[str]:
    """Extract a trailing ``ARTIFACTS: a, b`` line into a list of paths."""
    if not summary:
        return []
    for line in reversed(summary.splitlines()):
        stripped = line.strip()
        if stripped.upper().startswith("ARTIFACTS:"):
            payload = stripped.split(":", 1)[1]
            return [p.strip() for p in payload.split(",") if p.strip()]
    return []


def _strip_artifacts_line(summary: str) -> str:
    """Remove the machine ``ARTIFACTS:`` line(s) from a human-facing summary."""
    if not summary:
        return ""
    kept = [ln for ln in summary.splitlines() if not ln.strip().upper().startswith("ARTIFACTS:")]
    return "\n".join(kept).strip()


def _classify_model_auth_error(exc: Exception, model: str | None) -> Any | None:
    """Return an :class:`ActionableError` when *exc* is a model-credential problem.

    Used to convert a provider auth/credential failure that surfaces at invoke
    time (missing/invalid/expired key) into a graceful ``needs_setup`` outcome
    rather than a raw traceback. Returns ``None`` for any other error so real
    bugs still log a full traceback. Never raises.
    """
    try:
        from olano.core.actionable_errors import (
            CODE_AUTH_EXPIRED,
            CODE_INVALID_KEY,
            classify_exception,
        )
        from olano.models import parse_model_string

        provider, _ = parse_model_string(model or "")
        actionable = classify_exception(exc, provider or None)
        if actionable is not None and actionable.code in (CODE_INVALID_KEY, CODE_AUTH_EXPIRED):
            return actionable
    except Exception:
        return None
    return None


def _classify_step_budget_error(exc: Exception) -> str | None:
    """Return the user-facing sentence when *exc* is a step-budget exhaustion.

    ``None`` for anything else, so a real bug still logs a full traceback. The
    matching lives in :mod:`olano.core.actionable_errors` (``CODE_STEP_BUDGET``,
    matched on the message because the cognition path re-wraps error strings) —
    asking it here rather than sniffing for ``GraphRecursionError`` keeps one
    definition of "ran out of steps" across chat, Cortex and the bell. Never
    raises.
    """
    try:
        from olano.core.actionable_errors import CODE_STEP_BUDGET, classify_exception

        actionable = classify_exception(exc, None)
        if actionable is not None and actionable.code == CODE_STEP_BUDGET:
            return actionable.message
    except Exception:
        return None
    return None


def _build_worker_system_prompt(playbook: str, goal: str, context: str | None) -> str:
    parts = [
        "You are a focused background worker invoked to complete a specific task "
        "autonomously. You have no memory of any prior conversation — rely only "
        "on the task, context, and instructions below. Use your tools to read "
        "files, research, and WRITE real artifacts into the workspace.",
        "",
        f"TASK:\n{goal.strip()}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context.strip()}")
    if playbook and playbook.strip():
        parts.append(f"\nINSTRUCTIONS:\n{playbook.strip()}")
    parts.append(
        "\nWhen done, produce a concise summary of what you did and what you "
        "found. If you created or modified any files, end your final message "
        "with a single line listing them exactly as: 'ARTIFACTS: path1, path2' "
        "(workspace-relative paths). Only your final summary is returned."
    )
    return "\n".join(parts)


def _shell_backend_override(parent_config: AgentConfig) -> Any | None:
    """Promote a plain (non-shell) filesystem backend to a shell-capable
    ``LocalShellBackend`` so a worker can RUN scripts to test what it builds.

    Returns the replacement backend config, or ``None`` to keep the parent's
    backend untouched. We only ever promote a **read/write** ``FilesystemBackend``
    (no exec) → ``LocalShellBackend`` rooted at the SAME directory. A read-only
    filesystem backend (``access_mode="read"``) is deliberately left alone — we
    must not silently turn a read-only agent into a write + shell one. A backend
    that already has shell (``LocalShellBackend``) or runs in an isolated sandbox
    (Docker / remote) is likewise left exactly as-is — we never swap a sandbox for
    local execution.
    """
    from olano.config.backend_config import (
        FilesystemBackendConfig,
        LocalShellBackendConfig,
    )

    pb = parent_config.backend
    if isinstance(pb, FilesystemBackendConfig) and pb.access_mode == "read_write":
        return LocalShellBackendConfig(root_dir=pb.root_dir, virtual_mode=pb.virtual_mode)
    return None


def _release_sandbox_registration(child_name: str, worker_label: str) -> None:
    """Drop the worker's sandbox registry entry once its run is over.

    The worker's shell ran in its PARENT's container (``builder.sandbox_owner``:
    the unique child name used to mint a persistent container per run that
    nothing stopped, and one managed box hit 960 running sandboxes on twelve
    agents), so there is nothing to stop here, only the registry and status
    rows filed under the child's name to forget. Never raises: cleanup must
    not mask the outcome.
    """
    try:
        from olano.core.builder import release_agent_sandbox

        release_agent_sandbox(child_name)
    except Exception as exc:
        logger.debug("run_worker[%s] sandbox registry cleanup failed: %s", worker_label, exc)


def _build_worker_config(
    parent_config: AgentConfig,
    *,
    system_prompt: str,
    tools_allow: list[str] | None,
    extra_blocked: list[str] | None,
    model: str | None,
    label: str,
    want_shell: bool = False,
) -> AgentConfig:
    """Clone the parent config into an ephemeral worker config.

    Inherits the parent's backend + workspace (so the worker can read/write
    files) but strips skills/subagents/channels/schedule and applies the worker
    deny-list. With ``tools_allow`` set, the worker uses exactly that allowlist
    (minus deny); otherwise it inherits the parent's tools minus deny.

    ``want_shell`` promotes a plain filesystem backend to a shell-capable one so
    the worker gets the deepagents ``execute`` tool (scoped to the workspace) —
    used by the prototyping / work-advancing runners to actually RUN what they
    scaffold.

    The worker runs under a fresh, unique ``name`` (checkpoints, attribution)
    but resolves CREDENTIALS as its parent: ``credential_scope`` carries the
    parent's name (chain-safe for a child of a child), and every agent-keyed
    layer — vault overrides, connector records, Google tokens, MCP env refs,
    the model build's provider-key override — keys on it. That is what lets a
    worker use the account its parent connected instead of silently falling
    to the common tier.
    """
    blocked: set[str] = set(_WORKER_HARD_DENY)
    if extra_blocked:
        blocked.update(str(t).strip() for t in extra_blocked if str(t).strip())

    source_tools = tools_allow if tools_allow is not None else parent_config.tools
    child_tools = [t for t in source_tools if t not in blocked]

    child_name = f"{parent_config.name}__cog__{label}__{uuid.uuid4().hex[:6]}"
    update: dict[str, Any] = {
        "name": child_name,
        "credential_scope": parent_config.credential_scope or parent_config.name,
        "system_prompt": system_prompt,
        "tools": child_tools,
        "env": dict(parent_config.env or {}),
        "skills": [],
        "subagents": [],
        "builtin_subagents": [],
        "channels": [],
        "schedule": [],
    }
    if model:
        update["model"] = model
    if want_shell:
        override = _shell_backend_override(parent_config)
        if override is not None:
            update["backend"] = override
    return parent_config.model_copy(update=update)


async def run_worker(
    *,
    parent_config: AgentConfig,
    workspace_root: Path,
    playbook: str,
    goal: str,
    context: str | None = None,
    tools_allow: list[str] | None = None,
    extra_blocked_tools: list[str] | None = None,
    model: str | None = None,
    max_iterations: int | None = None,
    timeout: float | None = None,
    budget_check: bool = True,
    worker_label: str = "worker",
    want_shell: bool = False,
    notify_operator: bool = True,
) -> WorkerOutcome:
    """Run a headless deepagent playbook and return a :class:`WorkerOutcome`.

    ``want_shell`` gives the worker a shell-capable backend (the deepagents
    ``execute`` tool, scoped to the workspace) when the parent runs a plain
    filesystem backend — so the worker can run scripts to test what it builds.

    ``max_iterations`` / ``timeout`` left ``None`` resolve through
    ``system_settings.effective_worker_budget`` in :func:`_run_worker`, at the
    one seam every caller shares — threading them through each call site is
    exactly how the knob went inert once before (every phase silently fell back
    to the signature default). That resolver applies the two-tier rule: this
    agent's ``cognition.worker_*`` override when it has one, else the home-wide
    ``cognition:`` value. Callers with their own budget (``delegate_task``'s
    skill frontmatter, the skill librarian's size-scaled cap) pass explicit
    values, which always win.

    ``notify_operator`` (default on) raises a dashboard notification when the
    run fails and clears it when it succeeds. This wrapper exists *only* to own
    that single exit: the body has a dozen return sites, and a notification
    added at each is a notification forgotten at the next one. Interactive
    callers pass ``False`` — ``delegate_task`` runs inside a live turn the user
    is already watching, and a bell row per failed subagent is noise, not
    signal.
    """
    outcome = await _run_worker(
        parent_config=parent_config,
        workspace_root=workspace_root,
        playbook=playbook,
        goal=goal,
        context=context,
        tools_allow=tools_allow,
        extra_blocked_tools=extra_blocked_tools,
        model=model,
        max_iterations=max_iterations,
        timeout=timeout,
        budget_check=budget_check,
        worker_label=worker_label,
        want_shell=want_shell,
    )
    # A worker may have written a skill (Skillsmith, the librarian, an
    # execution run) by any route, a shell heredoc included. Align every
    # illegal name its folder can absorb before anything loads it.
    await asyncio.to_thread(_align_worker_skill_names, parent_config, workspace_root)
    from olano.cognition import jobs as cortex_jobs

    cortex_jobs.note(
        f"Worker {worker_label} finished: {outcome.status}"
        + (f" in {outcome.duration_seconds:.0f}s" if outcome.duration_seconds else "")
        + (f". {outcome.error}" if outcome.error else ""),
        type="worker_error"
        if cortex_jobs.status_for_outcome(outcome.status) == "error"
        else "worker",
    )
    if notify_operator:
        _notify_worker_outcome(parent_config, worker_label, outcome, workspace_root=workspace_root)
    return outcome


def _align_worker_skill_names(parent_config: AgentConfig, workspace_root: Path) -> None:
    """Set every illegal skill name in the workspace to its folder. Never raises."""
    try:
        from olano.admin.skills import carry_disabled_skill_renames
        from olano.skills.naming import align_skill_names

        rewritten = align_skill_names([Path(workspace_root) / "skills"])
        if rewritten:
            carry_disabled_skill_renames(
                {r["from"]: r["to"] for r in rewritten}, [parent_config.name]
            )
    except Exception:
        logger.debug("Skill name sweep after a worker run failed", exc_info=True)


def _notify_worker_outcome(
    parent_config: AgentConfig,
    worker_label: str,
    outcome: WorkerOutcome,
    *,
    workspace_root: Path | None = None,
) -> None:
    """Put a failed background run on the operator's bell; clear it on success.

    Background work has no human watching it. Before this, a Cortex phase or a
    pulse action that failed left an Activity-drawer row and a Pulse OPS item —
    both of which you only see if you already suspected something was wrong.
    The reported symptom was exactly that: *"I wouldn't have known unless I come
    to check the activity button."*

    Which outcomes ring:

    * ``needs_setup`` → ``error``. A missing/invalid key stays broken until a
      human acts, so it is the loudest.
    * ``error`` / ``timeout`` → ``warn``. Includes the step-budget exhaustion
      this was built for.
    * ``budget_blocked`` / ``rejected`` → nothing. Both are the guard rails
      doing their job, and both clear on their own (daily reset, unwinding
      delegation depth). Ringing for them would train the operator to ignore
      the bell.
    * ``completed`` / ``empty`` → resolve. The bell must reflect the world, not
      the history — the same discipline ``_clear_missing_channel_credentials``
      follows for connectors.

    One row per (agent, worker) via ``dedupe_key``, so a phase failing every six
    hours is one row with a rising ``count``, not forty. Never raises: a
    notification that broke the run it is reporting on would be worse than no
    notification.

    **A streak, not a run.** ``error``/``timeout`` ring only once the worker has
    failed ``bell_streak_threshold()`` runs in a row (persisted per workspace in
    ``reliability.record_failure``): a rate limit the scheduler is about to
    retry used to ring on the first attempt, and with the row deduped and
    re-surfaced on every recurrence it read as *"failing very often"* when the
    world was one transient refusal a night. ``needs_setup`` still rings on the
    first run — a missing key never heals on its own. Guard-rail outcomes leave
    the streak alone; only a real run can end one.
    """
    agent = parent_config.name
    key = f"cognition_worker_failed:{agent}:{worker_label}"
    try:
        from olano.runtime import notifications

        if outcome.status in ("completed", "empty"):
            if workspace_root is not None:
                clear_failures(workspace_root, worker_label)
            notifications.resolve(key)
            return
        if outcome.status not in ("error", "timeout", "needs_setup"):
            return

        streak = 1
        since: str | None = None
        if workspace_root is not None:
            streak = record_failure(workspace_root, worker_label)
            since = failure_since(workspace_root, worker_label)

        # Classify so the body carries the actionable sentence the chat surface
        # would show — never a raw traceback or LangGraph's own advice to set a
        # config key no user can reach — and so the hold below can tell a
        # failure the operator must fix from one a repeat may not repeat.
        body = outcome.error or "The run did not complete."
        remediation: dict[str, Any] | None = None
        try:
            from olano.cognition.orchestrator import classify_cognition_error_full

            _code, human, remediation = classify_cognition_error_full(body, parent_config)
            fix = (remediation or {}).get("summary") if remediation else None
            body = f"{human} {fix}".strip() if fix else human
        except Exception:  # pragma: no cover - classification is a nicety
            logger.debug("Could not classify a worker failure for the bell", exc_info=True)
        if holds_for_streak(outcome.status, remediation, streak):
            logger.info(
                "run_worker[%s] failed (streak=%d/%d) — holding the bell for a repeat",
                worker_label,
                streak,
                bell_streak_threshold(),
            )
            return
        if streak > 1:
            when = f" since {since[:10]}" if since else ""
            body = f"{body} Failed {streak} runs in a row{when}."

        notifications.notify(
            kind="cognition_worker_failed",
            level="error" if outcome.status == "needs_setup" else "warn",
            title=f"Background run '{worker_label}' {_STATUS_TITLE.get(outcome.status, 'failed')}",
            body=body,
            agent=agent,
            dedupe_key=key,
            action={"label": "Open Cortex", "href": f"/cortex?agent={agent}"},
        )
    except Exception:  # pragma: no cover - must never affect the worker's result
        logger.debug("Could not raise the background-run notification", exc_info=True)


#: Verb for the notification title, per failing status.
_STATUS_TITLE = {
    "error": "failed",
    "timeout": "ran out of time",
    "needs_setup": "needs setup",
}


async def _run_worker(
    *,
    parent_config: AgentConfig,
    workspace_root: Path,
    playbook: str,
    goal: str,
    context: str | None = None,
    tools_allow: list[str] | None = None,
    extra_blocked_tools: list[str] | None = None,
    model: str | None = None,
    max_iterations: int | None = None,
    timeout: float | None = None,
    budget_check: bool = True,
    worker_label: str = "worker",
    want_shell: bool = False,
) -> WorkerOutcome:
    """The worker body. See :func:`run_worker` for the contract."""
    goal = (goal or "").strip()
    if not goal:
        return WorkerOutcome(status="error", error="goal is required")

    if max_iterations is None or timeout is None:
        from olano.config.system_settings import effective_worker_budget

        resolved_steps, resolved_timeout = effective_worker_budget(parent_config)
        if max_iterations is None:
            max_iterations = resolved_steps
        if timeout is None:
            timeout = float(resolved_timeout)

    # 1. Budget gate — never spend when over the limit.
    if budget_check:
        ok, msg = budget_gate(
            parent_config.name, workspace_root / WORKSPACE_STATE_DIRNAME, parent_config.budget
        )
        if not ok:
            logger.info("run_worker[%s] budget blocked: %s", worker_label, msg)
            return WorkerOutcome(status="budget_blocked", error=msg or "budget exceeded")

    # 2. Depth guard — a worker can't recurse beyond the delegation limit.
    if at_limit():
        return WorkerOutcome(
            status="rejected",
            error=f"Delegation depth limit reached (depth={current_depth()}).",
        )

    # 3. Model readiness — a fresh deployment may have no provider API key or no
    #    model configured yet. This head-less worker has no human watching, so
    #    fail gracefully with an actionable message instead of crashing deep in
    #    the provider SDK the first time it calls the model. (The child resolves
    #    credentials as its parent via credential_scope, so nothing is pinned.)
    from olano.core.model_readiness import check_model_ready

    readiness = check_model_ready(parent_config, model=model)
    if not readiness.ok:
        message = readiness.error.message if readiness.error else "The model is not configured."
        logger.warning("run_worker[%s] model not ready: %s", worker_label, message)
        return WorkerOutcome(status="needs_setup", error=message)

    system_prompt = _build_worker_system_prompt(playbook, goal, context)
    child_config = _build_worker_config(
        parent_config,
        system_prompt=system_prompt,
        tools_allow=tools_allow,
        extra_blocked=extra_blocked_tools,
        model=model,
        label=worker_label,
        want_shell=want_shell,
    )

    # A background run holds one of the box-wide slots from here on — before the
    # graph is built, so N agents cannot build N clients at once, and before the
    # run's own clock starts, so queueing behind another agent's run costs a
    # later start rather than the run's budget. `delegate_task` subagents
    # (``sub__*``) run inside a live turn the user is waiting on and have their
    # own fan-out cap; an interactive reply must never queue behind Cortex.
    if worker_label.startswith("sub__"):
        return await _execute_worker(
            parent_config,
            child_config,
            goal,
            worker_label,
            max_iterations,
            timeout,
            workspace_root=workspace_root,
        )
    async with background_slot(worker_label):
        return await _execute_worker(
            parent_config,
            child_config,
            goal,
            worker_label,
            max_iterations,
            timeout,
            workspace_root=workspace_root,
        )


async def _execute_worker(
    parent_config: AgentConfig,
    child_config: AgentConfig,
    goal: str,
    worker_label: str,
    max_iterations: int,
    timeout: float,
    *,
    workspace_root: Path | None = None,
) -> WorkerOutcome:
    """Build the worker graph, run it to completion, and account for it.

    Split from :func:`_run_worker` so the concurrency slot wraps exactly the
    part that spends — the gates and config build above it stay unthrottled.
    """
    # Call-time import so callers can patch ``olano.core.builder.build_agent_async``.
    from olano.core.builder import build_agent_async

    async def _release_checkpointer() -> None:
        """Free this ephemeral worker's threads.db connection once it's done.

        ``child_config.name`` is unique per run (``__cog__``/``__sub__`` + uuid),
        so its checkpointer would otherwise stay cached forever — leaking an
        aiosqlite connection (and its OS thread) and adding to the writers that
        contend for the SQLite write lock. The persisted checkpoint rows remain
        in threads.db for analytics; only the live connection is released.
        """
        from olano.core.checkpoints import aclose_agent_checkpointer

        try:
            await aclose_agent_checkpointer(child_config.name)
        except Exception as exc:  # cleanup must never mask the worker outcome
            logger.debug("run_worker[%s] checkpointer cleanup failed: %s", worker_label, exc)

    started = time.monotonic()
    logger.info(
        "run_worker[%s] start model=%s depth=%d goal=%r",
        worker_label,
        child_config.model,
        current_depth(),
        goal[:80],
    )

    try:
        child = await build_agent_async(child_config, include_mcp=False)
    except Exception as exc:
        logger.exception("run_worker[%s] build failed", worker_label)
        # build_agent_async opens the checkpointer before wiring the graph, so a
        # later failure can still leave a cached connection — release it. Same
        # for the sandbox registration: the backend phase precedes most of the
        # phases that can fail.
        await _release_checkpointer()
        _release_sandbox_registration(child_config.name, worker_label)
        return WorkerOutcome(
            status="error",
            error=f"Failed to build worker agent: {exc}",
            duration_seconds=round(time.monotonic() - started, 2),
        )

    invocation = {"messages": [("user", goal)]}

    # Call-time import: olano.core.__init__ pulls builder+runner, and this module
    # is reached from olano/tools/delegate_task.py — importing core at module scope
    # would put the tool package inside that cycle.
    from olano.core.stream_isolation import detached_runnable_config, nostream_config
    from olano.security.egress_guard import exempt as egress_exempt

    # recursion_limit is a TOP-LEVEL RunnableConfig key. Nested inside
    # `configurable` langgraph never reads it (verified: ensure_config leaves
    # the default 25), so workers ran at ~2x the configured iteration cap.
    #
    # nostream: a worker reached through `delegate_task` runs INSIDE the caller's
    # graph, so without this its tokens are pushed into the caller's live stream
    # and rendered as the caller's own reply. A worker's output reaches the
    # caller as this function's WorkerOutcome, never as streamed text. The tag is
    # the marker LangGraph's messages mode honours; `detached_runnable_config`
    # below is the belt that does not depend on it. See
    # olano.core.stream_isolation.
    runtime_config = nostream_config(
        {
            "recursion_limit": max_iterations,
            "configurable": {
                "thread_id": f"cog::{child_config.name}",
            },
        }
    )
    # Inside a Cortex job (a Create project, a scheduled phase, an approved
    # Pulse task) every tool call, result and model message is recorded in the
    # job's log, which is the only record of what a worker did: it writes no
    # transcript. Outside a job (delegate_task in a chat turn) this is empty.
    from olano.cognition import jobs as cortex_jobs

    step_callbacks = cortex_jobs.job_callbacks()
    if step_callbacks:
        runtime_config["callbacks"] = step_callbacks
        cortex_jobs.note(
            f"Worker {worker_label} started on {child_config.model} "
            f"(up to {max_iterations} steps, {int(timeout)}s).",
            type="worker",
        )

    # Opt-in Langfuse tracing: under the caller's turn when there is one
    # (delegate_task), else as its own `cognition:<label>` trace. Added after
    # `nostream_config` and alongside the job callbacks, never in place of
    # them; the Langfuse handler streams nothing.
    from olano.observability.tracing import begin_background_run, end_run

    worker_trace, worker_trace_token, trace_callbacks = begin_background_run(
        parent_config,
        name=f"cognition:{worker_label}",
        session_id=str(runtime_config["configurable"]["thread_id"]),
        model=child_config.model,
    )
    if trace_callbacks:
        runtime_config["callbacks"] = [*(step_callbacks or []), *trace_callbacks]

    token = DEPTH_VAR.set(current_depth() + 1)
    try:
        # Cognition runs on the raw graph (not AgentRunner), so wrap it in a
        # small interrupt-resolve loop tagged with a `cognition` origin: gated
        # tools resolve via the cognition auto-approve switch (on → approve, off
        # → defer to the approvals inbox). Without this, a gated tool would stall
        # the worker with the interrupt pending forever.
        from langgraph.types import Command

        from olano.core.runner import (
            AgentRunner,
            _resolve_autonomous_decisions,
            _resume_payload_for_actions,
        )
        from olano.security.request_context import (
            ORIGIN_KEY,
            make_origin,
            push_request_context,
        )

        cog_origin = make_origin(
            "cognition", False, root_session_id=str(runtime_config["configurable"]["thread_id"])
        )

        async def _invoke_with_hitl() -> Any:
            # Each resume round is a fresh `ainvoke` and so gets a fresh
            # `recursion_limit` — the 8 here bounds interrupt-resolution
            # round-trips (gated tools), NOT total steps, and must not be
            # treated as a step-budget multiplier: rounds only continue while
            # the graph is parked on a pending interrupt, and the wall-clock
            # timeout around this whole loop still caps the run.
            decisions: list[dict] = []
            # The actions `decisions` answers, kept so the resume can be scoped
            # per interrupt id when more than one branch is parked.
            resume_actions: list[dict[str, Any]] = []
            last: Any = None
            from olano.core import thread_locks

            # Held for the whole run, like a chat turn in the runner: the
            # checkpoint sweep's compaction skips a thread whose lock is held
            # rather than appending a checkpoint under a step in flight
            # (core/thread_locks.py).
            async with await thread_locks.acquire(str(runtime_config["configurable"]["thread_id"])):
                for _ in range(8):
                    payload: Any = (
                        Command(resume=_resume_payload_for_actions(resume_actions, decisions))
                        if decisions
                        else invocation
                    )
                    last = await child.ainvoke(payload, config=runtime_config)
                    try:
                        state = await child.aget_state(runtime_config)
                    except Exception:
                        break
                    if not getattr(state, "interrupts", None):
                        break
                    actions = AgentRunner._extract_interrupt_actions(state)
                    if not actions:
                        break
                    decisions = await _resolve_autonomous_decisions(
                        actions, child_config, cog_origin
                    )
                    resume_actions = actions
            return last

        from olano.tools._credentials import agent_runtime_context

        with (
            # Detach from any caller's live stream (delegate_task invokes this
            # from inside the caller's turn) so the worker's tokens never leak
            # into the caller's channel reply. No-op for a top-level caller
            # (scheduler, Cortex router). See the helper's docstring.
            detached_runnable_config(),
            # Same shape for the egress lists: `push_request_context` merges,
            # so a worker started from inside a tool would otherwise run its
            # model calls under the CALLER's lists. Its own tools re-arm
            # enforcement under this agent's policy (egress_guard.governed).
            egress_exempt(),
            # `workspace_root` rides along because the worker's tools resolve
            # their workspace from the request context (cortex_*, pages_*,
            # monitor_*): a top-level worker (the Cortex router, the
            # scheduler) has no caller context, and the tools' bare fallback
            # is the HOME workspace every deployment pins in LIONCLAW_WORKSPACE,
            # not this agent's. A worker started from inside a turn inherits
            # the same value, so the override is a no-op there.
            push_request_context(
                source="cognition",
                channel="cognition",
                metadata={
                    ORIGIN_KEY: cog_origin,
                    **({"workspace_root": str(workspace_root)} if workspace_root else {}),
                },
            ),
            agent_runtime_context(
                child_config.name,
                child_config.env,
                credential_scope=child_config.credential_scope,
            ),
        ):
            result = await asyncio.wait_for(_invoke_with_hitl(), timeout=timeout)
    except TimeoutError:
        duration = round(time.monotonic() - started, 2)
        logger.warning("run_worker[%s] timed out after %ss", worker_label, timeout)
        return WorkerOutcome(
            status="timeout",
            error=f"Worker exceeded {timeout}s timeout",
            duration_seconds=duration,
        )
    except asyncio.CancelledError:
        # Shutdown/cancellation is expected — don't log a full traceback as if
        # the worker crashed; let it propagate.
        raise
    except Exception as exc:
        duration = round(time.monotonic() - started, 2)
        # A credential/auth failure that slipped past the readiness preflight
        # (e.g. a key that is set but invalid/expired) is an operator-config
        # problem, not a worker bug — surface it as an actionable `needs_setup`
        # with a warning, not a scary exception traceback.
        actionable = _classify_model_auth_error(exc, child_config.model)
        if actionable is not None:
            logger.warning("run_worker[%s] model auth failed: %s", worker_label, actionable.message)
            return WorkerOutcome(
                status="needs_setup",
                error=actionable.message,
                duration_seconds=duration,
            )
        # Exhausting the step budget is a BUDGET condition, not a crash. It
        # arrived here as a full GraphRecursionError traceback, which reads as a
        # bug in the worker and buries the one fact that matters (the number to
        # raise). Log the budget, and hand back the classified sentence so the
        # bell and the Think panel never show LangGraph's own advice to "set the
        # `recursion_limit` config key" — a key no user can set.
        budget_error = _classify_step_budget_error(exc)
        if budget_error is not None:
            logger.warning(
                "run_worker[%s] hit its step budget (max_iterations=%s, timeout=%ss) after %ss",
                worker_label,
                max_iterations,
                timeout,
                duration,
            )
            return WorkerOutcome(
                status="error",
                error=budget_error,
                duration_seconds=duration,
            )
        logger.exception("run_worker[%s] failed", worker_label)
        return WorkerOutcome(
            status="error",
            error=str(exc),
            duration_seconds=duration,
        )
    finally:
        DEPTH_VAR.reset(token)
        end_run(worker_trace, worker_trace_token)
        # Release the ephemeral worker's threads.db connection (no-op if already
        # closed); the persisted checkpoint rows stay for analytics.
        await _release_checkpointer()
        _release_sandbox_registration(child_config.name, worker_label)

    duration = round(time.monotonic() - started, 2)
    # Ledger this run's spend under the OWNING agent. Cognition runs on the
    # raw graph, so runner._record_usage never sees it — without this row the
    # single biggest gateway spender (all cortex phases, self-learning,
    # delegate_task) was invisible to Insights while the cloud billed it.
    # The final state's messages are the whole run (fresh thread per run, and
    # the HITL resume loop accumulates onto the same state).
    try:
        from olano.analytics.usage_recorder import record_result_usage

        record_result_usage(
            parent_config.name,
            child_config.model,
            result,
            origin="delegate" if worker_label.startswith("sub__") else "cognition",
            # The cog:: thread this run owns, so the drill-down can rank it from
            # the ledger like any other conversation.
            thread_id=str(runtime_config["configurable"]["thread_id"]),
        )
    except Exception:  # accounting must never mask the worker outcome
        logger.debug("run_worker[%s] usage recording failed", worker_label, exc_info=True)
    raw_summary = _extract_summary(result)
    artifacts = _parse_artifacts(raw_summary)
    summary = _strip_artifacts_line(raw_summary)
    logger.info(
        "run_worker[%s] done status=%s artifacts=%d duration=%ss",
        worker_label,
        "completed" if summary else "empty",
        len(artifacts),
        duration,
    )
    return WorkerOutcome(
        status="completed" if summary else "empty",
        summary=summary,
        artifacts=artifacts,
        duration_seconds=duration,
    )
