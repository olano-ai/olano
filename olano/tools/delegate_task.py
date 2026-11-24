"""
delegate_task — spawn a focused subagent using a tier=subagent skill.

The skill's SKILL.md body becomes the child agent's system prompt. The child
inherits the parent's model + workspace but runs in an isolated LangGraph
thread with a trimmed toolset (the skill's ``blocked_tools`` on top of the
default deny-list).

Modes:
  • Single: ``skill`` + ``goal`` + optional ``context``.
  • Batch:  ``tasks=[{skill, goal, context?}, …]`` — bounded concurrency
    (``DEFAULT_MAX_CONCURRENT`` in-flight, extras queue via semaphore).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

# ``_extract_summary`` / ``_read_skill_body`` live in the cognition worker now
# (one source of truth); re-exported here for back-compat with existing imports.
from olano.cognition.worker import _extract_summary, _read_skill_body, run_worker
from olano.security.delegate_guards import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_TIMEOUT_SECONDS,
    at_limit,
    current_depth,
    merge_blocked_tools,
)

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace

# Batch concurrency ceiling. Matches Hermes' default so the surface area
# stays familiar; bumpable per-factory via ``make_delegate_task_tool(max_concurrent=...)``.
DEFAULT_MAX_CONCURRENT = 3

logger = logging.getLogger(__name__)

__all__ = [
    "_build_child_config",
    "_build_child_system_prompt",
    "_extract_summary",
    "_read_skill_body",
    "make_delegate_task_tool",
]


def _build_child_system_prompt(skill_body: str, goal: str, context: str | None) -> str:
    parts = [
        "You are a focused subagent invoked by a parent agent to complete a "
        "specific delegated task. You have no memory of the parent's "
        "conversation — rely only on the task, context, and skill instructions "
        "below.",
        "",
        f"TASK:\n{goal.strip()}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context.strip()}")
    if skill_body:
        parts.append(f"\nSKILL INSTRUCTIONS:\n{skill_body}")
    parts.append(
        "\nProduce a concise, self-contained summary of what you did, what you "
        "found, and any artifacts you created. Only the summary is returned to "
        "the parent — intermediate tool calls stay isolated."
    )
    return "\n".join(parts)


def _build_child_config(
    parent_config: AgentConfig,
    skill_meta: dict[str, Any],
    child_system_prompt: str,
) -> AgentConfig:
    """Clone parent config for a child run: unique name, stripped toolset, no skills."""
    blocked = merge_blocked_tools(skill_meta.get("blocked_tools"))
    child_tools = [t for t in parent_config.tools if t not in blocked]
    child_name = f"{parent_config.name}__sub__{skill_meta['name']}__{uuid.uuid4().hex[:6]}"
    return parent_config.model_copy(
        update={
            "name": child_name,
            "credential_scope": parent_config.credential_scope or parent_config.name,
            "system_prompt": child_system_prompt,
            "tools": child_tools,
            "skills": [],
            "subagents": [],
            "builtin_subagents": [],
            "channels": [],
            "schedule": [],
        }
    )


async def _run_single(
    *,
    parent_config: AgentConfig,
    registry: dict[str, dict[str, Any]],
    skill: str,
    goal: str,
    context: str | None,
    task_index: int = 0,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Run one subagent invocation and return a structured result dict.

    Thin wrapper over :func:`olano.cognition.worker.run_worker`: it keeps
    delegate_task's skill-registry validation and full blocklist (the worker
    itself applies only a minimal deny-list), then maps the ``WorkerOutcome``
    back to delegate_task's historical result shape.
    """
    goal = (goal or "").strip()
    if not goal:
        return {
            "task_index": task_index,
            "skill": skill,
            "status": "error",
            "error": "goal is required",
        }

    skill_meta = registry.get(skill)
    if skill_meta is None:
        return {
            "task_index": task_index,
            "skill": skill,
            "status": "error",
            "error": f"Unknown subagent skill: {skill!r}",
            "available_skills": sorted(registry.keys()),
        }

    timeout = int(skill_meta.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    max_iter = int(skill_meta.get("max_iterations") or DEFAULT_MAX_ITERATIONS)
    skill_body = _read_skill_body(skill_meta["location"])

    # Preserve delegate_task's stricter security posture by passing its FULL
    # blocklist (default deny-list + skill-declared) as extra_blocked_tools.
    blocked = list(merge_blocked_tools(skill_meta.get("blocked_tools")))
    ws_root = workspace_root or (
        Path(parent_config.workspace) if parent_config.workspace else Path()
    )

    logger.info(
        "delegate_task[%d] start skill=%s depth=%d goal=%r",
        task_index,
        skill,
        current_depth(),
        goal[:80],
    )

    outcome = await run_worker(
        parent_config=parent_config,
        workspace_root=ws_root,
        playbook=skill_body,
        goal=goal,
        context=context,
        tools_allow=None,
        extra_blocked_tools=blocked,
        max_iterations=max_iter,
        timeout=timeout,
        budget_check=False,  # delegate_task historically does not budget-gate
        worker_label=f"sub__{skill}",
        # This failure is already returned into a live turn the calling agent is
        # mid-way through — the user sees it in the reply. A bell row per failed
        # subagent would be noise competing with the answer it belongs to.
        notify_operator=False,
    )

    result: dict[str, Any] = {
        "task_index": task_index,
        "skill": skill,
        "status": outcome.status,
        "duration_seconds": outcome.duration_seconds,
    }
    if outcome.status in ("completed", "empty"):
        result["summary"] = outcome.summary
    if outcome.error is not None:
        result["error"] = outcome.error
    return result


class _DelegateTaskInput(BaseModel):
    skill: str | None = Field(
        default=None,
        description=(
            "Single-task mode: name of the subagent skill to invoke. Omit when using `tasks`."
        ),
    )
    goal: str | None = Field(
        default=None,
        description=(
            "Single-task mode: what the subagent should accomplish. Omit when using `tasks`."
        ),
    )
    context: str | None = Field(
        default=None,
        description=(
            "Single-task mode: optional background info — file paths, errors, "
            "constraints. The subagent cannot see your conversation."
        ),
    )
    tasks: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Batch mode: list of tasks to run in parallel. Each item is "
            '`{"skill": str, "goal": str, "context"?: str}`. Up to '
            f"{DEFAULT_MAX_CONCURRENT} tasks run concurrently; extras queue."
        ),
    )


