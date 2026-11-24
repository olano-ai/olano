"""
Scheduler management tools for the agent.

Allows agents to create, list, update, delete, search, and run scheduled tasks.
Tasks are persisted in SQLite under the active Olano home.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool

from olano.scheduler.db import SchedulerDB
from olano.security.tool_metadata import tool_metadata


def _default_agent_name() -> str:
    from olano.config.home import load_home_config

    return load_home_config().default_agent


try:
    from olano.memory.embeddings import embed_text
except ImportError:
    embed_text = None  # type: ignore[assignment]

_db: SchedulerDB | None = None


def _get_db() -> SchedulerDB:
    global _db
    if _db is None:
        _db = SchedulerDB()
    return _db


def _next_run_text(task: dict) -> str:
    """When the task fires next, read off the live scheduler job.

    The ``tasks.next_run`` column is never written (the dashboard route reads
    the live job for the same reason), so rendering it told every agent that
    every task was "unscheduled". Falls back to the task's own trigger when no
    runtime scheduler is in this process.
    """
    if not task.get("enabled", True):
        return "none (task is disabled)"
    from olano.scheduler import clock

    tz_name = task.get("timezone") or "UTC"
    try:
        from olano.scheduler.cron import OlanoScheduler, get_active_scheduler

        live = get_active_scheduler()
        if live is not None:
            shown = clock.format_next_run(live.next_run_for_task(str(task["id"])), tz_name)
            if shown:
                return shown
            return "not registered with the running scheduler (save the task again to register it)"
        from datetime import datetime, timezone

        trigger, _problem = OlanoScheduler._db_task_trigger(task)
        if trigger is not None:
            nxt = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            shown = clock.format_next_run(nxt, tz_name)
            if shown:
                return shown
            return "none (its run time has passed)"
    except Exception:
        pass
    return "unknown"


def _weekday_line(schedule: str | None) -> str:
    from olano.scheduler import clock

    try:
        return clock.weekday_note(schedule)
    except Exception:
        return ""


def _format_task(task: dict, history: list[dict] | None = None) -> str:
    """Render one task in full — the prompt is **never** truncated here.

    Every other read path in this module shows a name and at most a preview
    (``scheduler_list`` shows no prompt at all, ``scheduler_search`` 100 chars),
    which made editing a task a blind overwrite of text nobody could read. This
    renderer is what ``scheduler_get`` exists to hand back.
    """
    parts = [
        f"ID: {task['id']}",
        f"Name: {task['name']}",
        f"Schedule: {task.get('schedule') or task.get('run_at') or 'one-time'}",
        *([_weekday_line(task.get("schedule"))] if _weekday_line(task.get("schedule")) else []),
        f"Timezone: {task.get('timezone') or 'UTC'}",
        f"Agent: {task.get('agent_name') or _default_agent_name()}",
        f"Created by: {task.get('created_by') or 'unknown'}",
        f"Visibility: {task.get('visibility', 'private')}",
        f"Model: {task.get('model') or 'agent default'}",
        f"Reasoning effort: {task.get('reasoning_effort') or 'agent default'}",
        f"Enabled: {task.get('enabled', True)}",
        f"Last run: {task.get('last_run') or 'never'}",
        f"Next run: {_next_run_text(task)}",
        f"Run count: {task.get('run_count', 0)}",
        "",
        "Prompt:",
        str(task.get("prompt") or ""),
    ]
    if history:
        parts += ["", f"Recent runs ({len(history)}):"]
        for h in history:
            status = "✓" if not h.get("error") else "✗"
            detail = (h.get("error") or h.get("result") or "")[:300]
            parts.append(f"{status} {h['ran_at']}" + (f" — {detail}" if detail else ""))
    return "\n".join(parts)


@tool
def scheduler_create(
    name: Annotated[str, "Short name for the task"],
    prompt: Annotated[str, "The message/prompt to send to the agent when the task runs"],
    schedule: Annotated[
        str,
        "Standard 5-field cron for recurring tasks: minute hour day-of-month month "
        "day-of-week (e.g., '0 9 * * *' = 9am daily). Day-of-week is standard "
        "cron: 0 or 7 = Sunday, 1 = Monday, so '1-5' is Monday to Friday; names "
        "(mon-fri) work too. Leave empty for one-time tasks.",
    ] = "",
    run_at: Annotated[
        str,
        "ISO datetime for one-time tasks (e.g., '2026-04-01T09:00:00'). "
        "Leave empty for recurring cron tasks.",
    ] = "",
    timezone: Annotated[
        str,
        "IANA timezone name (e.g., 'America/New_York', 'Asia/Singapore', 'UTC'). Call get_current_time() first if unsure.",
    ] = "UTC",
    agent_name: Annotated[
        str,
        "Agent to run the task. Leave empty to use yourself (the calling agent). "
        "Only set this if you want a DIFFERENT agent to run the task.",
    ] = "",
    visibility: Annotated[
        str, "Who can see this task: 'private' (only you) or 'shared' (all agents)"
    ] = "private",
    model: Annotated[
        str,
        "Model this task runs on, as provider:model (e.g. 'olano:turbo', "
        "'anthropic:claude-sonnet-4-6'). Leave empty to use the running agent's "
        "configured model. Pick a cheaper tier for a routine digest, a bigger one "
        "for deep analysis; the run bills the model that ran. Refused if the "
        "model's provider is not connected here.",
    ] = "",
    reasoning_effort: Annotated[
        str,
        "Reasoning effort for this task's runs (e.g. 'low', 'medium', 'high'), one "
        "of the levels the model it runs on accepts (`model` when set, else the "
        "agent's own). Leave empty to use the agent's own setting. Refused with the "
        "accepted levels if the model does not take it.",
    ] = "",
) -> str:
    """
    Create a new scheduled task. Supports recurring cron schedules and one-time runs.
    Examples:
    - Daily at 9am: schedule='0 9 * * *'
    - Every Monday: schedule='0 10 * * 1'
    - Weekdays: schedule='0 9 * * 1-5'
    - One-time: run_at='2026-04-01T09:00:00'
    """
    if not name.strip():
        return "Error: task name cannot be empty."
    if not prompt.strip():
        return "Error: task prompt cannot be empty."
    if not schedule and not run_at:
        return "Error: provide either a cron schedule or a run_at datetime."
    if visibility not in ("private", "shared"):
        visibility = "private"
    from olano.tools.talk_to import current_agent

    caller = current_agent.get() or None
    try:
        from olano.admin.scheduler import validate_task_effort, validate_task_model

        task_model = validate_task_model(model)
        task_effort = validate_task_effort(
            reasoning_effort, model=task_model, agent_name=agent_name.strip() or caller
        )
    except ValueError as exc:
        return f"Error: {exc}"

    # Validate timezone before touching the DB so we don't create orphan tasks.
    tz_name = timezone.strip() or "UTC"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz_name)
    except Exception:
        return (
            f"Error: unknown timezone '{tz_name}'. "
            "Use IANA names like 'UTC', 'America/New_York', 'Asia/Singapore', 'Europe/London'. "
            "Tip: call get_current_time() first to see your local timezone."
        )

    created_by = caller

    # Embed the prompt for semantic search
    embedding = None
    if embed_text is not None:
        try:
            embedding = embed_text(prompt)
        except Exception:
            pass  # Embeddings are optional

    db = _get_db()
    task_id = db.create_task(
        name=name.strip(),
        prompt=prompt.strip(),
        schedule=schedule.strip() or None,
        run_at=run_at.strip() or None,
        timezone=tz_name,
        agent_name=agent_name.strip() or caller or None,
        embedding=embedding,
        visibility=visibility,
        created_by=created_by,
        model=task_model or None,
        reasoning_effort=task_effort or None,
    )
    runtime_note = ""
    try:
        from olano.scheduler.cron import get_active_scheduler

        scheduler = get_active_scheduler()
        if scheduler is not None:
            ok, message = scheduler.sync_db_task(task_id)
            runtime_note = f"\nRuntime: {message}"
    except Exception:
        runtime_note = ""

    try:
        from olano.runtime.api import _push_event

        _push_event(
            "scheduler_update", {"action": "created", "id": task_id, "created_by": created_by}
        )
    except Exception:
        pass

    task_type = f"cron: {schedule}" if schedule else f"one-time at {run_at}"
    days = _weekday_line(schedule)
    if days:
        task_type += f"\n{days}"
    return (
        f"Task created!\nID: {task_id}\nName: {name}\nType: {task_type}\nTimezone: {tz_name}"
        f"\nVisibility: {visibility}\nModel: {task_model or 'agent default'}"
        f"\nReasoning effort: {task_effort or 'agent default'}{runtime_note}"
    )


@tool
def scheduler_list(
    filter_text: Annotated[str, "Optional text filter to search task names/prompts"] = "",
) -> str:
    """
    List scheduled tasks visible to you: your own tasks plus tasks others have shared.
    Optionally filter by text in the task name or prompt.
    """
    from olano.tools.talk_to import current_agent

    caller = current_agent.get() or None
    db = _get_db()
    tasks = db.list_tasks(caller=caller)

    if not tasks:
        return "No scheduled tasks found."

    if filter_text:
        fl = filter_text.lower()
        tasks = [t for t in tasks if fl in t["name"].lower() or fl in t["prompt"].lower()]
        if not tasks:
            return f"No tasks matching '{filter_text}'."

    lines = [f"Scheduled Tasks ({len(tasks)} total):\n"]
    for t in tasks:
        status = "✓" if t.get("enabled") else "✗"
        sched = t.get("schedule") or t.get("run_at") or "?"
        vis = "🔒" if t.get("visibility", "private") == "private" else "🌐"
        owner = t.get("created_by") or "unknown"
        lines.append(
            f"{status} {vis} [{t['id'][:8]}] {t['name']}\n"
            f"   Schedule: {sched}  |  Agent: {t.get('agent_name') or _default_agent_name()}"
            f"  |  Created by: {owner}  |  Runs: {t.get('run_count', 0)}"
            f"\n   Next run: {_next_run_text(t)}"
        )
        days = _weekday_line(t.get("schedule"))
        if days:
            lines.append(f"   {days}")

    return "\n".join(lines)


@tool
def scheduler_get(
    task_id: Annotated[str, "Task ID (full or first 8 chars)"],
    history: Annotated[int, "Recent run records to include (0-20)"] = 3,
) -> str:
    """
    Read one scheduled task in full, including its COMPLETE instructions.

    scheduler_list shows no prompt at all and scheduler_search only the first
    100 characters, so this is the only way to see the exact instructions a task
    will send. Call it before scheduler_update: an empty argument there means
    "keep", so an edit replaces the whole prompt whether or not you have read it.

    Scoped like scheduler_list — your own tasks plus ones others have shared.
    """
    from olano.tools.talk_to import current_agent

    caller = current_agent.get() or None
    db = _get_db()

    full_id = _resolve_task_id(db, task_id, caller=caller)
    if not full_id:
        return f"Error: no task found with ID starting with '{task_id}'."
    task = db.get_task(full_id)
    if task is None:
        return f"Error: task {task_id} not found."

    limit = max(0, min(20, history))
    records = db.get_history(full_id, limit=limit) if limit else []
    return _format_task(task, records)


@tool
def scheduler_update(
    task_id: Annotated[str, "Task ID (full or first 8 chars)"],
    name: Annotated[str, "New name. Empty keeps the current value"] = "",
    prompt: Annotated[
        str,
        "New prompt. REPLACES the whole existing prompt — read it with "
        "scheduler_get first. Empty keeps the current value",
    ] = "",
    schedule: Annotated[
        str,
        "New standard 5-field cron schedule (day-of-week 0 or 7 = Sunday, 1-5 = "
        "Monday to Friday). Empty keeps the current value",
    ] = "",
    enabled: Annotated[str, "Enable or disable the task: 'true' or 'false'"] = "",
    timezone: Annotated[str, "New timezone. Empty keeps the current value"] = "",
    visibility: Annotated[
        str, "Change visibility: 'private' or 'shared'. Empty keeps the current value"
    ] = "",
    model: Annotated[
        str,
        "New model as provider:model (e.g. 'olano:pro'). Empty keeps the current "
        "value; 'default' returns the task to the agent's configured model",
    ] = "",
    reasoning_effort: Annotated[
        str,
        "New reasoning effort, one of the levels the task's model accepts (the "
        "task's own model, else the agent's). Empty keeps the current value; "
        "'default' returns the task to the agent's own setting",
    ] = "",
) -> str:
    """
    Update an existing scheduled task's name, prompt, schedule, enabled status,
    visibility, model or reasoning effort. Only the agent that created the task
    can update it.

    An empty argument means *keep*, so this tool can never clear a field (the
    exceptions are ``model='default'`` and ``reasoning_effort='default'``, which
    return the task to the agent's own model / effort) — and a supplied prompt
    replaces the existing one in full. Call
    scheduler_get first: scheduler_list shows no prompt and scheduler_search
    only a 100-character preview, so without it you would be overwriting text
    you have not read.
    """
    from olano.tools.talk_to import current_agent

    caller = current_agent.get() or None
    db = _get_db()

    full_id = _resolve_task_id(db, task_id, caller=caller)
    if not full_id:
        return f"Error: no task found with ID starting with '{task_id}'."

    task = db.get_task(full_id)
    if task is None:
        return f"Error: task {task_id} not found."
    task_owner = task.get("created_by")
    if caller and task_owner and caller != task_owner:
        return f"Error: only the agent that created this task ('{task_owner}') can update it."

    updates: dict = {}
    if name.strip():
        updates["name"] = name.strip()
    if prompt.strip():
        updates["prompt"] = prompt.strip()
    if schedule.strip():
        updates["schedule"] = schedule.strip()
    if timezone.strip():
        updates["timezone"] = timezone.strip()
    if enabled.lower() in ("true", "false"):
        updates["enabled"] = 1 if enabled.lower() == "true" else 0
    if visibility.strip() in ("private", "shared"):
        updates["visibility"] = visibility.strip()
    if model.strip() or reasoning_effort.strip():
        # Validated as the pair will be after the edit: a new model against the
        # stored effort, a new effort against the stored model.
        try:
            from olano.admin.scheduler import validate_task_effort, validate_task_model

            if model.strip().lower() == "default":
                next_model = ""
            elif model.strip():
                next_model = validate_task_model(model)
            else:
                next_model = str(task.get("model") or "").strip()
            if reasoning_effort.strip().lower() == "default":
                next_effort = ""
            else:
                next_effort = validate_task_effort(
                    reasoning_effort.strip() or task.get("reasoning_effort"),
                    model=next_model,
                    agent_name=task.get("agent_name") or caller,
                )
        except ValueError as exc:
            return f"Error: {exc}"
        if model.strip():
            updates["model"] = next_model or None
        if reasoning_effort.strip():
            updates["reasoning_effort"] = next_effort or None

    if not updates:
        return (
            "No fields to update. Provide at least one of: name, prompt, schedule, "
            "enabled, timezone, visibility, model, reasoning_effort."
        )

    ok = db.update_task(full_id, **updates)
    if ok:
        runtime_note = ""
        try:
            from olano.scheduler.cron import get_active_scheduler

            scheduler = get_active_scheduler()
            if scheduler is not None:
                _, message = scheduler.sync_db_task(full_id)
                runtime_note = f"\nRuntime: {message}"
        except Exception:
            runtime_note = ""
        days = _weekday_line(updates.get("schedule"))
        days_note = f"\n{days}" if days else ""
        return f"Task {full_id[:8]}... updated: {list(updates.keys())}{days_note}{runtime_note}"
    return f"Error: failed to update task {task_id}."


@tool
def scheduler_delete(
    task_id: Annotated[str, "Task ID (full or first 8 chars) to permanently delete"],
) -> str:
    """Delete a scheduled task permanently. Only the agent that created the task can delete it."""
    from olano.tools.talk_to import current_agent

    caller = current_agent.get() or None
    db = _get_db()
    full_id = _resolve_task_id(db, task_id, caller=caller)
    if not full_id:
        return f"Error: no task found with ID starting with '{task_id}'."

    task = db.get_task(full_id)
    if task is None:
        return f"Error: task {task_id} not found."
    task_owner = task.get("created_by")
    if caller and task_owner and caller != task_owner:
        return f"Error: only the agent that created this task ('{task_owner}') can delete it."

    name = task["name"]
    try:
        from olano.scheduler.cron import get_active_scheduler

        scheduler = get_active_scheduler()
        if scheduler is not None:
            scheduler.remove_db_task(full_id)
    except Exception:
        pass

    ok = db.delete_task(full_id)
    if ok:
        return f"Task '{name}' ({full_id[:8]}...) deleted."
    return f"Error: failed to delete task {task_id}."


@tool
def scheduler_history(
    task_id: Annotated[str, "Task ID (full or first 8 chars)"],
    limit: Annotated[int, "Maximum number of history entries to return (1-50)"] = 10,
) -> str:
    """View execution history for a scheduled task.

    Scoped like scheduler_list — your own tasks plus ones others have shared.
    """
    from olano.tools.talk_to import current_agent

    db = _get_db()
    full_id = _resolve_task_id(db, task_id, caller=current_agent.get() or None)
    if not full_id:
        return f"Error: no task found with ID starting with '{task_id}'."

    limit = max(1, min(50, limit))
    history = db.get_history(full_id, limit=limit)

    if not history:
        return f"No execution history for task {task_id}."

    lines = [f"Execution history for task {task_id[:8]}... ({len(history)} entries):\n"]
    for h in history:
        status = "✓" if not h.get("error") else "✗"
        result_preview = (h.get("result") or "")[:100]
        error_preview = (h.get("error") or "")[:100]
        lines.append(
            f"{status} {h['ran_at']}\n"
            + (f"   Result: {result_preview}" if result_preview else "")
            + (f"   Error: {error_preview}" if error_preview else "")
        )

    return "\n".join(lines)


@tool
def scheduler_search(
    query: Annotated[str, "Natural language query to find semantically similar tasks"],
    k: Annotated[int, "Number of results to return"] = 5,
) -> str:
    """
    Search scheduled tasks using semantic similarity.
    Finds tasks whose prompts are conceptually similar to your query.

    Scoped like scheduler_list — your own tasks plus ones others have shared.
    Results carry a 100-character prompt preview; use scheduler_get for the full
    instructions.
    """
    from olano.tools.talk_to import current_agent

    if embed_text is None:
        return "Error: sentence-transformers not installed. Run: uv add sentence-transformers"
    try:
        query_vec = embed_text(query)
    except Exception as e:
        return f"Error computing embedding: {e}"

    db = _get_db()
    results = db.semantic_search(query_vec, k=k, caller=current_agent.get() or None)

    if not results:
        return "No matching tasks found (tasks need embeddings — try creating new tasks)."

    lines = [f"Tasks similar to '{query}':\n"]
    for r in results:
        score = r.get("similarity_score", 0)
        prompt = str(r.get("prompt") or "")
        preview = prompt[:100] + ("…" if len(prompt) > 100 else "")
        lines.append(f"[{r['id'][:8]}] {r['name']} (score: {score:.3f})\n   {preview}")

    return "\n".join(lines)


# Fires a full model turn now, outside the schedule the owner set. Owner-only
# and approved, as olano_scheduler_run_now is (hitl_policy "cross_agent_exec").
@tool_metadata(capability="mutating", risk="medium", requires_hitl=True, owner_only=True)
@tool
def scheduler_run_now(
    task_id: Annotated[str, "Task ID (full or first 8 chars) to execute immediately"],
) -> str:
    """
    Execute a scheduled task immediately regardless of its schedule.

    Requires the runtime scheduler to be active. Scoped like scheduler_list —
    your own tasks plus ones others have shared.
    """
    from olano.tools.talk_to import current_agent

    db = _get_db()
    full_id = _resolve_task_id(db, task_id, caller=current_agent.get() or None)
    if not full_id:
        return f"Error: no task found with ID starting with '{task_id}'."

    task = db.get_task(full_id)
    if not task:
        return f"Error: task {task_id} not found."

    try:
        from olano.scheduler.cron import get_active_scheduler

        scheduler = get_active_scheduler()
        if scheduler is not None:
            ok, message = scheduler.run_db_task_now(full_id)
            if ok:
                return (
                    f"Task '{task['name']}' ({full_id[:8]}...) triggered for immediate execution.\n"
                    f"Prompt: {task['prompt'][:200]}\n"
                    f"Runtime: {message}"
                )
            return f"Error: {message}"
    except Exception as e:
        return f"Error triggering task {full_id[:8]}...: {e}"

    db.add_history(full_id, result="Run requested while runtime inactive")
    return (
        f"Task '{task['name']}' ({full_id[:8]}...) queued for later execution.\n"
        f"Prompt: {task['prompt'][:200]}\n"
        "Note: the runtime scheduler is not active, so this request was recorded but not executed."
    )


# ----------------------------------------------------------------- helpers


def _resolve_task_id(db: SchedulerDB, partial_id: str, caller: str | None = None) -> str | None:
    """Resolve a full task ID from a partial ID (prefix match).

    ``caller`` scopes the match to that agent's own tasks plus shared ones. An
    unscoped prefix match sweeps every agent's private tasks and returns the
    first hit, which is how a guessed 8-char prefix reached another agent's job.
    ``caller=None`` keeps the unscoped sweep for callers with no agent identity
    bound (the CLI, tests, ephemeral workers) — and for the fleet tools, which
    deliberately resolve through ``olano.admin.scheduler.resolve_task_ref``
    instead of this function.
    """
    tasks = db.list_tasks(caller=caller)
    for t in tasks:
        if t["id"] == partial_id or t["id"].startswith(partial_id):
            return str(t["id"])
    return None


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
