"""Agent-facing Cortex item tools — drive ideas / brainstorms / prototypes /
projects directly from chat.

Agents whose Cortex engine is enabled (global cognition master switch on AND the
per-agent ``cognition.enabled`` not False) get these injected automatically (see
``AgentConfig._inject_builtin_ops``), so the user can ask the agent to list,
view, add, edit, approve (promote), explore, discard, or restore Cortex items
instead of clicking through the dashboard Cortex view.

All tools operate on the same structured store the dashboard and the autonomous
runners use (:mod:`olano.cognition.cortex_items` — folders + a JSON index +
auto-generated ``*.md`` mirrors), so changes made here show up everywhere
immediately. Promote / explore run the same deep workers as the dashboard
actions (``olano/cognition/ideation.py``) and log to the same audit trail.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.cognition import cortex_items as ci
from olano.cognition.audit import log_action
from olano.config.workspace import get_workspace
from olano.security.request_context import get_request_context
from olano.tools._sandbox_exec import handoff_to_sandbox, missing_parents

logger = logging.getLogger(__name__)

_KINDS = tuple(ci.KINDS)  # ("idea", "brainstorm", "prototype", "project")

# Cap the markdown returned by cortex_view so one huge detail file can't blow
# up the conversation context.
_VIEW_DETAIL_MAX_CHARS = 8000


def owner_agent_name() -> str:
    """The agent these tools act FOR: the running agent, or, inside a cognition
    worker (``<parent>__cog__<label>__<hex>``) or a delegated sub-agent, the
    parent it acts for. That is the credential scope every per-agent layer keys
    on, and it is what a scheduled step must be filed under: a task owned by
    the worker's throwaway name would never find a runner when it fired."""
    from olano.tools._credentials import credential_agent_name

    return credential_agent_name() or ""


def _agent_workspace_fallback() -> str | None:
    """The bound agent's configured workspace, for a call with no request context.

    A runner-driven turn always carries ``workspace_root`` in the request
    context. A background worker used to push a context without it, and the
    bare ``get_workspace()`` fallback then resolved ``LIONCLAW_WORKSPACE``,
    which every deployment sets to the *home* workspace: a worker's
    ``cortex_add`` landed in the wrong agent's index and its ``cortex_list``
    read an empty one. The agent's own config is the next best answer; only
    with no agent bound at all does the global default apply.
    """
    name = owner_agent_name()
    if not name:
        return None
    try:
        from olano.config.loader import load_agent_config

        return load_agent_config(name).workspace or None
    except Exception:
        return None


def _workspace_root() -> Path:
    request_context = get_request_context()
    workspace_root = request_context.metadata.get("workspace_root") or _agent_workspace_fallback()
    workspace = get_workspace(str(workspace_root) if workspace_root else None)
    workspace.init()
    return workspace.root


def _index_guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn an unreadable index into a tool result instead of a traceback.

    ``cortex_items`` refuses to read a corrupt index as empty (see its module
    docstring); the model needs that refusal as text it can relay, not as an
    exception the loop breaker counts in silence.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ci.CortexIndexError as exc:
                logger.warning("cortex tool %s refused: %s", fn.__name__, exc)
                return f"[tool-error] {exc}"

        return _async_guarded

    @functools.wraps(fn)
    def _guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ci.CortexIndexError as exc:
            logger.warning("cortex tool %s refused: %s", fn.__name__, exc)
            return f"[tool-error] {exc}"

    return _guarded


def _agent_config(workspace_root: Path) -> Any:
    """Best-available AgentConfig for the deep workers (promote / explore).

    Prefers the real config of the agent bound to this turn (its cognition
    model tiers + budget settings apply); falls back to a synthetic config
    against the current workspace, mirroring the pulse tools.
    """
    name = owner_agent_name()
    if name:
        try:
            from olano.config.loader import load_agent_config

            return load_agent_config(name)
        except Exception:
            pass
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    return AgentConfig(
        name=name or "agent",
        workspace=str(workspace_root),
        backend=FilesystemBackendConfig(root_dir=str(workspace_root)),
    )


def _parse_tags(tags: str) -> list[str]:
    return [t.strip() for t in tags.split(",") if t.strip()]


