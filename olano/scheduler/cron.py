"""
APScheduler-based cron scheduler with heartbeat support.

Schedules periodic agent runs from the `schedule` list in agent.json5.
Heartbeat reads HEARTBEAT.md and fires tasks on their schedules.
Cron expression format: "minute hour dom month dow"
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from olano.cognition.reliability import is_retryable_code, jittered, retry_delays
from olano.config.home import load_home_config
from olano.config.system_settings import (
    DEFAULT_HEARTBEAT_INTERVAL_MINUTES,
    cortex_enabled,
    load_scheduler_settings,
)
from olano.scheduler import clock

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig, ScheduleConfig

logger = logging.getLogger(__name__)


# Per-agent cron override fields (``agent.json5 → cognition``) by phase id.
_PHASE_CRON_OVERRIDE: dict[str, str] = {
    "memory_garden": "memory_garden_cron",
    "pulse": "pulse_agent_cron",
    "workspace_optimize": "workspace_optimize_cron",
    "skill_librarian": "skill_librarian_cron",
}


def phase_cron_for(config: AgentConfig, phase: Any, intensity: str) -> str:
    """The cron string a phase is registered with for this agent.

    An explicit per-agent ``*_cron`` is honoured verbatim. Otherwise the
    intensity preset's cadence applies, staggered per agent+phase so the fleet
    does not fire in lockstep. The one resolver behind both the registration
    below and ``GET /api/cognition/status``, so the Scheduler view shows the
    minute a job will actually fire at. (These four fields shipped as concrete
    strings once, which made the ``or`` here unreachable — see
    ``CognitionConfig``.)
    """
    from olano.cognition.orchestrator import cadence_for

    cog = getattr(config, "cognition", None)
    field = _PHASE_CRON_OVERRIDE.get(phase.id)
    explicit = getattr(cog, field, None) if field and cog is not None else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return _stagger_cron(cadence_for(phase, intensity), f"{config.name}:{phase.id}")


def _cognition_retry_job_id(agent_name: str, phase_id: str) -> str:
    return f"{agent_name}_cortex_{phase_id}_retry"


def _stagger_cron(expr: str, key: str) -> str:
    """Spread a registry cadence across its hour, deterministically per ``key``.

    Every agent on a box used to fire a phase in the identical minute — the
    cadence table is shared and nothing offset it — so an N-agent fleet was N
    concurrent DEEP runs against one provider quota at 04:30 sharp. The offset
    is a stable hash of ``agent:phase`` (never random: a restart must not move
    a job, and the Scheduler view must show the same minute every time), added
    to the minute field modulo 60 so the job stays inside its scheduled hour.

    Only a **single-integer** minute field is touched. ``*``, a list, a range
    or a step is left verbatim — the registry meant those to be multi-valued
    and a shifted list would change what the cadence says. An operator's
    explicit ``*_cron`` override never reaches this function at all.
    """
    parts = expr.strip().split()
    if len(parts) != 5 or not parts[0].isdigit():
        return expr
    offset = int(hashlib.blake2b(key.encode("utf-8"), digest_size=2).hexdigest(), 16) % 60
    parts[0] = str((int(parts[0]) + offset) % 60)
    return " ".join(parts)


# After (re)registration or an interval change, the first heartbeat poll fires
# within this many minutes (or the interval itself, if shorter) — a long
# interval (e.g. daily) must not mean the first check is a day away.
_HEARTBEAT_FIRST_POLL_MINUTES = 5
# Local hour at which the fleet-wide daily work-journal highlights are written
# for the just-completed day, so the Overview digest is ready each morning.
_DAILY_HIGHLIGHTS_HOUR = 8

#: How many times, and how far apart, the boot-time load of the scheduler DB
#: is retried when the database cannot be read. It used to be tried once and
#: logged at DEBUG, so a locked or half-migrated ``scheduler.db`` at boot meant
#: no saved task ran until the next restart and nothing said so.
DB_LOAD_RETRIES = 5
DB_LOAD_RETRY_DELAY_S = 30


class ScheduledRunTimeout(RuntimeError):
    """A scheduled run exceeded ``scheduler.max_run_minutes`` and was stopped."""


#: Grace for the short-cadence internal jobs (heartbeat, issue polls, Cortex
#: phases): the next tick is minutes away, so a dropped one costs little. The
#: scheduled tasks people write get ``scheduler.misfire_grace_minutes``.
INTERNAL_MISFIRE_GRACE_S = 300
# Monotonic, process-local half of every ``fresh_run_thread_id`` suffix (see
# there for why randomness alone is not enough). ``itertools.count`` is atomic
# under CPython's GIL, but the lock keeps that true on a free-threaded build —
# firings arrive from the scheduler's event loop and from API threads alike.
_RUN_ID_SEQUENCE = itertools.count()
_RUN_ID_LOCK = threading.Lock()


def fresh_run_thread_id(base: str) -> str:
    """Return a per-run thread id derived from ``base``.

    Scheduled jobs used to pin one constant thread id (``scheduled_<agent>``,
    ``scheduled_<agent>_<task8>``, ``heartbeat_<agent>``), so **every** firing
    appended to the same LangGraph conversation, forever. Two things made that
    expensive, and they compound:

    * LangGraph rewrites the entire ``messages`` channel on every step, so the
      per-step checkpoint blob is the size of the whole accumulated history. One
      production thread reached 14 MB of ``messages`` across 27 checkpoints, with
      a single snapshot at 1.88 MB — run N pays to re-serialize runs 1..N-1.
    * That (de)serialization runs on the event loop, so a long autonomous job
      starved ``/api/status`` and the dashboard for the duration. Observed: one
      2-hourly task holding ~65% of a core for 26 minutes.

    A fresh id per firing keeps each run's checkpoint proportional to that run.
    Callers must still honour an explicitly configured ``thread_id`` — that field
    is documented as opt-in memory continuity, so it is never freshened.

    The timestamp is human-greppable (it lines up with the firing's log line) and
    the suffix keeps two firings in the same second distinct — a cron fire racing
    a dashboard "Run now" would otherwise collide. That suffix is ``<seq><rand>``:
    randomness alone made distinctness a coin flip (4 hex digits is 65 536 values,
    so 50 ids in one second collide ~1.8% of the time by the birthday bound, and a
    collision means one run silently *resumes* another run's thread — exactly the
    bug this function exists to prevent). ``seq`` is a process-local monotonic
    counter, so in-process uniqueness is now a guarantee: wrapping it needs 65 536
    firings inside the same second. ``rand`` keeps separate processes on one
    checkpoint store apart (a CLI run racing the runtime's scheduler).
    """
    from uuid import uuid4

    with _RUN_ID_LOCK:
        seq = next(_RUN_ID_SEQUENCE) & 0xFFFF
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{base}@{stamp}-{seq:04x}{uuid4().hex[:4]}"


_ACTIVE_SCHEDULER: OlanoScheduler | None = None
# Module-level lock guarding the global ``_ACTIVE_SCHEDULER`` reference so
# concurrent ``start()`` / ``stop()`` from different threads can't observe
# a half-swapped value.
_SCHEDULER_REGISTRY_LOCK = threading.Lock()


class OlanoScheduler:
    """
    Wraps APScheduler to manage all agent cron jobs.

    Supports:
    - Regular scheduled jobs from agent.json5 `schedule` config
    - Heartbeat jobs that read HEARTBEAT.md and execute tasks
    - SQLite-persisted tasks loaded from SchedulerDB on startup

    Usage:
        scheduler = OlanoScheduler()
        scheduler.register_agent(config, runner, workspace_root)
        scheduler.start()
        ...
        scheduler.stop()
    """

    def __init__(self) -> None:
        # A busy runtime loop routinely wakes cron jobs a second or two late;
        # APScheduler's default misfire_grace_time of 1s then SKIPS the run
        # outright (observed daily: every job sharing an on-the-hour slot was
        # dropped with "was missed by 0:00:01.4"). Five minutes of grace runs
        # them late instead; coalesce collapses a backlog into one run.
        # max_instances=1 is APScheduler's default, but it is stated here
        # because it is a DELIBERATE product decision, not an inherited one: two
        # copies of the same agent task running concurrently would share a
        # thread id and race each other's workspace writes. The consequence — a
        # firing gets DROPPED whenever the previous run is still going — is why
        # the skip listener below exists.
        self._scheduler = AsyncIOScheduler(
            job_defaults={"misfire_grace_time": 300, "coalesce": True, "max_instances": 1}
        )
        # job_id → task_id for DB-backed jobs. The APScheduler id is truncated
        # (`db_<first 8>`), so an event carrying only that id cannot address the
        # tasks/history tables without this.
        self._db_task_ids: dict[str, str] = {}
        # Loop the scheduler (and every runner-bound asyncio object) lives on;
        # captured in start(). run_db_task_now uses it to route thread-origin
        # requests back onto this loop instead of spinning up a private one.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._jobs: dict[str, list[str]] = {}  # agent_name → list of job_ids
        # agent_name → list of config-schedule descriptors (one per entry in
        # agent.json5 `schedule`). Config jobs live ONLY in the agent's file,
        # never in the scheduler DB, so this registry is what lets the dashboard
        # enumerate them, show them next to DB tasks, and route edits back to the
        # file. Descriptor shape: see _describe_config_job().
        self._config_jobs: dict[str, list[dict[str, Any]]] = {}
        self._runners: dict[str, Any] = {}
        # agent_name → live heartbeat settings ({"enabled": bool,
        # "interval_minutes": int}). The poll closure reads this on every tick,
        # so set_heartbeat_options() applies without re-registering the agent.
        self._heartbeat_settings: dict[str, dict[str, Any]] = {}
        # agent_name → AgentConfig, kept ONLY for agents whose schedule has a
        # `for_each_customer` entry (Run-now needs the config + entry back).
        # A normal agent never lands here — multi-user bookkeeping must cost
        # single-user deployments nothing.
        self._multi_user_configs: dict[str, Any] = {}
        # agent_name → workspace_root passed at register_agent time. Cached so a
        # live cognition re-sync (resync_agent_cognition_jobs) can rebuild the
        # agent's cortex jobs without the caller re-supplying the path.
        self._workspace_roots: dict[str, Path] = {}
        self._db_loaded: bool = False
        self._db_load_attempts: int = 0
        # task_id → the fire time a boot-time catch-up is making up for; the
        # run that consumes it tells the agent how late it is.
        self._catch_up_due: dict[str, datetime] = {}
        # Re-entrant so individual handlers can call into one another (e.g.
        # sync_db_task → remove_db_task) without deadlocking.
        self._lock = threading.RLock()

    def start(self) -> None:
        global _ACTIVE_SCHEDULER
        with _SCHEDULER_REGISTRY_LOCK:
            self._register_skip_listener()
            self._scheduler.start()
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                # Started outside a coroutine — fall back to the loop
                # AsyncIOScheduler bound itself to.
                self._loop = getattr(self._scheduler, "_eventloop", None)
            _ACTIVE_SCHEDULER = self
        self._load_db_tasks()
        self._register_fleet_jobs()
        logger.info("Scheduler started")

    @staticmethod
    def _task_grace() -> timedelta:
        """How late a scheduled task or config entry may still run.

        ``config.yaml → scheduler.misfire_grace_minutes`` (six hours by
        default). APScheduler judges every firing against this and *runs it
        late* inside the window; a firing past it is recorded as skipped. The
        five-minute default that used to apply to these jobs turned any busy
        spell on the event loop into a lost daily run.
        """
        return timedelta(minutes=load_scheduler_settings().misfire_grace_minutes)

    @staticmethod
    def _run_time_limit_seconds() -> float | None:
        """``scheduler.max_run_minutes`` in seconds, or None when unlimited."""
        minutes = load_scheduler_settings().max_run_minutes
        return float(minutes) * 60 if minutes > 0 else None

    async def _bounded(self, awaitable: Any, *, label: str) -> Any:
        """Await one scheduled turn under the wall-clock limit.

        A scheduled turn had no bound at all: on 2026-11-02 one box's three
        campaign runs had been going for five to fifteen hours (84 model calls
        in one of them) when a restart ended them. Every one of those held
        its task's only slot (``max_instances=1``), so the task's next firing
        was dropped as "previous run still executing", and the person who
        wrote the schedule saw a job that fired once and then never again.
        On the limit the turn is cancelled (the runner's own cleanup runs, so
        the thread's lock and checkpoint are released) and the caller records
        the reason; what the run wrote before the cut stays.
        """
        limit = self._run_time_limit_seconds()
        if limit is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=limit)
        except TimeoutError:
            minutes = load_scheduler_settings().max_run_minutes
            raise ScheduledRunTimeout(
                f"stopped after {minutes} minutes: the run exceeded "
                "scheduler.max_run_minutes and was cancelled so the task's next "
                "firing can run (what it had written is kept). Shorten the work, "
                "split the task, or raise the limit in config.yaml."
            ) from None

    def next_run_for_task(self, task_id: str) -> str | None:
        """The live next fire time of a saved task, or None when it holds no job.

        What the Scheduler tab's Next column shows for a saved task: the
        column used to read a ``tasks.next_run`` cell nothing ever wrote, so
        every saved task showed a dash whether or not it was registered.
        """
        return self._next_run_for(_db_job_id(task_id))

    def _due_fire_for(self, trigger: Any, *, now: datetime | None = None) -> datetime | None:
        """The fire time this run is answering: the trigger's most recent fire
        inside the grace window, or None when the trigger cannot say."""
        try:
            moment = now or datetime.now(timezone.utc)
            return clock.latest_due_fire(
                trigger, now=moment, not_before=moment - self._task_grace()
            )
        except Exception:
            return None

    def _register_skip_listener(self) -> None:
        """Record runs APScheduler drops, instead of losing them to the log.

        Two events end a firing before it starts, and neither used to leave any
        trace a product surface could see:

        - ``EVENT_JOB_MAX_INSTANCES`` — the previous run is still going. Common
          on short cadences: an agent turn takes 10-30s, so a per-minute task
          that hits a slow tool drops firings silently (observed 2026-08-14 on a
          ``* * * * *`` job, with the Scheduler view still showing it healthy).
        - ``EVENT_JOB_MISSED`` — the firing fell outside ``misfire_grace_time``.

        Both now write a history row, so run-count, last-run and the task's
        history reflect what actually happened. Best-effort throughout: a
        listener that raises would take down the scheduler thread.
        """
        try:
            from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
        except Exception:  # pragma: no cover - APScheduler always ships these
            logger.debug("Scheduler skip listener unavailable", exc_info=True)
            return

        def _on_skip(event: Any) -> None:
            try:
                job_id = getattr(event, "job_id", "") or ""
                if getattr(event, "code", None) == EVENT_JOB_MISSED:
                    reason = (
                        "skipped: firing was missed by more than the grace period "
                        "(the runtime was busy or restarting for longer than "
                        "scheduler.misfire_grace_minutes allows)"
                    )
                else:
                    reason = (
                        "skipped: the previous run was still executing "
                        "(only one run of a task is allowed at a time — "
                        "consider a longer interval)"
                    )
                logger.warning("Scheduled job '%s' %s", job_id, reason)
                self._record_skipped_run(job_id, reason)
            except Exception:  # pragma: no cover - must never break the scheduler
                logger.debug("Recording a skipped run failed", exc_info=True)

        self._scheduler.add_listener(_on_skip, EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED)

    def _record_skipped_run(self, job_id: str, reason: str) -> None:
        """Write a skipped firing to task_history, for DB and config jobs alike."""
        if job_id.startswith("cfg:"):
            # Synthetic id — add_history's UPDATE is a no-op and only the
            # history row lands (see _record_config_run).
            _record_config_run(job_id, error=reason)
            _notify_run_problem(
                job_id,
                agent=_config_job_agent(job_id),
                name=job_id,
                outcome="skipped",
                detail=reason,
            )
            return
        task_id = self._db_task_ids.get(job_id)
        if not task_id:
            return
        agent_name: str | None = None
        task_name = task_id[:8]
        try:
            from olano.scheduler.db import SchedulerDB

            db = SchedulerDB()
            row = db.get_task(task_id) or {}
            agent_name = row.get("agent_name") or None
            task_name = str(row.get("name") or task_name)
            db.add_history(task_id, error=reason)
        except Exception as exc:
            logger.debug("Skipped-run history record failed for %s: %s", job_id, exc)
        _notify_run_problem(
            task_id, agent=agent_name, name=task_name, outcome="skipped", detail=reason
        )
        try:
            from olano.runtime.api import _push_event

            _push_event("scheduler_update", {"action": "skipped", "id": task_id})
        except Exception:
            pass

    def _load_db_tasks(self) -> None:
        """Load enabled tasks from SQLite SchedulerDB and register with APScheduler.

        Two things this used to get wrong. A database that could not be read
        at that moment (locked by a migration, a half-written file) was logged
        at DEBUG and never tried again, so every saved task stayed unscheduled
        until the next restart with nothing on any surface saying so; it now
        warns, retries ``DB_LOAD_RETRIES`` times and raises a notification
        when it gives up. And a task whose fire time fell inside the restart
        was re-added with its next run computed from boot, so the run was lost
        without even a skipped row; with ``scheduler.catch_up_on_boot`` the
        sync runs such a task at once (``clock.missed_fire`` decides, against
        the task's own history).
        """
        if self._db_loaded:
            return
        try:
            from olano.scheduler.db import SchedulerDB

            db = SchedulerDB()
            tasks = db.list_tasks(enabled_only=True)
        except Exception as e:
            self._db_load_attempts += 1
            if self._db_load_attempts <= DB_LOAD_RETRIES:
                logger.warning(
                    "Scheduler DB not readable (attempt %d/%d): %s; retrying in %ds",
                    self._db_load_attempts,
                    DB_LOAD_RETRIES,
                    e,
                    DB_LOAD_RETRY_DELAY_S,
                )
                self._schedule_db_load_retry()
            else:
                logger.error(
                    "Scheduler DB still not readable after %d attempts: %s. Saved scheduled "
                    "tasks are NOT running; save any task again or restart the runtime.",
                    self._db_load_attempts,
                    e,
                )
                _notify_scheduler_db_unreadable(str(e))
            return
        catch_up = load_scheduler_settings().catch_up_on_boot
        loaded = 0
        for task in tasks:
            ok, _ = self.sync_db_task(task["id"], catch_up=catch_up)
            if ok:
                loaded += 1
        if loaded:
            logger.info("Loaded %d tasks from scheduler DB", loaded)
        self._db_loaded = True
        self._db_load_attempts = 0

    def _schedule_db_load_retry(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_later(DB_LOAD_RETRY_DELAY_S, self._load_db_tasks)
            return
        timer = threading.Timer(DB_LOAD_RETRY_DELAY_S, self._load_db_tasks)
        timer.daemon = True
        timer.start()

    def _register_fleet_jobs(self) -> None:
        """Register fleet-wide (agent-independent) scheduled jobs.

        Currently one: a daily morning job that writes the work-journal
        **highlights** narrative for the just-completed day. It summarises the
        actual work the fleet did — the conversations agents handled, the
        scheduled tasks and heartbeat checks they ran, the agent-to-agent
        messages they exchanged, and their cortex activity per agent — so the
        Overview daily digest is ready each morning without anyone clicking
        "Generate highlights". A completely quiet day is skipped (no LLM call,
        nothing to narrate).
        """

        async def _daily_highlights() -> None:
            from olano.analytics import journal

            day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
            try:
                work = await asyncio.to_thread(journal.gather_day_work, day)
                if journal.work_is_empty(work):
                    logger.info("Daily highlights: no activity on %s — skipping", day)
                    return
                await journal.generate_highlights(day, work=work)
                logger.info("Daily highlights generated for %s", day)
            except Exception as exc:
                logger.warning("Daily highlights job failed for %s: %s", day, exc)

        async def _rollup_report(period: str) -> None:
            from olano.analytics import reports

            anchor = reports.last_completed_anchor(period)
            try:
                report = await reports.generate_report(period, anchor)
                if report.get("narrative_error"):
                    logger.warning(
                        "%s report %s: %s", period, report.get("key"), report["narrative_error"]
                    )
                else:
                    logger.info("%s report generated for %s", period, report.get("key"))
            except Exception as exc:
                logger.warning("%s report job failed for %s: %s", period, anchor, exc)

        async def _weekly_report() -> None:
            await _rollup_report("weekly")

        async def _monthly_report() -> None:
            await _rollup_report("monthly")

        try:
            self._scheduler.add_job(
                _daily_highlights,
                trigger=CronTrigger(hour=_DAILY_HIGHLIGHTS_HOUR, minute=0),
                id="fleet_daily_highlights",
                name="olano:fleet:daily_highlights",
                replace_existing=True,
            )
            # Weekly rollup every Monday, monthly rollup on the 1st — both after
            # the daily job (which fills yesterday's per-agent highlights) so the
            # just-completed period's last day is included. Same map-reduce over
            # the stored daily highlights; scoped per member exactly like dailies.
            # The wrappers must be genuine coroutine functions: APScheduler runs a
            # plain callable in its executor and never awaits the coroutine it
            # returns, so a sync lambda here means the rollup never executes.
            self._scheduler.add_job(
                _weekly_report,
                trigger=CronTrigger(day_of_week="mon", hour=_DAILY_HIGHLIGHTS_HOUR, minute=30),
                id="fleet_weekly_report",
                name="olano:fleet:weekly_report",
                replace_existing=True,
            )
            self._scheduler.add_job(
                _monthly_report,
                trigger=CronTrigger(day=1, hour=_DAILY_HIGHLIGHTS_HOUR, minute=30),
                id="fleet_monthly_report",
                name="olano:fleet:monthly_report",
                replace_existing=True,
            )
            logger.info(
                "Fleet highlights jobs registered (daily %02d:00, weekly Mon, monthly 1st)",
                _DAILY_HIGHLIGHTS_HOUR,
            )
        except Exception as exc:
            logger.warning("Could not register fleet highlights jobs: %s", exc)

    def stop(self) -> None:
        global _ACTIVE_SCHEDULER
        with _SCHEDULER_REGISTRY_LOCK:
            self._scheduler.shutdown(wait=False)
            if _ACTIVE_SCHEDULER is self:
                _ACTIVE_SCHEDULER = None
        logger.info("Scheduler stopped")

    def register_agent(
        self,
        config: AgentConfig,
        runner: Any,
        workspace_root: Path | None = None,
    ) -> None:
        """Register all scheduled jobs for an agent, including heartbeat."""
        with self._lock:
            job_ids: list[str] = []
            self._runners[config.name] = runner
            if workspace_root:
                self._workspace_roots[config.name] = workspace_root

            # Regular scheduled jobs from config (agent.json5 `schedule`).
            # Deterministic ids (cfg:{agent}:{i}) so the dashboard can address
            # them for edit/pause/delete, and so a re-registration REPLACES the
            # existing job instead of adding a duplicate under a fresh random id.
            job_ids.extend(self._register_config_schedule(config, runner))

            # Heartbeat poll (whenever the agent has a workspace). Registered
            # even if HEARTBEAT.md doesn't exist yet — the poll re-checks the
            # workspace and skill files on every tick, so tasks added later
            # (e.g. via the heartbeat_add tool) and skill-level HEARTBEAT.md
            # files start firing without a runtime restart.
            if workspace_root:
                heartbeat_job_id = self._add_heartbeat_job(config, runner, workspace_root)
                if heartbeat_job_id:
                    job_ids.append(heartbeat_job_id)

            # Issue-tracker poll — registered for EVERY agent, deliberately
            # independent of the heartbeat (which is not enabled everywhere):
            # assigned-issue delivery must not depend on an unrelated feature.
            issues_job_id = self._add_issues_job(config, runner)
            if issues_job_id:
                job_ids.append(issues_job_id)

            # Gmail watch: off unless the agent's config turns it on.
            if workspace_root and config.gmail_watch.enabled:
                gmail_job_id = self._add_gmail_watch_job(config, runner, workspace_root)
                if gmail_job_id:
                    job_ids.append(gmail_job_id)

            # Cognition jobs (memory gardening, pulse agent) — autonomous, budget-gated.
            # Through cortex_enabled(), never a bare read of `cognition.enabled`:
            # that field is three-state (None ⇒ inherit the global), so a
            # truthiness check registers nothing for every agent that inherits —
            # i.e. almost the whole fleet, silently. It also has to consult the
            # global master switch and the deployment entitlement, which a raw
            # attribute read cannot do.
            if workspace_root and cortex_enabled(config):
                job_ids.extend(self._add_cognition_jobs(config, runner, workspace_root))

            self._jobs[config.name] = job_ids
        logger.info(
            "Registered %d scheduled jobs for agent '%s'",
            len(job_ids),
            config.name,
        )

    def unregister_agent(self, agent_name: str) -> None:
        with self._lock:
            self._runners.pop(agent_name, None)
            self._heartbeat_settings.pop(agent_name, None)
            self._config_jobs.pop(agent_name, None)
            self._workspace_roots.pop(agent_name, None)
            self._multi_user_configs.pop(agent_name, None)
            for job_id in self._jobs.pop(agent_name, []):
                try:
                    self._scheduler.remove_job(job_id)
                except Exception:
                    pass
            self._remove_cognition_retries(agent_name)

    # ----------------------------------------------------------------- db-backed jobs

    def sync_db_task(self, task_id: str, *, catch_up: bool = False) -> tuple[bool, str]:
        """Create, replace, or remove the APScheduler job for a persisted DB task.

        ``catch_up`` (the boot-time load only) runs the task at once when its
        most recent fire time fell inside the grace window with no run
        recorded since; a dashboard save never passes it, so editing a task
        can never trigger a surprise run.
        """
        with self._lock:
            return self._sync_db_task_locked(task_id, catch_up=catch_up)

    @staticmethod
    def _db_task_trigger(task: dict[str, Any]) -> tuple[Any | None, str]:
        """The APScheduler trigger for a task row, or ``(None, reason)``."""
        from zoneinfo import ZoneInfo

        task_id = str(task.get("id") or "")
        if task.get("schedule"):
            parts = str(task["schedule"]).strip().split()
            if len(parts) != 5:
                return None, f"Invalid cron schedule for task {task_id[:8]}."
            minute, hour, day, month, dow = parts
            return (
                CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=clock.standard_weekdays(dow),
                    timezone=task.get("timezone") or "UTC",
                ),
                "",
            )
        if task.get("run_at"):
            run_at = datetime.fromisoformat(str(task["run_at"]))
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=ZoneInfo(task.get("timezone") or "UTC"))
            return DateTrigger(run_date=run_at), ""
        return None, f"Task {task_id[:8]} has no schedule."

    def _sync_db_task_locked(self, task_id: str, *, catch_up: bool = False) -> tuple[bool, str]:
        try:
            from olano.scheduler.db import SchedulerDB

            db = SchedulerDB()
            task = db.get_task(task_id)
            job_id = _db_job_id(task_id)
            if task is None or not task.get("enabled", True):
                self.remove_db_task(task_id)
                return False, f"Task {task_id[:8]} is disabled or missing."

            agent_name = task.get("agent_name") or load_home_config().default_agent
            trigger, problem = self._db_task_trigger(task)
            if trigger is None:
                if "no schedule" in problem:
                    self.remove_db_task(task_id)
                return False, problem

            async def _run_db_task(tid=task_id) -> None:
                from olano.scheduler.db import SchedulerDB

                current_db = SchedulerDB()
                current = current_db.get_task(tid)
                if current is None:
                    return
                await self._execute_db_task(current, current_db)

            grace = self._task_grace()
            extra: dict[str, Any] = {}
            if catch_up:
                now = datetime.now(timezone.utc)
                missed = clock.missed_fire(
                    trigger,
                    now=now,
                    grace=grace,
                    last_run=task.get("last_run"),
                    created_at=task.get("created_at"),
                )
                if missed is not None:
                    logger.info(
                        "Scheduled task '%s' (%s) was due at %s while the runtime was down; "
                        "running it now.",
                        task_id[:8],
                        task.get("name"),
                        missed.isoformat(timespec="minutes"),
                    )
                    self._catch_up_due[task_id] = missed
                    extra["next_run_time"] = now
            self._scheduler.add_job(
                _run_db_task,
                trigger=trigger,
                id=job_id,
                name=f"db:{agent_name}:{task['name']}",
                replace_existing=True,
                misfire_grace_time=int(grace.total_seconds()),
                **extra,
            )
            # Lets the skip listener map a dropped firing back to its task row.
            self._db_task_ids[job_id] = task_id
            return True, f"Task {task_id[:8]} synced."
        except Exception as exc:
            logger.warning("Failed to sync DB task %s: %s", task_id[:8], exc)
            return False, f"Failed to sync task {task_id[:8]}: {exc}"

    def remove_db_task(self, task_id: str) -> bool:
        with self._lock:
            self._db_task_ids.pop(_db_job_id(task_id), None)
            try:
                self._scheduler.remove_job(_db_job_id(task_id))
                return True
            except Exception:
                return False

    def _dispatch_now(self, coro: Any, label: str) -> None:
        """Fire-and-forget a run-now coroutine on the right event loop.

        On the runtime loop: schedule it directly. From a worker thread (a sync
        agent tool in the executor): hand it to the runtime loop via
        ``run_coroutine_threadsafe`` — never a private ``asyncio.run`` loop, which
        would crash against the runner's loop-bound checkpointer/store locks. Only
        a one-shot process with no runtime loop (CLI) gets a fresh loop.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            runtime_loop = self._loop
            if runtime_loop is not None and not runtime_loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(coro, runtime_loop)
                future.add_done_callback(_log_run_now_failure(label))
            else:
                asyncio.run(coro)

    def run_config_job_now(self, job_id: str) -> tuple[bool, str]:
        """Queue an immediate run for a config-schedule job (agent.json5 entry).

        Mirrors run_db_task_now for the config store: looks up the registered
        descriptor, runs its message on the owning agent's runner right now
        (recording history under the same cfg:… id), independent of its cron.
        """
        with self._lock:
            descriptor = next(
                (d for descs in self._config_jobs.values() for d in descs if d["id"] == job_id),
                None,
            )
            if descriptor is None:
                return False, f"Config job {job_id} is not registered in this runtime."
            agent_name = str(descriptor["agent"])
            message = str(descriptor["message"])
            override_model = str(descriptor.get("model") or "").strip() or None
            override_effort = str(descriptor.get("reasoning_effort") or "").strip() or None
            # Same rule as the cron fire: pinned id wins, else a fresh thread.
            thread_id = descriptor.get("thread_id") or fresh_run_thread_id(
                f"scheduled_{agent_name}"
            )
            runner = self._runners.get(agent_name)
            fan_out_config = (
                self._multi_user_configs.get(agent_name)
                if descriptor.get("for_each_customer")
                else None
            )
        if runner is None:
            return False, f"Agent '{agent_name}' is not active in the runtime."

        if descriptor.get("for_each_customer"):
            # Multi-user Type-1 entry: Run-now fans out per customer, exactly
            # like the cron fire — never a single owner-context turn.
            index = int(descriptor.get("index", -1))
            if fan_out_config is None:
                return False, f"Config job {job_id} is missing its fan-out config."
            schedule_list = getattr(fan_out_config, "schedule", [])
            if not (0 <= index < len(schedule_list)):
                return False, f"Config job {job_id} is missing its fan-out entry."
            sched = schedule_list[index]

            async def _run_fan_out() -> None:
                from olano.scheduler.multi_user import run_for_each_customer

                try:
                    summary = await run_for_each_customer(job_id, fan_out_config, sched, runner)
                    _record_config_run(job_id, result=summary)
                except Exception as e:
                    logger.error("Run-now fan-out '%s' failed: %s", job_id, e)
                    _record_config_run(job_id, error=str(e))

            self._dispatch_now(_run_fan_out(), job_id)
            return True, f"Config job {job_id} queued for immediate per-customer execution."

        tz_name = str(descriptor.get("timezone") or "").strip() or None

        async def _run() -> None:
            await self._run_config_message(
                job_id,
                agent_name,
                message,
                thread_id,
                runner,
                tz_name=tz_name,
                override_model=override_model,
                override_reasoning_effort=override_effort,
            )

        self._dispatch_now(_run(), job_id)
        return True, f"Config job {job_id} queued for immediate execution."

    def run_db_task_now(self, task_id: str) -> tuple[bool, str]:
        """Queue an immediate run for a DB-backed task if the runtime scheduler is live."""
        try:
            from olano.scheduler.db import SchedulerDB

            db = SchedulerDB()
            task = db.get_task(task_id)
            if task is None:
                return False, f"Task {task_id[:8]} not found."

            async def _run() -> None:
                await self._execute_db_task(task, db, manual=True)

            self._dispatch_now(_run(), task_id)
            return True, f"Task {task_id[:8]} queued for immediate execution."
        except Exception as exc:
            logger.warning("Failed to queue DB task %s: %s", task_id[:8], exc)
            return False, f"Failed to run task {task_id[:8]}: {exc}"

    async def _execute_db_task(
        self, task: dict[str, Any], db: Any, *, manual: bool = False
    ) -> None:
        task_id = str(task["id"])
        agent_name = str(task.get("agent_name") or load_home_config().default_agent)
        task_name = str(task.get("name") or task_id[:8])
        prompt = str(task.get("prompt") or "").strip()
        runner = self._runners.get(agent_name)
        if runner is None:
            problem = f"Agent '{agent_name}' is not active in the runtime."
            db.add_history(task_id, error=problem)
            _notify_run_problem(
                task_id, agent=agent_name, name=task_name, outcome="failed", detail=problem
            )
            return
        if not prompt:
            db.add_history(task_id, error="Task prompt is empty.")
            return

        thread_id = fresh_run_thread_id(f"scheduled_{agent_name}_{task_id[:8]}")
        # The fire time this run answers: a boot-time catch-up carries its own,
        # a cron fire is read off the trigger, a dashboard "Run now" has none
        # (it is not late, it is on demand).
        due_at = self._catch_up_due.pop(task_id, None)
        if due_at is None and not manual:
            trigger, _ = self._db_task_trigger(task)
            due_at = self._due_fire_for(trigger) if trigger is not None else None
        # Prepend the autonomous-mode directive (no one is there to answer a
        # question) and the current date and time in the task's own timezone,
        # so a weekday rule in the prompt is decided from the clock and not
        # from the newest date the agent happens to find in its files.
        tz_name = str(task.get("timezone") or "") or None
        autonomous_prompt = clock.autonomous_preamble(prompt, tz_name=tz_name, due_at=due_at)
        # The task's own model, if it pinned one (NULL/"" = the agent's).
        override_model = str(task.get("model") or "").strip() or None
        # Its own effort level, if it pinned one (validated at save time against
        # the ladder of the model the task runs on; a stale value is dropped by
        # the model builder with a log line, never a failed run).
        override_effort = str(task.get("reasoning_effort") or "").strip() or None
        # Multi-user Type-2 marker: both set ⇒ the task belongs to ONE end-user
        # of a multi-user agent and the run binds their identity. NULL on every
        # normal task, whose dispatch below is unchanged.
        identity_platform = str(task.get("platform") or "").strip()
        identity_user = str(task.get("user_id") or "").strip()
        try:
            logger.info(
                "DB scheduled task '%s' firing for agent=%s model=%s effort=%s",
                task_id[:8],
                agent_name,
                override_model or "default",
                override_effort or "default",
            )
            if identity_platform and identity_user:
                from olano.scheduler.multi_user import (
                    CustomerTaskSkipped,
                    run_customer_task_turn,
                )

                try:
                    # Binds channel=<platform> + the raw sender id (so the
                    # identity-injected tools resolve the real customer),
                    # checks entitlement at fire time, and delivers the
                    # turn's reply back to the customer. The helper adds its
                    # own autonomous preamble.
                    response = await self._bounded(
                        run_customer_task_turn(
                            runner,
                            agent_name,
                            identity_platform,
                            identity_user,
                            prompt,
                            thread_id,
                            task_id,
                            tz_name=tz_name,
                        ),
                        label=task_name,
                    )
                except CustomerTaskSkipped as skip:
                    # Not a failure: the job stays enabled and re-checks on the
                    # next fire, so it resumes by itself after a resubscribe.
                    db.add_history(task_id, error=str(skip))
                    logger.info("DB scheduled task '%s' %s", task_id[:8], skip)
                    return
            else:
                from olano.security.request_context import (
                    ORIGIN_KEY,
                    make_origin,
                    push_request_context,
                )

                # A step the agent scheduled for ITSELF from a Cortex run
                # (`cortex_schedule`, created_by "cortex:<agent>") keeps the
                # cognition origin: HITL then holds `cognition.approval_required`
                # (external sends, money, identity edits) exactly as the
                # background run that scheduled it would have, instead of the
                # blanket approval a human's own scheduled task gets. Without
                # this, self-scheduling was a one-hop route around that list.
                from olano.tools.cortex_tools import is_cortex_task

                root_source = "cognition" if is_cortex_task(task) else "scheduled"
                with push_request_context(
                    source="scheduled",
                    channel="scheduler",
                    session_id=thread_id,
                    authenticated=True,
                    # Autonomous run, no specific human → stays the implicit owner so
                    # no active-human block renders. The origin descriptor drives
                    # HITL resolution (scheduled → auto-approve; cognition →
                    # auto-approve minus approval_required).
                    metadata={
                        ORIGIN_KEY: make_origin(root_source, False, root_session_id=thread_id),
                        "task_id": task_id,
                    },
                ):
                    # The override is passed only when the task pinned one, so
                    # a runner without the kwarg (fakes, older shims) keeps
                    # working for every default-model task.
                    response = await self._bounded(
                        runner.ainvoke(
                            autonomous_prompt,
                            thread_id=thread_id,
                            **({"override_model": override_model} if override_model else {}),
                            **(
                                {"override_reasoning_effort": override_effort}
                                if override_effort
                                else {}
                            ),
                        ),
                        label=task_name,
                    )
            result = str(response).strip() or "completed"
            db.add_history(task_id, result=result[:2000])
            _resolve_run_alert(task_id)
            if task.get("run_at") and not task.get("schedule"):
                db.update_task(task_id, enabled=0)
                self.remove_db_task(task_id)
                # A one-time job just left the active set — tell live views (and
                # the sidebar active-jobs badge) so the count drops immediately.
                try:
                    from olano.runtime.api import _push_event

                    _push_event("scheduler_update", {"action": "completed", "id": task_id})
                except Exception:
                    pass
        except Exception as exc:
            db.add_history(task_id, error=str(exc))
            logger.error("DB scheduled task '%s' failed: %s", task_id[:8], exc)
            _notify_run_problem(
                task_id, agent=agent_name, name=task_name, outcome="failed", detail=str(exc)
            )

    # ----------------------------------------------------------------- heartbeat

    @staticmethod
    def _collect_skill_heartbeats(workspace_root: Path) -> list[tuple[str, Path, str]]:
        """Return ``(skill_slug, heartbeat_path, skill_body)`` for every skill that has a
        ``HEARTBEAT.md``.  ``skill_body`` is the SKILL.md body (frontmatter stripped) used
        to give the agent skill-specific context when a task fires.

        Deduped by slug with ``skills/learned/`` preferred: a learned fork carries
        the full copy of its original (including HEARTBEAT.md), so registering
        both would fire every task twice — and the fork's version is the one the
        agent actually serves."""
        learned_root = workspace_root / "skills" / "learned"
        chosen: dict[str, tuple[str, Path, str]] = {}
        for skills_dir in (
            workspace_root / "skills",
            workspace_root / "system" / "skills",
        ):
            if not skills_dir.exists():
                continue
            # rglob the marker file, never "*": learned skills can vendor
            # node_modules/venv trees, and materializing every entry froze polls.
            for hb_path in sorted(skills_dir.rglob("HEARTBEAT.md")):
                skill_dir = hb_path.parent
                slug = skill_dir.name
                is_learned = learned_root in skill_dir.parents
                if slug in chosen:
                    prior_learned = learned_root in chosen[slug][1].parents
                    if prior_learned or not is_learned:
                        continue  # keep the learned copy (or the first seen)
                skill_md = skill_dir / "SKILL.md"
                body = ""
                if skill_md.exists():
                    try:
                        raw = skill_md.read_text(encoding="utf-8")
                        lines = raw.splitlines()
                        if lines and lines[0].strip() == "---":
                            end = next(
                                (i for i in range(1, len(lines)) if lines[i].strip() == "---"),
                                -1,
                            )
                            body = "\n".join(lines[end + 1 :]).strip() if end > 0 else raw.strip()
                        else:
                            body = raw.strip()
                    except OSError:
                        pass
                chosen[slug] = (slug, hb_path, body)
        return list(chosen.values())

    def _add_issues_job(self, config: AgentConfig, runner: Any) -> str | None:
        """Register the per-agent issue-tracker poll.

        Every agent gets one — issue delivery is deliberately decoupled from
        the heartbeat, which is off for many agents. Each tick re-reads the
        effective ``auto_process`` switch (home-wide ``issues:`` block +
        per-agent ``agent.json5 → issues:`` override), so toggling it in
        config.yaml applies within one interval; ``poll_minutes`` applies at
        boot / agent re-registration. The last-checked watermark lives in
        the issues store itself (``issue_watermarks``), not in any feature's
        state file. Manual mode (``auto_process: false``) means issues wait
        for a human prompt ("check your issues"), a scheduled job with such
        a message, or the dashboard.
        """
        from olano.config.system_settings import (
            effective_issues_auto_process,
            effective_issues_poll_minutes,
        )

        job_id = f"{config.name}_issues"
        poll_minutes = effective_issues_poll_minutes(config)

        async def _run_issues_poll() -> None:
            if not effective_issues_auto_process(config):
                return
            from olano.issues.delivery import (
                format_poll_message,
                mark_checked,
                pending_for_agent,
            )

            pending = await asyncio.to_thread(pending_for_agent, config.name)
            if not pending:
                return
            message = format_poll_message(pending)
            thread_id = fresh_run_thread_id(f"issues_{config.name}")
            try:
                from olano.security.request_context import (
                    ORIGIN_KEY,
                    make_origin,
                    push_request_context,
                )

                with push_request_context(
                    source="scheduled",
                    channel="scheduler",
                    session_id=thread_id,
                    authenticated=True,
                    metadata={
                        ORIGIN_KEY: make_origin("scheduled", False, root_session_id=thread_id)
                    },
                ):
                    await runner.ainvoke(message, thread_id=thread_id)
            except Exception as exc:
                logger.error("Issue poll delivery failed for '%s': %s", config.name, exc)
            # Advance even when the turn failed — a broken run must not become
            # an every-tick nag; a still-open issue resurfaces on its next
            # activity (any comment/assign/reopen bumps updated_at).
            await asyncio.to_thread(mark_checked, config.name)

        self._scheduler.add_job(
            _run_issues_poll,
            trigger=IntervalTrigger(minutes=poll_minutes),
            id=job_id,
            name=f"olano:{config.name}:issues",
            replace_existing=True,
        )
        logger.info(
            "Issue poll registered for agent '%s' (every %d min)", config.name, poll_minutes
        )
        return job_id

    def _add_gmail_watch_job(
        self, config: AgentConfig, runner: Any, workspace_root: Path
    ) -> str | None:
        """Register the agent's Gmail watch (``agent.json5 → gmail_watch``).

        Every ``poll_minutes`` the watch lists new mail matching the query
        and starts one agent turn for it (``olano/scheduler/gmail_watch.py``).
        The turn's root source is ``gmail``, not ``scheduled``: the email is
        a stranger's text, so a gated tool call waits in the approvals queue
        instead of being approved the way a scheduled job's is.
        """
        job_id = f"{config.name}_gmail_watch"

        async def _deliver(message: str) -> None:
            from olano.security.request_context import (
                ORIGIN_KEY,
                make_origin,
                push_request_context,
            )

            # ``scheduled_`` prefix: the dashboard files the run with the other
            # scheduler firings instead of passing it off as a human chat.
            thread_id = fresh_run_thread_id(f"scheduled_gmail_{config.name}")
            # Not the owner, not authenticated: the turn's input is an email.
            # ``is_owner`` defaults to True on a RequestContext, which would
            # open owner-only tools to whoever can send this mailbox a message.
            with push_request_context(
                source="gmail",
                channel="gmail",
                session_id=thread_id,
                authenticated=False,
                is_owner=False,
                metadata={ORIGIN_KEY: make_origin("gmail", False, root_session_id=thread_id)},
            ):
                await runner.ainvoke(message, thread_id=thread_id)

        async def _run_gmail_watch() -> None:
            from olano.scheduler import gmail_watch
            from olano.tools._credentials import agent_runtime_context

            try:
                with agent_runtime_context(config.name, config.env):
                    await gmail_watch.run_check(config, workspace_root, _deliver)
            except Exception as exc:
                logger.error("Gmail watch failed for '%s': %s", config.name, exc)

        self._scheduler.add_job(
            _run_gmail_watch,
            trigger=IntervalTrigger(minutes=config.gmail_watch.poll_minutes),
            id=job_id,
            name=f"olano:{config.name}:gmail_watch",
            replace_existing=True,
        )
        logger.info(
            "Gmail watch registered for agent '%s' (every %d min)",
            config.name,
            config.gmail_watch.poll_minutes,
        )
        return job_id

    def _add_heartbeat_job(
        self,
        config: AgentConfig,
        runner: Any,
        workspace_root: Path,
    ) -> str | None:
        """Register a recurring heartbeat job that polls HEARTBEAT.md and any
        skill-level HEARTBEAT.md files found under ``skills/``.

        Cadence and on/off come from ``agent.json5 → heartbeat`` with home-wide
        defaults (``config.yaml → heartbeat:``, hourly). The poll closure reads
        the live settings dict on every tick, so ``set_heartbeat_options()``
        (tools / dashboard) applies without re-registering the agent.
        """
        from olano.config.system_settings import (
            effective_heartbeat_enabled,
            effective_heartbeat_interval,
        )

        job_id = f"{config.name}_heartbeat"
        state_path = workspace_root / "memories" / "heartbeat-state.json"
        workspace_heartbeat = workspace_root / "HEARTBEAT.md"
        interval_minutes = effective_heartbeat_interval(config)
        self._heartbeat_settings[config.name] = {
            "enabled": effective_heartbeat_enabled(config),
            "interval_minutes": interval_minutes,
        }

        async def _run_one_task(
            task: dict,
            task_key: str,
            state: dict,
            now: datetime,
            interval_minutes: int,
            *,
            skill_context: str = "",
        ) -> None:
            when = task.get("when", "always")
            if when != "always" and not _cron_matches(when, now, interval_minutes):
                return
            last_run = state.get(task_key)
            if last_run and not _should_rerun(last_run, when, now, interval_minutes):
                return

            message = f"HEARTBEAT: {task_key}"
            if task.get("notes"):
                message += f"\n\nContext: {task['notes']}"
            if skill_context:
                message += f"\n\nSkill instructions:\n{skill_context[:1200]}"
            # The heartbeat is autonomous too: a `when:` rule was decided from
            # the clock, so hand the agent that clock rather than letting it
            # infer the day from its files.
            message += f"\n\n{clock.run_stamp(clock.display_timezone(), now=now)}"

            thread_id = fresh_run_thread_id(f"heartbeat_{config.name}")
            try:
                from olano.security.request_context import (
                    ORIGIN_KEY,
                    make_origin,
                    push_request_context,
                )

                # Heartbeat is autonomous: mark it scheduled so HITL resolves to
                # auto-approve. Previously it set no context → inherited "local"
                # → gated tools were auto-rejected.
                with push_request_context(
                    source="scheduled",
                    channel="scheduler",
                    session_id=thread_id,
                    authenticated=True,
                    metadata={
                        ORIGIN_KEY: make_origin("scheduled", False, root_session_id=thread_id)
                    },
                ):
                    response = await self._bounded(
                        runner.ainvoke(message, thread_id=thread_id), label=task_key
                    )
                state[task_key] = now.isoformat()
                logger.info("Heartbeat task '%s' completed: %r", task_key, str(response)[:100])
                if response and str(response).strip() and str(response).strip() != "HEARTBEAT_OK":
                    try:
                        from olano.pulse.generators import PulseGenerator

                        PulseGenerator(config, workspace_root).capture_heartbeat_findings(
                            [
                                {
                                    "task": task_key,
                                    "content": str(response).strip(),
                                    "priority": "normal",
                                }
                            ]
                        )
                    except Exception as pulse_exc:
                        logger.debug("Heartbeat PULSE skipped for '%s': %s", task_key, pulse_exc)
            except Exception as e:
                logger.error("Heartbeat task '%s' failed: %s", task_key, e)

        def _gather_heartbeat_tasks() -> tuple[
            dict[str, str], list[dict[str, str]], list[tuple[str, list[dict[str, str]], str]]
        ]:
            """State + parsed workspace/skill heartbeat tasks, in one pass.

            Everything here is file I/O (state read, skills-tree walk, HEARTBEAT.md
            parsing) — the poll runs it in a thread so the runtime loop only ever
            executes the awaits on the agent runs themselves."""
            state = _load_state(state_path)
            workspace_tasks = (
                _parse_heartbeat(workspace_heartbeat) if workspace_heartbeat.exists() else []
            )
            skill_tasks = [
                (slug, _parse_heartbeat(hb_path), skill_body)
                for slug, hb_path, skill_body in OlanoScheduler._collect_skill_heartbeats(
                    workspace_root
                )
            ]
            return state, workspace_tasks, skill_tasks

        async def _run_heartbeat() -> None:
            settings = self._heartbeat_settings.get(config.name) or {}
            if not settings.get("enabled", True):
                logger.debug("Heartbeat: disabled for '%s' — skipping poll", config.name)
                return
            interval_minutes = int(
                settings.get("interval_minutes") or DEFAULT_HEARTBEAT_INTERVAL_MINUTES
            )

            state, workspace_tasks, skill_tasks = await asyncio.to_thread(_gather_heartbeat_tasks)
            now = datetime.now(timezone.utc)
            any_tasks = False

            # 1. Workspace-level HEARTBEAT.md (original behaviour).
            for task in workspace_tasks:
                task_key = task.get("task", "")[:50]
                if not task_key:
                    continue
                any_tasks = True
                await _run_one_task(task, task_key, state, now, interval_minutes)

            # 2. Skill-level HEARTBEAT.md files — tasks are namespaced by skill slug
            #    so they never clash with workspace-level task keys.
            for slug, tasks, skill_body in skill_tasks:
                for task in tasks:
                    raw_key = task.get("task", "")[:50]
                    if not raw_key:
                        continue
                    # Prefix with skill slug: "[get-crypto-price] fetch btc"
                    task_key = f"[{slug}] {raw_key}"
                    any_tasks = True
                    await _run_one_task(
                        task, task_key, state, now, interval_minutes, skill_context=skill_body
                    )

            if not any_tasks:
                logger.debug("Heartbeat: no tasks found in workspace or skill HEARTBEAT.md files")

            await asyncio.to_thread(_save_state, state_path, state)

        self._scheduler.add_job(
            _run_heartbeat,
            trigger=_heartbeat_trigger(interval_minutes),
            id=job_id,
            name=f"olano:{config.name}:heartbeat",
            replace_existing=True,
        )
        logger.info(
            "Heartbeat job registered for agent '%s' (every %d min, %s)",
            config.name,
            interval_minutes,
            "enabled" if self._heartbeat_settings[config.name]["enabled"] else "disabled",
        )
        return job_id

    def set_heartbeat_options(
        self,
        agent_name: str,
        *,
        enabled: bool | None = None,
        interval_minutes: int | None = None,
    ) -> tuple[bool, str]:
        """Live-apply heartbeat settings for a registered agent (no restart).

        Updates the settings the poll closure reads on every tick and, when the
        interval changes (or the poll is re-enabled), reschedules the job so the
        next poll lands within ~5 minutes. Persisting the change to agent.json5
        is the caller's job (heartbeat_configure tool / dashboard PUT).
        """
        from olano.config.system_settings import clamp_heartbeat_interval

        with self._lock:
            settings = self._heartbeat_settings.get(agent_name)
            if settings is None:
                return False, (
                    f"Agent '{agent_name}' has no heartbeat poll registered in this runtime."
                )
            if enabled is not None:
                settings["enabled"] = bool(enabled)
            if interval_minutes is not None:
                settings["interval_minutes"] = clamp_heartbeat_interval(interval_minutes)
            if interval_minutes is not None or enabled is True:
                try:
                    self._scheduler.reschedule_job(
                        f"{agent_name}_heartbeat",
                        trigger=_heartbeat_trigger(int(settings["interval_minutes"])),
                    )
                except Exception as exc:
                    logger.debug("Heartbeat reschedule skipped for '%s': %s", agent_name, exc)
            state = "enabled" if settings["enabled"] else "disabled"
            return True, (
                f"Heartbeat for '{agent_name}' is {state}, polling every "
                f"{settings['interval_minutes']} min."
            )

    # ----------------------------------------------------------------- cognition jobs

    def _add_cognition_jobs(
        self,
        config: AgentConfig,
        runner: Any,
        workspace_root: Path,
    ) -> list[str]:
        """Register the autonomous Cortex engine's cron jobs — one per enabled phase.

        Driven entirely by the CortexEngine phase registry
        (:data:`olano.cognition.orchestrator.PHASES`) so there is no second
        list to maintain. Gating:

        - the home-wide + per-agent **autonomy** switch (else manual-only);
        - each phase's **feature** toggle (``feature_enabled``);
        - the legacy per-worker ``enable_*`` flags (so existing opt-outs still work);

        and the cadence comes from the effective **intensity** preset (a per-agent
        cron field on ``cognition`` overrides it where one exists). Each job runs
        the phase through :func:`olano.cognition.orchestrator.run_phase`, which
        enforces budget + the daily deep-run cap and surfaces results (undo pulse /
        approval / activity log).
        """
        from olano.cognition.orchestrator import PHASES, run_phase
        from olano.config.system_settings import (
            autonomy_enabled,
            effective_intensity,
            feature_enabled,
        )

        cog = config.cognition
        job_ids: list[str] = []

        # When autonomy is off, register nothing — cognition is manual-only (the
        # dashboard "Think" / API still work on demand).
        if not autonomy_enabled(config):
            logger.info("Cortex: autonomy disabled for '%s' — no background jobs", config.name)
            return job_ids

        intensity = effective_intensity(config)

        def _cron(expr: str) -> CronTrigger:
            parts = expr.strip().split()
            if len(parts) == 5:
                minute, hour, day, month, dow = parts
                return CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=clock.standard_weekdays(dow),
                )
            return CronTrigger.from_crontab(expr)

        # Legacy per-worker enable flags (back-compat).
        _ENABLE_FLAG = {
            "memory_garden": "enable_memory_gardener",
            "pulse": "enable_pulse_agent",
            "workspace_optimize": "enable_workspace_optimizer",
            "skill_librarian": "enable_skill_librarian",
        }

        def _make_job(phase_id: str, attempt: int = 0):
            async def _job() -> None:
                try:
                    from olano.cognition.orchestrator import run_phase as _run_phase

                    result = await _run_phase(phase_id, config, workspace_root, source="autonomous")
                    logger.info("Cortex[%s] %s → %s", config.name, phase_id, result.get("status"))
                    self._maybe_schedule_cognition_retry(
                        config, phase_id, result, attempt=attempt, make_job=_make_job
                    )
                except Exception as e:
                    logger.error("Cortex[%s] phase %s failed: %s", config.name, phase_id, e)

            return _job

        # Touch run_phase at import time so a broken registry fails loudly here.
        assert callable(run_phase)

        for phase in PHASES:
            if not feature_enabled(phase.feature, config):
                continue
            flag = _ENABLE_FLAG.get(phase.id)
            if flag and getattr(cog, flag, True) is False:
                continue

            # An explicit per-agent cron is honoured verbatim. Otherwise the
            # intensity preset's cadence applies, staggered per agent+phase so
            # the fleet does not fire in lockstep. (These four fields shipped
            # as concrete strings once, which made this `or` unreachable —
            # see CognitionConfig.)
            cron_str = phase_cron_for(config, phase, intensity)

            job_id = f"{config.name}_cortex_{phase.id}"
            self._scheduler.add_job(
                _make_job(phase.id),
                trigger=_cron(cron_str),
                id=job_id,
                name=f"olano:{config.name}:cortex:{phase.id}",
                replace_existing=True,
            )
            job_ids.append(job_id)

        if job_ids:
            logger.info(
                "Cortex: registered %d autonomous phase(s) for '%s' (intensity=%s)",
                len(job_ids),
                config.name,
                intensity,
            )
        return job_ids

    def _maybe_schedule_cognition_retry(
        self,
        config: AgentConfig,
        phase_id: str,
        result: dict[str, Any],
        *,
        attempt: int,
        make_job: Any,
    ) -> bool:
        """Re-fire a phase later when its failure is one that heals with time.

        A rate limit or an overloaded provider used to cost the whole cadence
        period — one 429 at 04:30 and memory gardening was dead until tomorrow,
        with nothing in the process retrying above the SDK's seconds-apart
        transport attempts. The retry is a fresh one-shot ``DateTrigger`` job
        running the same phase with a **new** worker (never a resumed graph),
        after a minute-scale backoff (``reliability.retry_delays``) with ±25%
        jitter so a fleet does not re-converge on the same second. Skipped when
        the phase's regular firing is due sooner than the backoff — the cron is
        about to do the same work. Returns True when a retry was registered.
        """
        if result.get("status") != "error" or not is_retryable_code(result.get("code")):
            return False
        delays = retry_delays()
        if attempt >= len(delays):
            logger.warning(
                "Cortex[%s] %s still failing (%s) after %d attempt(s); giving up until the "
                "next scheduled run",
                config.name,
                phase_id,
                result.get("code"),
                attempt + 1,
            )
            return False
        delay = jittered(delays[attempt])
        run_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        regular = self._scheduler.get_job(f"{config.name}_cortex_{phase_id}")
        next_regular = getattr(regular, "next_run_time", None)
        if next_regular is not None and next_regular <= run_at:
            logger.info(
                "Cortex[%s] %s failed transiently (%s); its regular run at %s comes first",
                config.name,
                phase_id,
                result.get("code"),
                next_regular.isoformat(timespec="minutes"),
            )
            return False
        retry_id = _cognition_retry_job_id(config.name, phase_id)
        self._scheduler.add_job(
            make_job(phase_id, attempt + 1),
            trigger=DateTrigger(run_date=run_at),
            id=retry_id,
            name=f"olano:{config.name}:cortex:{phase_id}:retry",
            replace_existing=True,
        )
        logger.warning(
            "Cortex[%s] %s failed transiently (%s); retry %d/%d in %d min",
            config.name,
            phase_id,
            result.get("code"),
            attempt + 1,
            len(delays),
            round(delay / 60),
        )
        return True

    def _remove_cognition_retries(self, agent_name: str) -> None:
        """Drop any pending phase retries for an agent (unregister / resync)."""
        suffix = "_retry"
        prefix = f"{agent_name}_cortex_"
        try:
            jobs = self._scheduler.get_jobs()
        except Exception:
            return
        for job in jobs:
            job_id = getattr(job, "id", "")
            if job_id.startswith(prefix) and job_id.endswith(suffix):
                try:
                    self._scheduler.remove_job(job_id)
                except Exception:
                    pass

    # ----------------------------------------------------------------- regular jobs

    def _register_config_schedule(self, config: AgentConfig, runner: Any) -> list[str]:
        """(Re)build the config-schedule jobs for an agent from its `schedule` list.

        Resets ``self._config_jobs[agent]`` to a descriptor per entry (so the
        dashboard can enumerate them) and registers a live APScheduler job for
        each *enabled* entry. A disabled entry keeps its descriptor — so the UI
        can show and resume it — but fires nothing. Returns the live job ids.
        """
        descriptors: list[dict[str, Any]] = []
        job_ids: list[str] = []
        has_fan_out = False
        for index, sched in enumerate(config.schedule):
            enabled = bool(getattr(sched, "enabled", True))
            fan_out = bool(getattr(sched, "for_each_customer", False))
            has_fan_out = has_fan_out or fan_out
            if enabled:
                if fan_out:
                    # Multi-user Type-1 job: fires once per customer folder.
                    # Registered beside — never through — the classic path, so
                    # a normal entry's dispatch is untouched by construction.
                    job_ids.append(self._add_fan_out_job(config, sched, runner, index=index))
                else:
                    job_ids.append(self._add_job(config.name, sched, runner, index=index))
            descriptors.append(self._describe_config_job(config.name, index, sched))
        self._config_jobs[config.name] = descriptors
        # Keep (or drop) the config reference Run-now needs for fan-out entries.
        if has_fan_out:
            self._multi_user_configs[config.name] = config
        else:
            self._multi_user_configs.pop(config.name, None)
        return job_ids

    async def _run_config_message(
        self,
        job_id: str,
        agent_name: str,
        message: str,
        thread_id: str,
        runner: Any,
        *,
        override_model: str | None = None,
        override_reasoning_effort: str | None = None,
        tz_name: str | None = None,
        due_at: datetime | None = None,
    ) -> None:
        """Execute one config-schedule job's message and record the run.

        Shared by the cron fire (`_add_job`) and Run-now (`run_config_job_now`)
        so both dispatch identically — autonomous `scheduled` origin (HITL
        auto-approves) and a history row under the synthetic cfg:… id.
        ``override_model`` / ``override_reasoning_effort`` are the entry's own
        ``model`` / ``reasoning_effort`` (None = the agent's). The message the
        agent receives opens with the autonomous directive and the current
        date and time in ``tz_name`` (the entry's timezone), plus how late the
        run is when ``due_at`` is behind the clock."""
        logger.info(
            "Scheduled job '%s' firing: agent=%s model=%s effort=%s message=%r",
            job_id,
            agent_name,
            override_model or "default",
            override_reasoning_effort or "default",
            message,
        )
        try:
            from olano.security.request_context import (
                ORIGIN_KEY,
                make_origin,
                push_request_context,
            )

            # Config-cron jobs are autonomous: mark scheduled so HITL
            # auto-approves. Previously set no context → "local" → rejected.
            with push_request_context(
                source="scheduled",
                channel="scheduler",
                session_id=thread_id,
                authenticated=True,
                metadata={ORIGIN_KEY: make_origin("scheduled", False, root_session_id=thread_id)},
            ):
                # Passed only when the entry pinned a model (see _execute_db_task).
                response = await self._bounded(
                    runner.ainvoke(
                        clock.autonomous_preamble(message, tz_name=tz_name, due_at=due_at),
                        thread_id=thread_id,
                        **({"override_model": override_model} if override_model else {}),
                        **(
                            {"override_reasoning_effort": override_reasoning_effort}
                            if override_reasoning_effort
                            else {}
                        ),
                    ),
                    label=job_id,
                )
            logger.info("Scheduled job '%s' completed: %r", job_id, str(response)[:100])
            # Record the run under the synthetic id so the dashboard shows real
            # run-count / last-run / history for config jobs too — the same
            # task_history table DB tasks use.
            _record_config_run(job_id, result=(str(response)[:2000] or "completed"))
            _resolve_run_alert(job_id)
        except Exception as e:
            logger.error("Scheduled job '%s' failed: %s", job_id, e)
            _record_config_run(job_id, error=str(e))
            _notify_run_problem(
                job_id, agent=agent_name, name=job_id, outcome="failed", detail=str(e)
            )

    def _add_job(
        self,
        agent_name: str,
        sched: ScheduleConfig,
        runner: Any,
        *,
        index: int,
    ) -> str:
        job_id = _config_job_id(agent_name, index)
        trigger = self._make_trigger(sched)
        pinned_thread_id = sched.thread_id
        message = sched.message
        override_model = (getattr(sched, "model", None) or "").strip() or None
        override_effort = (getattr(sched, "reasoning_effort", None) or "").strip() or None

        tz_name = (getattr(sched, "timezone", None) or "").strip() or None

        async def _run() -> None:
            # Resolved per firing, not once at registration: an explicit
            # sched.thread_id is opt-in memory continuity and stays pinned, but
            # the default gets a fresh thread each run (see fresh_run_thread_id).
            thread_id = pinned_thread_id or fresh_run_thread_id(f"scheduled_{agent_name}")
            await self._run_config_message(
                job_id,
                agent_name,
                message,
                thread_id,
                runner,
                tz_name=tz_name,
                due_at=self._due_fire_for(trigger),
                override_model=override_model,
                override_reasoning_effort=override_effort,
            )

        self._scheduler.add_job(
            _run,
            trigger=trigger,
            id=job_id,
            name=f"olano:{agent_name}",
            replace_existing=True,
            misfire_grace_time=int(self._task_grace().total_seconds()),
        )
        return job_id

    def _add_fan_out_job(
        self,
        config: AgentConfig,
        sched: ScheduleConfig,
        runner: Any,
        *,
        index: int,
    ) -> str:
        """Register one multi-user `for_each_customer` schedule entry.

        The firing enumerates the agent's ``customers/`` folders and runs the
        entry once per customer (action script or bound agent turn — see
        ``scheduler/multi_user.run_for_each_customer``). History lands under
        the same synthetic ``cfg:…`` id as classic config jobs, so the
        dashboard's run-count / last-run keep working unchanged.
        """
        job_id = _config_job_id(config.name, index)
        trigger = self._make_trigger(sched)

        async def _run() -> None:
            from olano.scheduler.multi_user import run_for_each_customer

            logger.info(
                "Scheduled fan-out job '%s' firing: agent=%s action=%r",
                job_id,
                config.name,
                sched.action,
            )
            try:
                summary = await run_for_each_customer(job_id, config, sched, runner)
                _record_config_run(job_id, result=summary)
            except Exception as e:
                logger.error("Scheduled fan-out job '%s' failed: %s", job_id, e)
                _record_config_run(job_id, error=str(e))

        self._scheduler.add_job(
            _run,
            trigger=trigger,
            id=job_id,
            name=f"olano:{config.name}",
            replace_existing=True,
            misfire_grace_time=int(self._task_grace().total_seconds()),
        )
        return job_id

    def _describe_config_job(
        self, agent_name: str, index: int, sched: ScheduleConfig
    ) -> dict[str, Any]:
        """Neutral descriptor for one agent.json5 `schedule` entry.

        Kept UI-agnostic; the API layer maps it into the Scheduler view's Task
        shape (adding source tag, history-derived run stats, live next_run)."""
        return {
            "id": _config_job_id(agent_name, index),
            "index": index,
            "agent": agent_name,
            "name": (getattr(sched, "name", None) or "").strip() or None,
            "message": sched.message,
            "cron": self._cron_string(sched),
            "timezone": sched.timezone,
            "thread_id": sched.thread_id,
            "enabled": bool(getattr(sched, "enabled", True)),
            # The entry's own model ("" = the agent's configured one).
            "model": (getattr(sched, "model", None) or "").strip(),
            # The entry's own effort level ("" = the agent's setting).
            "reasoning_effort": (getattr(sched, "reasoning_effort", None) or "").strip().lower(),
            # Multi-user Type-1 markers (False/None on every classic entry) so
            # the dashboard can label a fan-out job for what it is.
            "for_each_customer": bool(getattr(sched, "for_each_customer", False)),
            "action": getattr(sched, "action", None),
        }

    @staticmethod
    def _cron_string(sched: ScheduleConfig) -> str:
        """Canonical 5-field cron string for a schedule entry.

        `cron` (when a valid 5-field expression) wins over the individual
        minute/hour/… fields — mirroring _make_trigger — so the value shown in
        the dashboard is exactly what actually fires."""
        if sched.cron and len(sched.cron.strip().split()) == 5:
            return sched.cron.strip()
        return f"{sched.minute} {sched.hour} {sched.day} {sched.month} {sched.day_of_week}"

    def _next_run_for(self, job_id: str) -> str | None:
        try:
            job = self._scheduler.get_job(job_id)
        except Exception:
            return None
        next_run = getattr(job, "next_run_time", None) if job else None
        return str(next_run) if next_run is not None else None

    def list_config_jobs(self, agent_name: str | None = None) -> list[dict[str, Any]]:
        """Enumerate config-schedule jobs (all agents, or one) with live next_run.

        This is the read side the dashboard uses to surface agent.json5 schedule
        entries alongside DB tasks — the jobs that were previously invisible."""
        with self._lock:
            agents = [agent_name] if agent_name else list(self._config_jobs.keys())
            out: list[dict[str, Any]] = []
            for a in agents:
                for descriptor in self._config_jobs.get(a, []):
                    item = dict(descriptor)
                    item["next_run"] = (
                        self._next_run_for(descriptor["id"]) if descriptor.get("enabled") else None
                    )
                    out.append(item)
            return out

    def resync_agent_gmail_watch(self, config: AgentConfig) -> tuple[bool, str]:
        """Live-apply an agent's edited ``gmail_watch`` without a restart.

        Drops the agent's current watch job and, when the new settings enable
        it, registers one with the new interval. Nothing else of the agent is
        touched. The watch's state file is kept, so turning it off and on again
        does not replay mail it already delivered.
        """
        job_id = f"{config.name}_gmail_watch"
        with self._lock:
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass
            if config.name in self._jobs:
                self._jobs[config.name] = [j for j in self._jobs[config.name] if j != job_id]
            runner = self._runners.get(config.name)
            root = self._workspace_roots.get(config.name)
            if not config.gmail_watch.enabled:
                return True, f"Gmail watch off for '{config.name}'."
            if runner is None or root is None:
                return False, f"Agent '{config.name}' is not active; it starts at boot."
            added = self._add_gmail_watch_job(config, runner, root)
            if added:
                self._jobs.setdefault(config.name, []).append(added)
            return True, f"Gmail watch on for '{config.name}'."

    def resync_agent_config_schedule(self, config: AgentConfig) -> tuple[bool, str]:
        """Live-apply an agent's edited `schedule` without a restart.

        Removes the agent's current config-schedule jobs and rebuilds them from
        ``config.schedule`` — leaving heartbeat / cognition jobs untouched.
        Called after the dashboard writes agent.json5 (Scheduler edit/delete or
        the agent Config editor). If the agent isn't booted in this runtime, the
        descriptors are refreshed so a later read/boot reflects the new config.
        """
        with self._lock:
            old_ids = {d["id"] for d in self._config_jobs.get(config.name, [])}
            for job_id in old_ids:
                try:
                    self._scheduler.remove_job(job_id)
                except Exception:
                    pass
            if config.name in self._jobs:
                self._jobs[config.name] = [j for j in self._jobs[config.name] if j not in old_ids]

            runner = self._runners.get(config.name)
            if runner is None:
                self._config_jobs[config.name] = [
                    self._describe_config_job(config.name, i, s)
                    for i, s in enumerate(config.schedule)
                ]
                return False, f"Agent '{config.name}' is not active; schedule saved to config."

            new_ids = self._register_config_schedule(config, runner)
            self._jobs.setdefault(config.name, []).extend(new_ids)
            return True, f"Re-registered {len(new_ids)} config schedule job(s) for '{config.name}'."

    def resync_agent_cognition_jobs(self, config: AgentConfig) -> tuple[bool, str]:
        """Live-apply an agent's edited Cortex ``cognition`` block without a restart.

        Removes the agent's current cognition cron jobs (``{name}_cortex_*``) and
        rebuilds them from ``config.cognition`` — leaving config-schedule and
        heartbeat jobs untouched. Called after the dashboard writes agent.json5
        (the Overview Cortex toggle or the Autonomy panel's per-agent scope). When
        Cortex is disabled for the agent — or the agent isn't booted in this
        runtime — the jobs are simply removed. ``_add_cognition_jobs`` re-applies
        the autonomy / per-feature gating, so exactly the right subset is
        registered. The agent-facing ``cortex_*`` chat tools are baked into the
        built graph and are unaffected until the agent is rebuilt/restarted.
        """
        with self._lock:
            prefix = f"{config.name}_cortex_"
            existing = [j for j in self._jobs.get(config.name, []) if j.startswith(prefix)]
            for job_id in existing:
                try:
                    self._scheduler.remove_job(job_id)
                except Exception:
                    pass
            if config.name in self._jobs:
                self._jobs[config.name] = [
                    j for j in self._jobs[config.name] if not j.startswith(prefix)
                ]
            self._remove_cognition_retries(config.name)

            runner = self._runners.get(config.name)
            workspace_root = self._workspace_roots.get(config.name)
            if runner is None or workspace_root is None:
                return False, f"Agent '{config.name}' is not active; cognition config saved."

            # Same resolver as the registration path above — a live cognition
            # save must reach the same verdict the boot path would.
            if not cortex_enabled(config):
                return True, f"Cortex disabled for '{config.name}'; removed {len(existing)} job(s)."

            new_ids = self._add_cognition_jobs(config, runner, workspace_root)
            self._jobs.setdefault(config.name, []).extend(new_ids)
            return True, f"Re-registered {len(new_ids)} cognition job(s) for '{config.name}'."

    @staticmethod
    def _make_trigger(sched: ScheduleConfig) -> CronTrigger:
        # An explicit IANA timezone (e.g. "America/Costa_Rica") pins the cron
        # fields to that wall clock regardless of the runtime's host timezone.
        # None lets APScheduler fall back to its default (server local time).
        tz: Any = None
        if sched.timezone:
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(sched.timezone)
            except Exception:
                logger.warning(
                    "Unknown schedule timezone %r; falling back to server local time.",
                    sched.timezone,
                )
        extra = {"timezone": tz} if tz is not None else {}
        if sched.cron:
            parts = sched.cron.strip().split()
            if len(parts) == 5:
                minute, hour, day, month, day_of_week = parts
                return CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=clock.standard_weekdays(day_of_week),
                    **extra,
                )
        return CronTrigger(
            minute=sched.minute,
            hour=sched.hour,
            day=sched.day,
            month=sched.month,
            day_of_week=clock.standard_weekdays(sched.day_of_week),
            **extra,
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        jobs = []
        for job in self._scheduler.get_jobs():
            # A job added before the scheduler is started is still "pending" and
            # APScheduler 3.x hasn't computed its next_run_time yet — accessing
            # the attribute then raises AttributeError. list_jobs() is called on
            # the hot /api/status path during boot (agents register jobs before
            # scheduler.start()), so guard it or the readiness probe sees HTTP
            # 500s until boot finishes. Mirrors the getattr guard in api.py.
            next_run = getattr(job, "next_run_time", None)
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": str(next_run) if next_run is not None else None,
                }
            )
        return jobs


# ----------------------------------------------------------------- helpers


def _heartbeat_trigger(interval_minutes: int) -> IntervalTrigger:
    """Interval trigger for the heartbeat poll.

    The first poll fires within ``_HEARTBEAT_FIRST_POLL_MINUTES`` of
    (re)registration — not a full interval away — so a long cadence (e.g. daily)
    still gets a check shortly after boot or a settings change; subsequent polls
    run every ``interval_minutes``.
    """
    first = datetime.now(timezone.utc) + timedelta(
        minutes=min(interval_minutes, _HEARTBEAT_FIRST_POLL_MINUTES)
    )
    return IntervalTrigger(minutes=interval_minutes, start_date=first)


def _parse_heartbeat(path: Path) -> list[dict[str, str]]:
    """Parse HEARTBEAT.md for task entries (shared parser — see scheduler/heartbeat.py)."""
    from olano.scheduler.heartbeat import parse_heartbeat_file

    return parse_heartbeat_file(path)


def _load_state(path: Path) -> dict[str, str]:
    if path.exists():
        try:
            data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
            return data
        except Exception:
            pass
    return {}


def _save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _cron_matches(cron_expr: str, now: datetime, lookback_minutes: int = 5) -> bool:
    """Quick check if a cron expression would have fired within the last poll
    window (``lookback_minutes`` — the heartbeat interval, so no fire time falls
    between two polls)."""
    try:
        from apscheduler.triggers.cron import CronTrigger

        parts = cron_expr.strip().split()
        if len(parts) == 5:
            minute, hour, day, month, day_of_week = parts
        else:
            return True  # Can't parse, allow

        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=clock.standard_weekdays(day_of_week),
        )
        # Get next fire time from one poll interval ago
        prev = now - timedelta(minutes=lookback_minutes)
        next_fire = trigger.get_next_fire_time(None, prev)
        return next_fire is not None and next_fire <= now
    except Exception:
        return True


