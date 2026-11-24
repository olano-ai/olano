"""Per-user scheduling tools for a multi-user agent, with identity injected.

An end-user of a multi-user agent can ask for their own scheduled work ("every
morning at 8, send me my bill summary"). These tools store that job with the
asker's platform + raw sender id — resolved from the authenticated request
context, exactly like ``remember``/``execute_action``, and **never** a tool
parameter — so a prompt-injected "schedule that for user 449281" has nothing
to manipulate, and each user can only ever see or cancel their own jobs.

When such a job fires, the scheduler binds the stored identity for the run
(``scheduler/multi_user.run_customer_task_turn``): the turn's per-customer
tools resolve the right person, their subscription is re-checked at fire time,
and the reply is delivered back to them on their own chat.

Two guard rails, both operator knobs on ``customer_service``
(see :class:`olano.config.agent_config.CustomerServiceConfig`):

* ``min_job_interval_minutes`` — a recurring job may never fire more often
  than this (default hourly). Checked against the cron's actual next firings,
  not its spelling, so ``0,30 * * * *`` is caught as 30-minutely.
* ``max_jobs_per_user`` — most enabled jobs one user may hold at a time.

Every scheduled turn spends the agent owner's tokens; the caps bound a user's
daily cost at ``max_jobs_per_user × (1440 / min_job_interval_minutes)`` runs.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Annotated

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

#: How many consecutive fire times the interval floor samples. Four gaps catch
#: list-form minutes (``0,30 * * * *``) and step forms alike; irregular crons
#: whose *shortest* gap is compliant pass, which is the intended contract.
_FLOOR_SAMPLE_FIRES = 5


def _identity() -> tuple[str, str] | None:
    """``(platform, raw_user_id)`` of the authenticated sender, or None.

    None under the scheduler's synthetic context or an unidentified session —
    the same refusal rule as every other identity-injected tool.
    """
    from olano.memory.customer_memory import active_customer_platform
    from olano.security.request_context import get_request_context

    platform = active_customer_platform()
    user_id = (get_request_context().user_id or "").strip()
    if not platform or not user_id:
        return None
    return platform, user_id


def _no_identity_message() -> str:
    return (
        "No identified user in this conversation, so there is no personal "
        "schedule to manage. This tool only works on a message from an "
        "authenticated sender."
    )


def min_cron_gap_minutes(schedule: str, tz_name: str) -> float | None:
    """Shortest gap (minutes) between consecutive firings of a 5-field cron.

    Computed from the trigger's actual next fire times, so every spelling of
    "too often" is caught the same way. Returns None when the expression
    produces fewer than two future firings (effectively one-shot — nothing to
    rate-limit). Raises ``ValueError`` for an unparseable expression.
    """
    from zoneinfo import ZoneInfo

    from apscheduler.triggers.cron import CronTrigger

    from olano.scheduler.clock import standard_weekdays

    parts = schedule.strip().split()
    if len(parts) != 5:
        raise ValueError("cron must have 5 fields: minute hour day month day_of_week")
    minute, hour, day, month, dow = parts
    trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=standard_weekdays(dow),
        timezone=ZoneInfo(tz_name),
    )
    fires: list[datetime] = []
    t = trigger.get_next_fire_time(None, datetime.now(ZoneInfo(tz_name)))
    while t is not None and len(fires) < _FLOOR_SAMPLE_FIRES:
        fires.append(t)
        t = trigger.get_next_fire_time(t, t)
    if len(fires) < 2:
        return None
    return min((b - a).total_seconds() / 60 for a, b in itertools.pairwise(fires))


def make_customer_scheduler_tools(agent_name: str, cs_config) -> list:
    """Build the per-user scheduling tools for one multi-user agent.

    *cs_config* is the agent's ``CustomerServiceConfig`` — the caps are read
    per call (`getattr` with safe defaults), so a hot-reloaded config object
    with different limits applies without rebuilding the tools.
    """

    def _max_jobs() -> int:
        return max(1, int(getattr(cs_config, "max_jobs_per_user", 3) or 3))

    def _floor_minutes() -> int:
        return int(getattr(cs_config, "min_job_interval_minutes", 60) or 0)

    def _db():
        from olano.scheduler.db import SchedulerDB

        return SchedulerDB()

    def _sync_runtime(task_id: str) -> str:
        try:
            from olano.scheduler.cron import get_active_scheduler

            scheduler = get_active_scheduler()
            if scheduler is not None:
                _, message = scheduler.sync_db_task(task_id)
                return f" {message}"
        except Exception:
            pass
        return ""

    def _push(action: str, task_id: str) -> None:
        try:
            from olano.runtime.api import _push_event

            _push_event("scheduler_update", {"action": action, "id": task_id})
        except Exception:
            pass

    @tool
    def schedule_my_task(
        name: Annotated[str, "Short label for the task, shown back to the user"],
        prompt: Annotated[
            str,
            "What to do when it fires, written as an instruction to you (e.g. "
            "'Send this user their upcoming bill summary'). The run executes "
            "as this user, and your reply is delivered to them.",
        ],
        schedule: Annotated[
            str,
            "Cron expression for a recurring task (e.g. '0 8 * * *' = 8am "
            "daily). Leave empty for a one-time task.",
        ] = "",
        run_at: Annotated[
            str,
            "ISO datetime for a one-time task (e.g. '2026-09-01T09:00:00'). "
            "Leave empty for recurring tasks.",
        ] = "",
        timezone: Annotated[
            str,
            "IANA timezone the schedule is interpreted in (e.g. "
            "'America/Costa_Rica'). Ask the user theirs if unsure.",
        ] = "UTC",
    ) -> str:
        """Schedule a recurring or one-time task for the CURRENT user only.

        The user is resolved automatically from the conversation — never pass,
        ask for, or accept a user id. When the task fires, it runs as this
        user (their private memory and files) and the result is sent to them
        on this same chat. Recurring tasks cannot fire more often than the
        agent's configured minimum interval, and each user has a small cap on
        how many scheduled tasks they may keep.
        """
        identity = _identity()
        if identity is None:
            return _no_identity_message()
        platform, user_id = identity

        if not name.strip():
            return "Error: the task needs a short name."
        if not prompt.strip():
            return "Error: the task needs an instruction (what should happen when it fires)."
        if bool(schedule.strip()) == bool(run_at.strip()):
            return "Error: provide exactly one of `schedule` (recurring) or `run_at` (one-time)."

        tz_name = timezone.strip() or "UTC"
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)
        except Exception:
            return (
                f"Error: unknown timezone '{tz_name}'. Use IANA names like 'UTC', "
                "'America/New_York', 'Europe/London'."
            )

        if schedule.strip():
            floor = _floor_minutes()
            try:
                gap = min_cron_gap_minutes(schedule, tz_name)
            except Exception as exc:
                return f"Error: invalid cron schedule ({exc})."
            if floor > 0 and gap is not None and gap < floor:
                return (
                    f"Error: that schedule would run every {gap:.0f} minutes; the "
                    f"minimum allowed interval on this agent is {floor} minutes. "
                    "Pick a less frequent schedule."
                )
        else:
            try:
                when = datetime.fromisoformat(run_at.strip())
            except ValueError:
                return "Error: `run_at` must be an ISO datetime like '2026-09-01T09:00:00'."
            if when.tzinfo is None:
                when = when.replace(tzinfo=tz)
            if when <= datetime.now(dt_timezone.utc).astimezone(tz) + timedelta(seconds=30):
                return "Error: `run_at` must be in the future."

        db = _db()
        existing = db.count_user_tasks(agent_name, platform, user_id)
        if existing >= _max_jobs():
            return (
                f"You already have {existing} scheduled task(s) — the limit on this "
                f"agent is {_max_jobs()} per user. Cancel one first "
                "(list_my_scheduled_tasks / cancel_my_scheduled_task)."
            )

        task_id = db.create_task(
            name=name.strip(),
            prompt=prompt.strip(),
            schedule=schedule.strip() or None,
            run_at=run_at.strip() or None,
            timezone=tz_name,
            agent_name=agent_name,
            visibility="private",
            created_by=agent_name,
            platform=platform,
            user_id=user_id,
        )
        runtime_note = _sync_runtime(task_id)
        _push("created", task_id)
        kind = f"recurring ({schedule.strip()})" if schedule.strip() else f"one-time at {run_at}"
        logger.info(
            "[%s] user task created id=%s for %s-... (%s)", agent_name, task_id[:8], platform, kind
        )
        return (
            f"Scheduled! '{name.strip()}' — {kind}, timezone {tz_name}. "
            f"Task id: {task_id[:8]}.{runtime_note}"
        )

    @tool
    def list_my_scheduled_tasks() -> str:
        """List the CURRENT user's own scheduled tasks (never anyone else's)."""
        identity = _identity()
        if identity is None:
            return _no_identity_message()
        platform, user_id = identity
        tasks = _db().list_user_tasks(agent_name, platform, user_id)
        if not tasks:
            return "You have no scheduled tasks."
        lines = [f"Your scheduled tasks ({len(tasks)}):"]
        for t in tasks:
            status = "active" if t.get("enabled") else "paused"
            when = t.get("schedule") or t.get("run_at") or "?"
            lines.append(
                f"- [{t['id'][:8]}] {t['name']} — {when} ({t.get('timezone') or 'UTC'}), "
                f"{status}, ran {t.get('run_count', 0)} time(s)"
            )
        return "\n".join(lines)

    @tool
    def cancel_my_scheduled_task(
        task_id: Annotated[str, "Task id (full, or the 8-character short id) to cancel"],
    ) -> str:
        """Cancel one of the CURRENT user's scheduled tasks, permanently.

        Only this user's own tasks can be matched — another user's task id,
        even a valid one, is reported as not found.
        """
        identity = _identity()
        if identity is None:
            return _no_identity_message()
        platform, user_id = identity
        db = _db()
        # Resolve the prefix ONLY within this user's own tasks: matching the
        # global table would let a guessed prefix probe other users' jobs.
        mine = db.list_user_tasks(agent_name, platform, user_id)
        wanted = task_id.strip()
        matches = [t for t in mine if t["id"] == wanted or str(t["id"]).startswith(wanted)]
        if not matches:
            return f"No scheduled task of yours matches '{task_id}'."
        if len(matches) > 1:
            return f"'{task_id}' is ambiguous — use more characters of the id."
        task = matches[0]
        full_id = str(task["id"])
        try:
            from olano.scheduler.cron import get_active_scheduler

            scheduler = get_active_scheduler()
            if scheduler is not None:
                scheduler.remove_db_task(full_id)
        except Exception:
            pass
        if not db.delete_task(full_id):
            return f"Could not cancel task {full_id[:8]} — try again."
        _push("deleted", full_id)
        return f"Canceled '{task['name']}' ({full_id[:8]})."

    return [schedule_my_task, list_my_scheduled_tasks, cancel_my_scheduled_task]