def _format_item(it: dict[str, Any]) -> str:
    summary = (it.get("summary") or "").strip().replace("\n", " ")
    line = f"[{it.get('id')}] [{it.get('kind')}/{it.get('status')}] {it.get('title')}"
    score = ci.rating_score(it)
    if score is not None:
        rec = str((it.get("rating") or {}).get("recommendation") or "")
        line += f" {{score {score:g}/10" + (f", {rec}" if rec else "") + "}"
    if summary:
        line += f" — {summary[:140]}"
    updated = (it.get("updated_at") or "")[:10]
    if updated:
        line += f" (updated {updated})"
    return line


def _kind_error(kind: str) -> str:
    return f"Unknown kind '{kind}'. Valid kinds: {', '.join(_KINDS)}."


@tool
@_index_guard
def cortex_list(
    kind: Annotated[
        str, "Filter by kind: idea | brainstorm | prototype | project. Empty = all."
    ] = "",
    status: Annotated[
        str, "Filter by status: active | exploring | promoted | discarded. Empty = all."
    ] = "",
    include_discarded: Annotated[bool, "Include discarded items in the listing"] = False,
    max_items: Annotated[int, "Maximum number of items to return"] = 30,
) -> str:
    """List Cortex items (ideas, brainstorms, prototypes, projects) with their ids.

    Use this to answer "what ideas/projects do we have?" and to find the item id
    that the other cortex_* tools need."""
    kind = kind.strip().lower()
    status = status.strip().lower()
    if kind and kind not in _KINDS:
        return _kind_error(kind)
    if status and status not in ci.STATUSES:
        return f"Unknown status '{status}'. Valid statuses: {', '.join(ci.STATUSES)}."
    root = _workspace_root()
    items = ci.list_items(
        root,
        kind=kind or None,
        status=status or None,
        include_discarded=include_discarded or status == "discarded",
    )
    counts = ci.counts_by_kind(root)
    counts_line = ", ".join(f"{k}s: {v}" for k, v in counts.items())
    if not items:
        return f"No matching Cortex items. Active counts — {counts_line}."
    lines = [f"Cortex items ({len(items[:max_items])} of {len(items)}; active — {counts_line}):"]
    lines.extend(_format_item(it) for it in items[:max_items])
    return "\n".join(lines)


@tool
@_index_guard
def cortex_view(
    item_id: Annotated[str, "Cortex item id (from cortex_list)"],
) -> str:
    """Show one Cortex item in full: metadata, its detail markdown, and the files
    in its folder (for projects, also PLAN.md checkbox progress)."""
    root = _workspace_root()
    item = ci.get_item(root, item_id.strip())
    if item is None:
        return f"Cortex item {item_id} not found. Use cortex_list to see valid ids."
    detail, files = ci.resolve_detail(root, item)
    lines = [
        f"[{item.get('id')}] {item.get('title')}",
        f"Kind: {item.get('kind')} | Status: {item.get('status')} | Source: {item.get('source')}",
        f"Folder: {item.get('path')}",
    ]
    tags = item.get("tags") or []
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    rating = item.get("rating") if isinstance(item.get("rating"), dict) else None
    if rating:
        dims = rating.get("dimensions") or {}
        dims_text = (
            " ("
            + ", ".join(f"{k} {v:g}" for k, v in dims.items() if isinstance(v, int | float))
            + ")"
            if dims
            else ""
        )
        lines.append(
            f"Rating: {rating.get('score')}/10{dims_text}, recommendation: "
            f"{rating.get('recommendation') or 'keep'} (rated {str(rating.get('rated_at') or '')[:10]})"
        )
        if rating.get("rationale"):
            lines.append(f"Why: {rating['rationale']}")
        if rating.get("next_step"):
            lines.append(f"Next step: {rating['next_step']}")
    links = item.get("links") or {}
    if links.get("promoted_to"):
        to_kind = links.get("promoted_to_kind") or "project"
        lines.append(f"Promoted to {to_kind} folder: {links['promoted_to']}")
    if links.get("from_path"):
        lines.append(f"Promoted from: {links['from_path']}")
    if item.get("kind") == "project":
        plan = root / (item.get("path") or "") / "PLAN.md"
        if plan.is_file():
            try:
                import re as _re

                text = plan.read_text(encoding="utf-8", errors="replace")
                done = len(_re.findall(r"(?mi)^\s*[-*]\s*\[x\]", text))
                todo = len(_re.findall(r"(?mi)^\s*[-*]\s*\[ \]", text))
                if done + todo:
                    lines.append(f"Plan progress: {done}/{done + todo} tasks done")
            except OSError:
                pass
    if detail.strip():
        body = detail.strip()
        if len(body) > _VIEW_DETAIL_MAX_CHARS:
            body = body[:_VIEW_DETAIL_MAX_CHARS] + "\n… (truncated — read the file for the rest)"
        lines.append("\n--- Detail ---\n" + body)
    if files:
        lines.append("\nFiles:\n" + "\n".join(f"- {f}" for f in files[:40]))
    return "\n".join(lines)