def _should_rerun(last_run_iso: str, when: str, now: datetime, interval_minutes: int = 5) -> bool:
    """Determine if a task should re-run based on its schedule and last run time."""
    try:
        last = datetime.fromisoformat(last_run_iso)
        diff = now - last
        if when == "always":
            # Cooldown just under the poll interval so scheduler jitter between
            # consecutive polls can't make an "always" task skip a tick.
            return diff.total_seconds() > max(30, interval_minutes * 60 - 30)
        return diff.total_seconds() > 60  # ran less than a minute ago, skip
    except Exception:
        return True


def _db_job_id(task_id: str) -> str:
    return f"db_{task_id[:8]}"


def _config_job_id(agent_name: str, index: int) -> str:
    """Stable id for a config-schedule job — the Nth entry in an agent's
    agent.json5 `schedule`. The ``cfg:`` prefix lets the API route edits back to
    the file (vs a DB task) and the index maps straight to the array slot."""
    return f"cfg:{agent_name}:{index}"


def _record_config_run(job_id: str, *, result: str | None = None, error: str | None = None) -> None:
    """Best-effort: append a config-job run to the shared task_history table so
    the dashboard shows run-count / last-run / history for config jobs, exactly
    like DB tasks. The synthetic ``cfg:…`` id has no `tasks` row, so add_history's
    UPDATE is a harmless no-op and only the history row lands."""
    try:
        from olano.scheduler.db import SchedulerDB

        SchedulerDB().add_history(job_id, result=result, error=error)
    except Exception as exc:
        logger.debug("Config-job history record skipped for %s: %s", job_id, exc)