def make_delegate_task_tool(
    *,
    parent_config: AgentConfig,
    workspace: Workspace,
    subagent_registry: dict[str, dict[str, Any]],
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> StructuredTool | None:
    """Factory: return a ``delegate_task`` LangChain tool bound to this agent.

    Supports two modes:
      • Single: ``skill`` + ``goal`` (+ optional ``context``)
      • Batch: ``tasks=[{skill, goal, context?}, …]`` — runs with bounded
        concurrency (``max_concurrent`` in-flight at once).

    Returns ``None`` when the registry is empty so the caller can skip
    registration entirely (the tool would have nothing to offer).
    """
    if not subagent_registry:
        return None

    registry_snapshot = dict(subagent_registry)
    semaphore_limit = max(1, int(max_concurrent))

    async def _run(
        skill: str | None = None,
        goal: str | None = None,
        context: str | None = None,
        tasks: list[dict[str, Any]] | None = None,
    ) -> str:
        if at_limit():
            return json.dumps(
                {
                    "status": "rejected",
                    "error": (
                        f"Delegation depth limit reached (depth={current_depth()}). "
                        "Subagents cannot spawn further subagents."
                    ),
                }
            )

        # Normalize single-task into the batch pipeline
        if tasks and isinstance(tasks, list) and len(tasks) > 0:
            normalized = [
                {
                    "skill": str(t.get("skill", "")).strip(),
                    "goal": str(t.get("goal", "")).strip(),
                    "context": t.get("context"),
                }
                for t in tasks
            ]
        elif skill and goal:
            normalized = [
                {"skill": str(skill).strip(), "goal": str(goal).strip(), "context": context}
            ]
        else:
            return json.dumps(
                {
                    "status": "error",
                    "error": "Provide either `skill`+`goal` (single) or `tasks` (batch).",
                }
            )

        sem = asyncio.Semaphore(semaphore_limit)
        overall_start = time.monotonic()

        async def _bounded(idx: int, task: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                return await _run_single(
                    parent_config=parent_config,
                    registry=registry_snapshot,
                    skill=task["skill"],
                    goal=task["goal"],
                    context=task.get("context"),
                    task_index=idx,
                )

        results = await asyncio.gather(
            *(_bounded(i, t) for i, t in enumerate(normalized)),
            return_exceptions=False,
        )

        total_duration = round(time.monotonic() - overall_start, 2)

        if len(results) == 1:
            # Single-task shape preserved for back-compat with callers that
            # expect a flat summary rather than a results array.
            single = dict(results[0])
            single.pop("task_index", None)
            return json.dumps(single, ensure_ascii=False)

        return json.dumps(
            {
                "results": results,
                "total_duration_seconds": total_duration,
                "max_concurrent": semaphore_limit,
            },
            ensure_ascii=False,
        )

    available = ", ".join(sorted(registry_snapshot.keys()))
    description = (
        "Spawn one or more focused subagents to handle delegated tasks. Each "
        "subagent runs in an isolated context with a restricted toolset — "
        "only the final summary returns to you. Use this when a task would "
        "flood your context with intermediate data, or when a dedicated skill "
        "exists for it.\n\n"
        "TWO MODES (one is required):\n"
        "  1. Single: provide `skill` + `goal` (+ optional `context`).\n"
        f"  2. Batch: provide `tasks` array (up to {semaphore_limit} run in "
        "parallel). Each item is `{skill, goal, context?}`.\n\n"
        f"Available subagent skills: {available}.\n\n"
        "Pass all needed info via `context` — subagents cannot see your "
        "conversation history."
    )

    return StructuredTool.from_function(
        coroutine=_run,
        name="delegate_task",
        description=description,
        args_schema=_DelegateTaskInput,
    )