@tool
@_index_guard
def cortex_add(
    kind: Annotated[str, "What to create: idea | brainstorm | prototype | project"],
    title: Annotated[str, "Short title for the item"],
    summary: Annotated[str, "One-or-two sentence summary"] = "",
    detail: Annotated[str, "Markdown body written to the item's detail file"] = "",
    tags: Annotated[str, "Optional comma-separated tags"] = "",
) -> str:
    """Capture a new Cortex item (idea/brainstorm/prototype/project) as a tracked
    folder + index entry. If a near-identical active item of the same kind already
    exists it is improved instead of duplicated."""
    kind = kind.strip().lower()
    if kind not in _KINDS:
        return _kind_error(kind)
    if not title.strip():
        return "A non-empty title is required."
    root = _workspace_root()
    item, created = ci.upsert_item(
        root,
        kind=kind,
        title=title.strip(),
        summary=summary.strip(),
        detail=detail.strip(),
        tags=_parse_tags(tags),
        source="agent",
    )
    log_action(
        root,
        "cortex_add",
        summary=f"{'Added' if created else 'Improved existing'} {kind}: {item.get('title')}",
        detail={"item_id": item.get("id"), "kind": kind, "created": created},
    )
    if created:
        return f"Created {kind} '{item.get('title')}' [{item.get('id')}] at {item.get('path')}/."
    return (
        f"A near-identical {kind} already existed — improved it instead: "
        f"'{item.get('title')}' [{item.get('id')}] at {item.get('path')}/."
    )


@tool
@_index_guard
def cortex_edit(
    item_id: Annotated[str, "Cortex item id (from cortex_list)"],
    title: Annotated[str, "New title (empty = keep current)"] = "",
    summary: Annotated[str, "New summary (empty = keep current)"] = "",
    tags: Annotated[str, "New comma-separated tags, replacing the old set (empty = keep)"] = "",
    detail: Annotated[
        str, "New markdown body — REPLACES the item's detail file (empty = keep)"
    ] = "",
) -> str:
    """Edit a Cortex item's title, summary, tags, and/or detail markdown. Only the
    fields you pass are changed; the item keeps its folder and id."""
    root = _workspace_root()
    item = ci.get_item(root, item_id.strip())
    if item is None:
        return f"Cortex item {item_id} not found. Use cortex_list to see valid ids."
    updates: dict[str, Any] = {}
    if title.strip():
        updates["title"] = title.strip()
    if summary.strip():
        updates["summary"] = summary.strip()
    if tags.strip():
        updates["tags"] = _parse_tags(tags)
    changed: list[str] = list(updates)
    if detail.strip():
        detail_file = item.get("detail_file") or ""
        if not detail_file:
            return f"Item {item_id} has no detail file to write to."
        body = detail.strip()
        header = (
            ""
            if body.lstrip().startswith("#")
            else f"# {updates.get('title') or item.get('title')}\n\n"
        )
        dpath = root / detail_file
        created = missing_parents(dpath)
        dpath.parent.mkdir(parents=True, exist_ok=True)
        dpath.write_text(header + body + "\n", encoding="utf-8")
        handoff_to_sandbox(dpath, created=created)
        changed.append("detail")
    if not changed:
        return "Nothing to change — pass at least one of title/summary/tags/detail."
    # A detail-only edit still gets the updated_at bump + mirror regeneration.
    updated = ci.update_item(root, item_id.strip(), **updates)
    log_action(
        root,
        "cortex_edit",
        summary=f"Edited {item.get('kind')}: {(updated or item).get('title')}",
        detail={"item_id": item_id.strip(), "fields": changed},
    )
    return (
        f"Updated {', '.join(changed)} of '{(updated or item).get('title')}' [{item_id.strip()}]."
    )