def _config_job_agent(job_id: str) -> str | None:
    """The agent a ``cfg:<agent>:<index>`` id belongs to, else None."""
    parts = job_id.split(":")
    return parts[1] if len(parts) == 3 and parts[0] == "cfg" and parts[1] else None


def _run_alert_key(job_id: str) -> str:
    return f"scheduled_run:{job_id}"


def _notify_run_problem(
    job_id: str, *, agent: str | None, name: str, outcome: str, detail: str
) -> None:
    """Put a skipped or failed scheduled run on the bell.

    A dropped firing used to leave a history row and nothing else, so the
    person who wrote "every weekday at 08:00" learned a run was missing when
    they went looking for its output. One row per job (``dedupe_key``), so a
    task that fails on every tick does not flood, and ``_resolve_run_alert``
    clears it on the next run that completes. Never raises.
    """
    try:
        from olano.runtime import notifications

        verb = "was skipped" if outcome == "skipped" else "failed"
        notifications.notify(
            kind="scheduled_run",
            level="warn",
            title=f"Scheduled task '{name}' {verb}",
            body=detail[:600],
            agent=agent,
            dedupe_key=_run_alert_key(job_id),
            action={"label": "Open Scheduler", "href": f"/scheduler?task={job_id}"},
        )
    except Exception:
        logger.debug("Scheduled-run notification failed for %s", job_id, exc_info=True)


