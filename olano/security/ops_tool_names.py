"""The operations tool names: scheduler, heartbeat, PULSE, approvals, ops queue.

A leaf on purpose. ``security.delegate_guards`` folds the mutating subset into
its block list and ``config.agent_config`` imports ``delegate_guards`` at module
level, so these names cannot live under ``olano.config``: importing them from
there runs ``olano/config/__init__`` (→ ``agent_config`` → ``delegate_guards``,
half-initialised) whenever ``delegate_guards`` or ``skills.loader`` is a
process's first olano import. ``config.ops_defaults`` re-exports them, so that
remains the public spelling for everything that is not a security leaf.
"""

from __future__ import annotations

# Every agent gets these at the top level (ops_defaults splices them into
# UNIVERSAL_AGENT_TOOL_NAMES). They used to live only on a built-in OpsManager
# subagent reached through the `task` tool, which cost a delegation hop for
# "remind me at 9" and hid the tools from the agent's own Tools tab. Like every
# universal default, each one is switched off per agent with `disabled_tools`
# (the Tools tab writes it), and all of them go with
# `default_internal_tools: false`.
OPS_TOOL_NAMES: list[str] = [
    # Scheduler
    "scheduler_create",
    "scheduler_list",
    "scheduler_get",
    "scheduler_update",
    "scheduler_delete",
    "scheduler_history",
    "scheduler_search",
    "scheduler_run_now",
    # Heartbeat
    "heartbeat_list",
    "heartbeat_add",
    "heartbeat_update",
    "heartbeat_remove",
    "heartbeat_install_from_skill",
    "heartbeat_status",
    "heartbeat_configure",
    # Pulse / proactive intelligence
    "pulse_list",
    "pulse_generate",
    "pulse_accept",
    "pulse_defer",
    "pulse_reject",
    "pulse_undo",
    "pulse_clear_expired",
    # Approvals workflow
    "approvals_list",
    "approvals_approve",
    "approvals_reject",
    "approvals_defer",
    "approvals_clear_expired",
    # Ops queue
    "ops_list",
    "ops_reject",
    "ops_defer",
    "ops_clear_expired",
]

# The ops tools that only read. Everything else in OPS_TOOL_NAMES changes state
# (books or edits a job, fires one now, approves an item and runs its action,
# spawns a PULSE generation run), and background workers / delegate_task
# children never get those: they inherit the parent's tool list, and before the
# ops tools were universal they never carried them.
OPS_READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "scheduler_list",
        "scheduler_get",
        "scheduler_history",
        "scheduler_search",
        "heartbeat_list",
        "heartbeat_status",
        "pulse_list",
        "approvals_list",
        "ops_list",
    }
)
OPS_MUTATING_TOOL_NAMES: frozenset[str] = frozenset(
    n for n in OPS_TOOL_NAMES if n not in OPS_READ_ONLY_TOOL_NAMES
)