@tool
@_index_guard
def cortex_discard(
    item_id: Annotated[str, "Cortex item id (from cortex_list)"],
    reason: Annotated[str, "Optional short reason, recorded in the activity log"] = "",
) -> str:
    """Discard a Cortex item (idea/brainstorm/prototype/project). The folder stays
    on disk; the item is marked discarded and can be restored with cortex_restore."""
    root = _workspace_root()
    item = ci.set_status(root, item_id.strip(), "discarded")
    if item is None:
        return f"Cortex item {item_id} not found. Use cortex_list to see valid ids."
    log_action(
        root,
        "cortex_discard",
        summary=f"Discarded {item.get('kind')}: {item.get('title')}",
        detail={"item_id": item_id.strip(), "kind": item.get("kind"), "reason": reason.strip()},
    )
    return f"Discarded {item.get('kind')} '{item.get('title')}' [{item_id.strip()}]."


@tool
@_index_guard
def cortex_restore(
    item_id: Annotated[str, "Cortex item id of a discarded item"],
) -> str:
    """Restore a previously discarded Cortex item back to active."""
    root = _workspace_root()
    item = ci.get_item(root, item_id.strip())
    if item is None:
        return f"Cortex item {item_id} not found. Use cortex_list to see valid ids."
    if item.get("status") != "discarded":
        return (
            f"'{item.get('title')}' [{item_id.strip()}] is not discarded "
            f"(status: {item.get('status')}) — nothing to restore."
        )
    restored = ci.set_status(root, item_id.strip(), "active")
    log_action(
        root,
        "cortex_restore",
        summary=f"Restored {item.get('kind')}: {item.get('title')}",
        detail={"item_id": item_id.strip(), "kind": item.get("kind")},
    )
    return f"Restored {item.get('kind')} '{(restored or item).get('title')}' [{item_id.strip()}] to active."


@tool
@_index_guard
async def cortex_promote(
    item_id: Annotated[str, "Cortex item id to promote (from cortex_list)"],
    target: Annotated[
        str,
        "Stage to promote into: idea | prototype | project. Empty = the NEXT stage of "
        "the funnel (brainstorm → idea → prototype → project). 'project' is allowed "
        "from any stage when the user explicitly approves something as a project.",
    ] = "",
) -> str:
    """Move a Cortex item down the funnel: brainstorm → idea (its strongest angle
    distilled), idea → prototype (a small runnable build, run when safe), prototype
    → project (projects/<slug>/ scaffolded with README/PLAN/STATUS/JOURNAL). A deep
    worker does the work; the source item is marked promoted and linked both ways.
    Use when the user approves an item for the next step, or when a rating says
    it is ready."""
    try:
        root = _workspace_root()
        item = ci.get_item(root, item_id.strip())
        if item is None:
            return f"Cortex item {item_id} not found. Use cortex_list to see valid ids."
        if item.get("kind") == "project":
            return f"'{item.get('title')}' [{item_id.strip()}] is already a project."
        if item.get("status") == "promoted":
            dest = (item.get("links") or {}).get("promoted_to") or "the next stage"
            return f"'{item.get('title')}' [{item_id.strip()}] was already promoted to {dest}."
        from olano.cognition.ideation import run_promote

        config = _agent_config(root)
        outcome, created = await run_promote(
            config, root, item, target=target.strip().lower() or None, source="manual"
        )
        if created is None:
            return (
                f"[tool-error] Promotion of '{item.get('title')}' failed: "
                f"{outcome.error or outcome.summary or outcome.status}"
            )
        log_action(
            root,
            "cortex_promote",
            summary=f"Promoted to {created.get('kind')}: {created.get('title')}",
            detail={
                "item_id": item_id.strip(),
                "from_kind": item.get("kind"),
                "to_kind": created.get("kind"),
                "target_id": created.get("id"),
                "path": created.get("path"),
            },
        )
        extra = (
            " (README/PLAN/STATUS/JOURNAL scaffolded)" if created.get("kind") == "project" else ""
        )
        return (
            f"Promoted '{item.get('title')}' to {created.get('kind')} '{created.get('title')}' "
            f"[{created.get('id')}] at {created.get('path')}/{extra}."
        )
    except Exception as exc:
        return f"[tool-error] cortex_promote: {exc}"


# ───────────────────────────── self-scheduling ──────────────────────────────

#: Minimum spacing a recurring Cortex step may ask for. A background step that
#: fires more often than hourly is a loop, not a workflow.
_MIN_RECURRING_MINUTES = 60
#: Pending Cortex steps one item may hold. A chain of follow-ups on one project
#: is fine; a fan-out is not.
_MAX_STEPS_PER_ITEM = 2


