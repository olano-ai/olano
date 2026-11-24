"""Per-job reasoning effort, validated once for every writer.

A scheduled job (a saved scheduler task or an ``agent.json5 → schedule``
entry) may name the model it runs on and the reasoning effort it runs at,
the same two per-turn overrides the dashboard chat's model and effort chips
set. Both are optional: unset means the agent's own configured model and its
agent-level ``reasoning_effort``.

The model half is ``admin.scheduler.validate_task_model`` (is the provider
connected here). This module owns the effort half: every writer (the
scheduler tools, AgentFather's fleet tools, the dashboard's scheduler and
agent schedule forms through the API) validates through
:func:`validate_task_effort` so a job can never be saved with an effort the
model it will run on does not accept. The run path is deliberately lenient
the other way round: ``build_model`` drops any effort a model cannot take, so
a value that becomes stale later (a package downgrade, a tier retired) costs
a log line, never the job.

Provider differences are not this module's problem. Olano sends one
standardized ``reasoning_effort`` value and the model builder maps it, or
drops it, per provider (``models/reasoning.py``); the ladder a model accepts
comes from the installed package's model profile or the synced catalog, which
is the one source the dashboard pickers read too.
"""

from __future__ import annotations

from typing import Any


def normalize_effort(value: Any) -> str:
    """Lower-cased effort level, or ``""`` for unset (whitespace counts as unset)."""
    return str(value or "").strip().lower()


def validate_task_effort(reasoning_effort: Any, *, runs_on: str | None) -> str:
    """Normalize a job's effort and check it against the model it will run on.

    ``runs_on`` is the job's own model when it has one, else the owning
    agent's configured model: the one the effort has to be valid for. Returns
    ``""`` for "the agent's own setting" (None, empty, blanks), else the
    lower-cased level. Raises ``ValueError`` with a message written for the
    person or agent saving the job.
    """
    effort = normalize_effort(reasoning_effort)
    if not effort:
        return ""
    target = str(runs_on or "").strip()
    if not target:
        raise ValueError(
            "reasoning_effort needs a model to apply to: set `model` on the job "
            "or give the agent a configured model"
        )
    from olano.models.reasoning import reasoning_effort_info

    info = reasoning_effort_info(target)
    if info is None:
        raise ValueError(
            f"'{target}' has no reasoning-effort control, so reasoning_effort "
            "cannot be set for this job. Leave it empty, or pick a model that "
            "advertises effort levels."
        )
    levels = [str(v) for v in info["levels"]]
    if effort not in levels:
        raise ValueError(
            f"'{target}' does not accept reasoning_effort '{effort}'. "
            f"It accepts: {', '.join(levels)}."
        )
    return effort
