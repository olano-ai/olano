"""Guardrails for the ``delegate_task`` subagent tool.

Keeps the depth, blocked-tools, and policy knobs centralized so the tool
implementation and tests share one source of truth.
"""

from __future__ import annotations

from contextvars import ContextVar

from olano.security.ops_tool_names import OPS_MUTATING_TOOL_NAMES

# Parent (0) → child (1) → grandchild rejected (2).
MAX_DEPTH = 2

# Tracks nested delegation depth across async tasks. ContextVar survives
# asyncio task boundaries; each child agent inherits+increments this.
DEPTH_VAR: ContextVar[int] = ContextVar("lionclaw_delegate_depth", default=0)

# Tools children must never have — prevents recursive explosion, shared-memory
# writes, user-facing side effects, and unsandboxed shell access.
DEFAULT_BLOCKED_TOOLS: frozenset[str] = frozenset(
    {
        "delegate_task",
        "memory_write",
        "memory_append",
        "send_message",
        "safe_shell",
        "shell",
        # Spawn deep cognition workers — children must not fan out more deep runs.
        "cortex_promote",
        "cortex_explore",
        # State-changing ops tools (book, edit or fire a job, approve an item
        # and run its action, rewrite heartbeat config, ...): universal
        # defaults, so a child would otherwise inherit them.
        *OPS_MUTATING_TOOL_NAMES,
    }
)

# Defaults when a skill's frontmatter omits them, and the FLOOR of the two-tier
# budget every background worker (cognition phases, self-learning, pulse
# actions, delegate_task) resolves against — see
# `system_settings.effective_worker_budget`.
#
# Max-iterations becomes a langgraph `recursion_limit`, counted in SUPER-STEPS
# (graph node executions), not tool calls: with the olano middleware stack one
# model turn costs ~4 steps (before_model → model → after_model → tools), so
# 100 ≈ 25 rounds of thinking + tool use. The history is a straight line of
# this number being too small: 12 allowed ~3 rounds (and was nested where
# langgraph ignored it, so every worker really ran at the stock 25); 40 allowed
# ~10 and still lost whole research runs — `research_note` died on it in
# production, discarding its work.
#
# THE TWO MOVE TOGETHER, ALWAYS. The timeout is not an independent preference;
# it is the wall a step budget runs into. 600s was calibrated for 40 steps
# (~60s/turn), so shipping 100 steps behind it would have made the raise inert —
# runs would simply time out where they used to exhaust steps, which is the same
# lost work under a different status. 1200s ≈ 48s/turn at 25 rounds. Change one
# of these and you must re-derive the other.
DEFAULT_MAX_ITERATIONS = 100
DEFAULT_TIMEOUT_SECONDS = 1200

# Bounds for the operator-settable budget (dashboard Cortex → Step budget,
# `cognition.worker_max_iterations` / `worker_timeout_seconds`). They exist so a
# typo can't disable background work outright (a 1-step budget fails every run
# before the first tool call) or let one run bill unbounded. The ceilings are
# generous on purpose — a research playbook that legitimately needs 30 model
# turns should be reachable without editing code — and the resolver clamps
# rather than raises, so a bad hand-edit degrades to the nearest legal value
# instead of breaking every worker on the box.
MIN_MAX_ITERATIONS = 10
MAX_MAX_ITERATIONS = 400
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 7200

# How many background reasoning runs may talk to a provider at once, box-wide
# (`cognition.worker_max_concurrency`). Every agent's Cortex phases, the
# self-learning distiller and Pulse actions share the cap. Two: enough for a
# slow phase and a quick one to overlap, small enough that a fleet whose phases
# all fire in the same minute stays under a typical per-account concurrency
# limit — before this cap existed, that burst is exactly what 429'd the first
# model call of every run. Clamped rather than raised, like the budget above.
DEFAULT_WORKER_CONCURRENCY = 2
MIN_WORKER_CONCURRENCY = 1
MAX_WORKER_CONCURRENCY = 16


def clamp_max_iterations(value: int) -> int:
    """Clamp a step budget into ``[MIN_MAX_ITERATIONS, MAX_MAX_ITERATIONS]``."""
    return max(MIN_MAX_ITERATIONS, min(MAX_MAX_ITERATIONS, int(value)))


def clamp_timeout_seconds(value: float) -> int:
    """Clamp a wall-clock budget into ``[MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS]``."""
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, int(value)))


def clamp_worker_concurrency(value: int) -> int:
    """Pull an operator-set concurrency cap into ``[1, 16]``."""
    return max(MIN_WORKER_CONCURRENCY, min(MAX_WORKER_CONCURRENCY, int(value)))


def current_depth() -> int:
    """Return the current delegation depth in the running task."""
    return DEPTH_VAR.get()


def at_limit() -> bool:
    """True when further delegation would exceed ``MAX_DEPTH``."""
    return current_depth() >= MAX_DEPTH


def merge_blocked_tools(skill_blocked: list[str] | None) -> frozenset[str]:
    """Merge skill-declared blocked tools with the default deny-list."""
    merged: set[str] = set(DEFAULT_BLOCKED_TOOLS)
    if skill_blocked:
        merged.update(str(t).strip() for t in skill_blocked if str(t).strip())
    return frozenset(merged)