def cortex_task_creator(agent: str) -> str:
    """The ``created_by`` stamp on a task the agent scheduled for itself through
    ``cortex_schedule``. The scheduler reads it back to run those tasks under
    the cognition origin (so ``cognition.approval_required`` still holds) rather
    than the blanket-approving ``scheduled`` origin a human's task gets."""
    return f"cortex:{agent}"


def is_cortex_task(task: dict[str, Any]) -> bool:
    return str(task.get("created_by") or "").startswith("cortex:")


def _cron_too_frequent(expr: str) -> bool:
    """True when a 5-field cron fires more often than hourly (every minute, or
    every N minutes). Coarse on purpose: a schedule this guard cannot read is
    left to the scheduler's own validation."""
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute = parts[0]
    if minute == "*":
        return True
    if minute.startswith("*/"):
        try:
            return int(minute[2:]) < _MIN_RECURRING_MINUTES
        except ValueError:
            return False
    return "," in minute and len(minute.split(",")) > 1 and parts[1] == "*"


def _pending_cortex_tasks(agent: str) -> list[dict[str, Any]]:
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB()
    return [
        t
        for t in db.list_tasks(enabled_only=True, agent_name=agent)
        if t.get("created_by") == cortex_task_creator(agent)
    ]


@tool
@_index_guard
def cortex_schedule(
    item_id: Annotated[str, "Cortex item the step belongs to (a prototype or project id)"],
    instruction: Annotated[
        str,
        "Exactly what the step must do, specific enough to act on with no other "
        "context (which file, which command, what 'done' looks like).",
    ],
    run_at: Annotated[
        str, "ISO datetime for a one-shot step (e.g. '2026-10-15T09:00:00'). Empty = recurring."
    ] = "",
    schedule: Annotated[
        str,
        "Standard cron expression for a recurring step (at most hourly, e.g. "
        "'0 9 * * 1-5' for weekdays; day-of-week 0 or 7 = Sunday). Empty = one-shot.",
    ] = "",
    timezone: Annotated[str, "IANA timezone for the schedule (default UTC)"] = "UTC",
) -> str:
    """Schedule a follow-up step for yourself on a Cortex prototype or project, so
    the work continues after this run: a check tomorrow, a daily data pull, the
    next PLAN phase. Each step runs as a full turn of yours with your tools,
    inside the item's folder, and reports what it produced. Bounded: at most a
    few pending steps per agent and two per item; recurring steps at most hourly.
    """
    root = _workspace_root()
    item = ci.get_item(root, item_id.strip())
    if item is None:
        return f"Cortex item {item_id} not found. Use cortex_list to see valid ids."
    if item.get("status") == "discarded":
        return f"'{item.get('title')}' is discarded; restore it before scheduling work on it."
    if item.get("kind") not in ("prototype", "project"):
        return (
            f"'{item.get('title')}' is a {item.get('kind')}. Steps are scheduled on prototypes "
            "and projects; promote it first (cortex_promote) or explore it (cortex_explore)."
        )
    instruction = instruction.strip()
    if len(instruction) < 20:
        return "Give the step a specific instruction (what to do, in which file, what 'done' is)."
    run_at = run_at.strip()
    schedule = schedule.strip()
    if bool(run_at) == bool(schedule):
        return "Provide exactly one of run_at (one-shot) or schedule (recurring cron)."
    if schedule and _cron_too_frequent(schedule):
        return "A recurring step may run at most hourly; use a cron with a fixed minute."
    tz_name = timezone.strip() or "UTC"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz_name)
    except Exception:
        return f"Unknown timezone '{tz_name}'. Use an IANA name such as 'UTC' or 'Asia/Singapore'."

    agent = owner_agent_name()
    if not agent:
        return "[tool-error] cortex_schedule: no agent is bound to this turn."
    from olano.config.system_settings import effective_max_self_scheduled_tasks

    try:
        config = _agent_config(root)
        cap = effective_max_self_scheduled_tasks(config)
    except Exception:
        cap = effective_max_self_scheduled_tasks(None)
    pending = _pending_cortex_tasks(agent)
    if len(pending) >= cap:
        return (
            f"You already have {len(pending)} pending Cortex steps (the limit is {cap}). "
            "Let one finish, or remove one with scheduler_delete, before adding another."
        )
    marker = f"[cortex-item:{item['id']}]"
    on_item = [t for t in pending if marker in str(t.get("prompt") or "")]
    if len(on_item) >= _MAX_STEPS_PER_ITEM:
        return (
            f"'{item.get('title')}' already has {len(on_item)} pending steps "
            f"(the limit is {_MAX_STEPS_PER_ITEM} per item)."
        )

    folder = root / str(item.get("path") or "")
    kind = str(item.get("kind"))
    record = "RESULTS.md" if kind == "prototype" else "PLAN.md, STATUS.md and JOURNAL.md"
    prompt = (
        f"{marker} Cortex execution step for the {kind} '{item.get('title')}' "
        f"(item {item['id']}, folder {folder}).\n\n"
        f"Step: {instruction}\n\n"
        f"Start with cortex_view {item['id']} to see the current state, then work inside that "
        f"folder. Do the step for real: run and verify what you build. Record the outcome in "
        f"{record} (for a project, tick only the PLAN.md boxes you genuinely completed and "
        f"append a dated JOURNAL.md entry). Name every file you created or changed with its "
        f"full path on a 'Saved: <path>' line. Your prompt lists your teammates and what "
        f"each can do; when the step needs a capability you lack, ask that agent with "
        f"talk_to_agent (exact request, file paths included) and record its reply in the "
        f"folder. If the step is finished or impossible, say so plainly and do not schedule "
        f"it again."
    )
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB()
    task_id = db.create_task(
        name=f"Cortex: {str(item.get('title'))[:50]}",
        prompt=prompt,
        schedule=schedule or None,
        run_at=run_at or None,
        timezone=tz_name,
        agent_name=agent,
        visibility="private",
        created_by=cortex_task_creator(agent),
    )
    runtime_note = ""
    try:
        from olano.scheduler.cron import get_active_scheduler

        scheduler = get_active_scheduler()
        if scheduler is not None:
            ok, message = scheduler.sync_db_task(task_id)
            runtime_note = f" Runtime: {message}"
    except Exception:
        runtime_note = ""
    try:
        from olano.runtime.api import _push_event

        _push_event(
            "scheduler_update",
            {"action": "created", "id": task_id, "created_by": cortex_task_creator(agent)},
        )
    except Exception:
        pass
    log_action(
        root,
        "cortex_schedule",
        summary=f"Scheduled a step on {kind}: {item.get('title')}",
        detail={
            "item_id": item["id"],
            "task_id": task_id,
            "schedule": schedule or None,
            "run_at": run_at or None,
            "instruction": instruction[:200],
        },
    )
    when = f"cron {schedule} ({tz_name})" if schedule else f"once at {run_at} ({tz_name})"
    try:
        from olano.scheduler.clock import weekday_note

        days = weekday_note(schedule)
    except Exception:
        days = ""
    if days:
        when += f"; {days}"
    return (
        f"Scheduled step on {kind} '{item.get('title')}' [{item['id']}]: {when}. "
        f"Task {task_id}.{runtime_note}"
    )