def _resolve_run_alert(job_id: str) -> None:
    try:
        from olano.runtime import notifications

        notifications.resolve(_run_alert_key(job_id))
    except Exception:
        logger.debug("Scheduled-run alert resolve failed for %s", job_id, exc_info=True)


def _notify_scheduler_db_unreadable(detail: str) -> None:
    try:
        from olano.runtime import notifications

        notifications.notify(
            kind="scheduler_db_unreadable",
            level="error",
            title="Scheduled tasks are not running",
            body=(
                "The scheduler database could not be read when the runtime started, so "
                f"no saved scheduled task is registered. Last error: {detail[:300]}. "
                "Saving any task again re-registers all of them; otherwise restart the runtime."
            ),
            dedupe_key="scheduler_db_unreadable",
            action={"label": "Open Scheduler", "href": "/scheduler"},
        )
    except Exception:
        logger.debug("Scheduler DB notification failed", exc_info=True)


def _log_run_now_failure(task_id: str) -> Any:
    """Done-callback for a run-now handed to the runtime loop from a thread.

    _execute_db_task records its own failures; this only catches the coroutine
    dying before it (cancellation at shutdown, a broken SchedulerDB, …) so a
    fire-and-forget run never disappears without a trace.
    """

    def _callback(future: Any) -> None:
        try:
            future.result()
        except (Exception, asyncio.CancelledError) as exc:
            logger.error("Run-now for task '%s' failed on the runtime loop: %s", task_id[:8], exc)

    return _callback


def get_active_scheduler() -> OlanoScheduler | None:
    return _ACTIVE_SCHEDULER