@tool
@_index_guard
async def cortex_explore(
    item_id: Annotated[str, "Cortex item id to research in depth (from cortex_list)"],
) -> str:
    """Explore a Cortex item in depth — run a deep research pass that enriches the
    item's folder with findings and appends an '## Exploration' section to its
    detail file. The item returns to active (enriched) when done."""
    root = _workspace_root()
    item = ci.get_item(root, item_id.strip())
    if item is None:
        return f"Cortex item {item_id} not found. Use cortex_list to see valid ids."
    ci.set_status(root, item_id.strip(), "exploring")
    try:
        from olano.cognition.ideation import run_idea_explore

        config = _agent_config(root)
        outcome = await run_idea_explore(config, root, item)
    except Exception as exc:
        ci.set_status(root, item_id.strip(), "active")
        return f"[tool-error] cortex_explore: {exc}"
    ci.set_status(root, item_id.strip(), "active")
    log_action(
        root,
        "cortex_explore",
        summary=outcome.summary or "Explored",
        detail={
            "item_id": item_id.strip(),
            "status": outcome.status,
            "artifacts": outcome.artifacts,
        },
    )
    if outcome.status not in ("completed", "empty"):
        return (
            f"[tool-error] Exploration of '{item.get('title')}' did not complete: "
            f"{outcome.error or outcome.summary or outcome.status}"
        )
    result = f"Explored '{item.get('title')}' [{item_id.strip()}]."
    if outcome.summary:
        result += f" {outcome.summary[:400]}"
    if outcome.artifacts:
        result += "\nArtifacts:\n" + "\n".join(f"- {a}" for a in outcome.artifacts[:20])
    return result


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
