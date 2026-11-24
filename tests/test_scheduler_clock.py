"""Autonomous runs know the date; a late firing runs late instead of never.

Both halves come from one morning on two managed boxes (2026-11-02):

* An agent's scheduled brief said "weekdays only". Nothing in its prompt said
  what day it was, so it read the newest dates in its own files, decided it
  was Saturday on a Monday, and logged a skip. Every autonomous prompt now
  opens with the weekday, date and time in the task's timezone, and every
  model request's system message ends with today's date.
* A daily task due at 08:00 did not run. APScheduler drops a firing more
  than ``misfire_grace_time`` late, the default here was five minutes, and a
  firing inside a restart was never recorded at all. Tasks now get hours of
  grace, a restart catches up a fire it covered, a load that cannot read the
  database retries and says so, and a skipped or failed run rings the bell.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from olano.scheduler import clock
from olano.scheduler.cron import (
    DB_LOAD_RETRIES,
    OlanoScheduler,
    _config_job_agent,
    _run_alert_key,
)

UTC = timezone.utc
MONDAY_0000Z = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)  # 08:00 Monday in Singapore


# ───────────────────────────────── clock helpers ─────────────────────────────


class TestStamp:
    def test_weekday_date_and_local_time_lead_utc_follows(self) -> None:
        stamp = clock.format_stamp(MONDAY_0000Z, "Asia/Singapore")
        assert stamp.startswith("Monday 2 November 2026, 08:00 Asia/Singapore (UTC+08:00)")
        assert stamp.endswith("00:00 UTC")

    def test_utc_task_is_not_stamped_twice(self) -> None:
        assert clock.format_stamp(MONDAY_0000Z, None) == "Monday 2 November 2026, 00:00 UTC"
        assert clock.format_stamp(MONDAY_0000Z, "UTC") == "Monday 2 November 2026, 00:00 UTC"

    def test_utc_date_is_spelled_out_when_it_differs_from_the_local_one(self) -> None:
        # 20:30 UTC Sunday is 04:30 Monday in Singapore: the one case where
        # "today" is ambiguous, so both dates are written in full.
        stamp = clock.format_stamp(datetime(2026, 11, 1, 20, 30, tzinfo=UTC), "Asia/Singapore")
        assert "Monday 2 November 2026, 04:30 Asia/Singapore" in stamp
        assert "Sunday 1 November 2026, 20:30 UTC" in stamp

    def test_unknown_timezone_falls_back_to_utc_instead_of_raising(self) -> None:
        assert clock.resolve_zone("Mars/Olympus")[0] == "UTC"
        assert "UTC" in clock.format_stamp(MONDAY_0000Z, "Mars/Olympus")

    def test_run_stamp_names_the_weekday_as_authoritative(self) -> None:
        text = clock.run_stamp("Asia/Singapore", now=MONDAY_0000Z)
        assert text.startswith("Current date and time: Monday 2 November 2026")
        assert "authoritative date and weekday" in text
        assert "get_current_time" in text
        assert "late because" not in text

    def test_run_stamp_says_how_late_a_run_is(self) -> None:
        text = clock.run_stamp(
            "Asia/Singapore", due_at=MONDAY_0000Z, now=MONDAY_0000Z + timedelta(minutes=40)
        )
        assert "was due at Monday 2 November 2026, 08:00 Asia/Singapore" in text
        assert "starting 40 minutes late" in text

    def test_run_stamp_ignores_loop_jitter(self) -> None:
        text = clock.run_stamp("UTC", due_at=MONDAY_0000Z, now=MONDAY_0000Z + timedelta(seconds=45))
        assert "late because" not in text

    def test_autonomous_preamble_shape(self) -> None:
        text = clock.autonomous_preamble("do it", tz_name="UTC", now=MONDAY_0000Z)
        head, _, body = text.partition("\n\n")
        assert head.startswith(clock.AUTONOMOUS_DIRECTIVE)
        assert "Current date and time: Monday 2 November 2026, 00:00 UTC." in head
        assert body == "do it"

    def test_date_section_is_day_granular(self) -> None:
        # No hour anywhere: the block sits at the end of a cached system
        # prompt and must change once a day, not once a minute.
        section = clock.date_section(MONDAY_0000Z + timedelta(hours=13, minutes=24), "UTC")
        assert section.startswith("## Current date\nToday is Monday 2 November 2026 (UTC).")
        assert "13:24" not in section
        assert "get_current_time" in section

    def test_date_section_names_the_local_date_when_it_differs(self) -> None:
        section = clock.date_section(datetime(2026, 11, 1, 20, 0, tzinfo=UTC), "Asia/Singapore")
        assert (
            "Today is Sunday 1 November 2026 (UTC); in Asia/Singapore it is Monday 2 November 2026."
            in section
        )
        same = clock.date_section(MONDAY_0000Z, "Asia/Singapore")
        assert "the same date in Asia/Singapore" in same

    def test_display_timezone_prefers_the_explicit_setting(self) -> None:
        assert clock.display_timezone(
            {"LIONCLAW_CLOCK_TIMEZONE": "Asia/Singapore", "TZ": "UTC"}
        ) == ("Asia/Singapore")
        assert clock.display_timezone({"TZ": "Europe/Zurich"}) == "Europe/Zurich"
        assert clock.display_timezone({}) is None


class TestMissedFire:
    weekdays_0800_sgt = CronTrigger(
        minute="0", hour="8", day_of_week="mon-fri", timezone="Asia/Singapore"
    )
    six_hours = timedelta(hours=6)

    def test_a_fire_inside_the_window_with_no_run_since_is_missed(self) -> None:
        due = clock.missed_fire(
            self.weekdays_0800_sgt,
            now=MONDAY_0000Z + timedelta(minutes=7),
            grace=self.six_hours,
            last_run="2026-10-30T00:31:00+00:00",
            created_at="2026-10-20T00:00:00+00:00",
        )
        assert due == MONDAY_0000Z

    def test_a_run_recorded_after_the_fire_covers_it(self) -> None:
        # A skipped or failed row counts: the history is the trace, and
        # re-running a failure would be a retry policy this is not.
        assert (
            clock.missed_fire(
                self.weekdays_0800_sgt,
                now=MONDAY_0000Z + timedelta(minutes=7),
                grace=self.six_hours,
                last_run="2026-11-02T00:00:30+00:00",
            )
            is None
        )

    def test_a_fire_older_than_the_grace_is_let_go(self) -> None:
        assert (
            clock.missed_fire(
                self.weekdays_0800_sgt, now=MONDAY_0000Z + timedelta(hours=7), grace=self.six_hours
            )
            is None
        )

    def test_a_task_created_after_the_fire_is_not_owed_it(self) -> None:
        assert (
            clock.missed_fire(
                self.weekdays_0800_sgt,
                now=MONDAY_0000Z + timedelta(minutes=7),
                grace=self.six_hours,
                created_at="2026-11-02T00:05:00+00:00",
            )
            is None
        )

    def test_the_weekend_is_not_a_fire(self) -> None:
        sunday = datetime(2026, 11, 1, 0, 30, tzinfo=UTC)
        assert clock.missed_fire(self.weekdays_0800_sgt, now=sunday, grace=self.six_hours) is None

    def test_one_time_task_in_the_window(self) -> None:
        once = DateTrigger(run_date=MONDAY_0000Z - timedelta(minutes=3))
        assert clock.missed_fire(once, now=MONDAY_0000Z, grace=self.six_hours) == (
            MONDAY_0000Z - timedelta(minutes=3)
        )
        assert (
            clock.missed_fire(once, now=MONDAY_0000Z + timedelta(hours=9), grace=self.six_hours)
            is None
        )

    def test_latest_due_fire_picks_the_newest_of_several(self) -> None:
        hourly = CronTrigger(minute="0")
        due = clock.latest_due_fire(
            hourly, now=MONDAY_0000Z + timedelta(hours=3, minutes=10), not_before=MONDAY_0000Z
        )
        assert due == MONDAY_0000Z + timedelta(hours=3)

    def test_a_trigger_that_cannot_answer_yields_none(self) -> None:
        broken = MagicMock()
        broken.run_date = None
        broken.get_next_fire_time.side_effect = RuntimeError("no")
        assert clock.latest_due_fire(broken, now=MONDAY_0000Z, not_before=MONDAY_0000Z) is None


# ───────────────────────────────── scheduler ─────────────────────────────────


def _runner(reply: str = "done") -> MagicMock:
    runner = MagicMock()
    runner.ainvoke = AsyncMock(return_value=reply)
    return runner


@pytest.fixture
def db(tmp_path: Path):
    from olano.scheduler.db import SchedulerDB

    return SchedulerDB(db_path=tmp_path / "sched.db")


class TestAutonomousRunsKnowTheDate:
    async def test_db_task_prompt_opens_with_the_directive_and_the_stamp(self, db: Any) -> None:
        task_id = db.create_task(
            "leads",
            "find leads",
            schedule="0 8 * * 1-5",
            agent_name="anna",
            timezone="Asia/Singapore",
        )
        runner = _runner()
        sched = OlanoScheduler()
        sched._runners["anna"] = runner

        await sched._execute_db_task(db.get_task(task_id), db, manual=True)

        sent = runner.ainvoke.call_args.args[0]
        assert sent.startswith(clock.AUTONOMOUS_DIRECTIVE)
        assert "Current date and time: " in sent
        assert "Asia/Singapore" in sent
        assert sent.endswith("\n\nfind leads")

    async def test_run_now_is_never_called_late(self, db: Any) -> None:
        # A dashboard "Run now" is on demand, not a late firing, however long
        # ago the schedule's last fire time was.
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="anna")
        runner = _runner()
        sched = OlanoScheduler()
        sched._runners["anna"] = runner
        await sched._execute_db_task(db.get_task(task_id), db, manual=True)
        assert "late because" not in runner.ainvoke.call_args.args[0]

    async def test_catch_up_run_tells_the_agent_how_late_it_is(self, db: Any) -> None:
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="anna")
        runner = _runner()
        sched = OlanoScheduler()
        sched._runners["anna"] = runner
        sched._catch_up_due[task_id] = datetime.now(UTC) - timedelta(minutes=41)

        await sched._execute_db_task(db.get_task(task_id), db)

        sent = runner.ainvoke.call_args.args[0]
        assert "This run was due at" in sent
        assert "41 minutes late" in sent
        assert task_id not in sched._catch_up_due

    def test_customer_turn_carries_the_stamp_behind_its_own_preamble(self) -> None:
        from olano.scheduler.multi_user import CUSTOMER_TURN_PREAMBLE

        text = clock.autonomous_preamble(
            "remind them", tz_name="Europe/Madrid", directive=CUSTOMER_TURN_PREAMBLE
        )
        assert text.startswith(CUSTOMER_TURN_PREAMBLE.strip())
        assert "Current date and time:" in text
        assert "Europe/Madrid" in text
        assert text.endswith("\n\nremind them")


class TestMisfireGrace:
    def test_db_tasks_get_the_configured_grace(self, db: Any) -> None:
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="anna")
        sched = OlanoScheduler()
        with (
            patch("olano.scheduler.cron.SchedulerDB", create=True),
            patch("olano.scheduler.db.SchedulerDB", return_value=db),
            patch.object(sched._scheduler, "add_job") as add_job,
        ):
            ok, _ = sched.sync_db_task(task_id)
        assert ok is True
        assert add_job.call_args.kwargs["misfire_grace_time"] == 360 * 60
        assert "next_run_time" not in add_job.call_args.kwargs

    def test_grace_follows_the_scheduler_block(self, db: Any) -> None:
        from olano.config.system_settings import set_scheduler_settings

        set_scheduler_settings({"misfire_grace_minutes": 90})
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="anna")
        sched = OlanoScheduler()
        with (
            patch("olano.scheduler.db.SchedulerDB", return_value=db),
            patch.object(sched._scheduler, "add_job") as add_job,
        ):
            sched.sync_db_task(task_id)
        assert add_job.call_args.kwargs["misfire_grace_time"] == 90 * 60

    def test_config_schedule_entries_get_the_same_grace(self, base_config: Any) -> None:
        from olano.config.agent_config import ScheduleConfig

        base_config.schedule = [ScheduleConfig(cron="0 10 * * *", message="digest")]
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, _runner(), workspace_root=None)
        cfg_call = next(
            c for c in add_job.call_args_list if c.kwargs.get("id") == f"cfg:{base_config.name}:0"
        )
        assert cfg_call.kwargs["misfire_grace_time"] == 360 * 60

    def test_internal_polls_keep_the_short_default(self) -> None:
        # Heartbeat / issue polls / Cortex phases: the next tick is minutes
        # away, so they stay on the job defaults.
        sched = OlanoScheduler()
        assert sched._scheduler._job_defaults["misfire_grace_time"] == 300


class TestCatchUpOnBoot:
    def test_a_fire_the_restart_covered_runs_at_once(self, db: Any) -> None:
        task_id = db.create_task("leads", "find leads", schedule="* * * * *", agent_name="anna")
        # Never ran, created a while ago: the previous minute's fire is owed.
        db.update_task(task_id, last_run=None)
        with db._conn() as conn:
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), task_id),
            )
            conn.commit()
        sched = OlanoScheduler()
        with (
            patch("olano.scheduler.db.SchedulerDB", return_value=db),
            patch.object(sched._scheduler, "add_job") as add_job,
        ):
            sched._load_db_tasks()
        kwargs = add_job.call_args.kwargs
        assert "next_run_time" in kwargs  # fire now, then follow the cron
        assert task_id in sched._catch_up_due
        assert sched._db_loaded is True

    def test_a_dashboard_save_never_catches_up(self, db: Any) -> None:
        task_id = db.create_task("leads", "find leads", schedule="* * * * *", agent_name="anna")
        with db._conn() as conn:
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), task_id),
            )
            conn.commit()
        sched = OlanoScheduler()
        with (
            patch("olano.scheduler.db.SchedulerDB", return_value=db),
            patch.object(sched._scheduler, "add_job") as add_job,
        ):
            sched.sync_db_task(task_id)
        assert "next_run_time" not in add_job.call_args.kwargs

    def test_catch_up_can_be_switched_off(self, db: Any) -> None:
        from olano.config.system_settings import set_scheduler_settings

        set_scheduler_settings({"catch_up_on_boot": False})
        task_id = db.create_task("leads", "find leads", schedule="* * * * *", agent_name="anna")
        with db._conn() as conn:
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), task_id),
            )
            conn.commit()
        sched = OlanoScheduler()
        with (
            patch("olano.scheduler.db.SchedulerDB", return_value=db),
            patch.object(sched._scheduler, "add_job") as add_job,
        ):
            sched._load_db_tasks()
        assert "next_run_time" not in add_job.call_args.kwargs

    def test_a_task_that_already_ran_is_not_run_again(self, db: Any) -> None:
        task_id = db.create_task("leads", "find leads", schedule="* * * * *", agent_name="anna")
        db.add_history(task_id, result="ok")  # last_run = now
        sched = OlanoScheduler()
        with (
            patch("olano.scheduler.db.SchedulerDB", return_value=db),
            patch.object(sched._scheduler, "add_job") as add_job,
        ):
            sched._load_db_tasks()
        assert "next_run_time" not in add_job.call_args.kwargs


class TestBootLoadRetries:
    def test_an_unreadable_db_warns_and_retries(self, caplog: Any) -> None:
        import olano.scheduler.db  # noqa: F401

        sched = OlanoScheduler()
        with (
            patch("olano.scheduler.db.SchedulerDB", side_effect=RuntimeError("database is locked")),
            patch.object(sched, "_schedule_db_load_retry") as retry,
            caplog.at_level("WARNING", logger="olano.scheduler.cron"),
        ):
            sched._load_db_tasks()
        retry.assert_called_once()
        assert sched._db_loaded is False
        assert "database is locked" in caplog.text
        assert "attempt 1/" in caplog.text

    def test_giving_up_raises_a_notification(self) -> None:
        import olano.scheduler.db  # noqa: F401

        sched = OlanoScheduler()
        sched._db_load_attempts = DB_LOAD_RETRIES
        with (
            patch("olano.scheduler.db.SchedulerDB", side_effect=RuntimeError("corrupt")),
            patch("olano.scheduler.cron._notify_scheduler_db_unreadable") as notify,
            patch.object(sched, "_schedule_db_load_retry") as retry,
        ):
            sched._load_db_tasks()
        notify.assert_called_once()
        retry.assert_not_called()

    def test_a_later_success_clears_the_attempt_counter(self, db: Any) -> None:
        sched = OlanoScheduler()
        sched._db_load_attempts = 2
        with patch("olano.scheduler.db.SchedulerDB", return_value=db):
            sched._load_db_tasks()
        assert sched._db_loaded is True
        assert sched._db_load_attempts == 0


class TestRunOutcomeNotifications:
    def test_a_failed_db_run_rings_the_bell_and_a_good_one_clears_it(self, db: Any) -> None:
        from olano.runtime import notifications

        notifications.reset_for_tests()
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="anna")
        sched = OlanoScheduler()
        failing = MagicMock()
        failing.ainvoke = AsyncMock(side_effect=RuntimeError("provider down"))
        sched._runners["anna"] = failing

        import asyncio

        asyncio.run(sched._execute_db_task(db.get_task(task_id), db, manual=True))
        rows = notifications.list_notifications()
        assert any(
            r["kind"] == "scheduled_run" and r["dedupe_key"] == _run_alert_key(task_id)
            for r in rows
        )
        row = next(r for r in rows if r["kind"] == "scheduled_run")
        assert "failed" in row["title"] and "leads" in row["title"]
        assert row["agent"] == "anna"
        assert row["action"]["href"] == f"/scheduler?task={task_id}"

        sched._runners["anna"] = _runner()
        asyncio.run(sched._execute_db_task(db.get_task(task_id), db, manual=True))
        assert not [
            r
            for r in notifications.list_notifications(include_read=False)
            if r["dedupe_key"] == _run_alert_key(task_id) and not r.get("resolved_at")
        ]

    def test_an_inactive_agent_is_a_failure_worth_telling(self, db: Any) -> None:
        from olano.runtime import notifications

        notifications.reset_for_tests()
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="ghost")
        sched = OlanoScheduler()
        import asyncio

        asyncio.run(sched._execute_db_task(db.get_task(task_id), db))
        assert any(
            r["dedupe_key"] == _run_alert_key(task_id) for r in notifications.list_notifications()
        )

    def test_a_skipped_db_run_rings_the_bell(self, db: Any) -> None:
        from olano.runtime import notifications

        notifications.reset_for_tests()
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="anna")
        sched = OlanoScheduler()
        sched._db_task_ids[f"db_{task_id[:8]}"] = task_id
        with patch("olano.scheduler.db.SchedulerDB", return_value=db):
            sched._record_skipped_run(
                f"db_{task_id[:8]}", "skipped: the previous run was still executing"
            )
        row = next(
            r
            for r in notifications.list_notifications()
            if r["dedupe_key"] == _run_alert_key(task_id)
        )
        assert "skipped" in row["title"]
        assert db.get_history(task_id)[0]["error"].startswith("skipped")

    def test_a_skipped_config_job_names_its_agent(self) -> None:
        from olano.runtime import notifications

        notifications.reset_for_tests()
        sched = OlanoScheduler()
        with patch("olano.scheduler.cron._record_config_run"):
            sched._record_skipped_run("cfg:maya-ops:0", "skipped: whatever")
        row = next(
            r
            for r in notifications.list_notifications()
            if r["dedupe_key"] == _run_alert_key("cfg:maya-ops:0")
        )
        assert row["agent"] == "maya-ops"
        assert _config_job_agent("cfg:maya-ops:0") == "maya-ops"
        assert _config_job_agent("db_1234") is None


# ──────────────────────────────── middleware ─────────────────────────────────


class _Req:
    def __init__(self, system_message=None) -> None:
        self.system_message = system_message

    def override(self, *, system_message):
        return _Req(system_message)


class TestClockMiddleware:
    def test_today_is_appended_to_the_system_message(self) -> None:
        from langchain_core.messages import SystemMessage

        from olano.core.clock_middleware import ClockMiddleware

        seen: list[Any] = []
        ClockMiddleware().wrap_model_call(_Req(SystemMessage("You are Anna.")), seen.append)
        text = seen[0].system_message.text
        assert text.startswith("You are Anna.")
        assert "## Current date\nToday is " in text
        assert "get_current_time" in text

    async def test_async_path_matches(self) -> None:
        from olano.core.clock_middleware import ClockMiddleware

        seen: list[Any] = []

        async def handler(req: Any) -> None:
            seen.append(req)

        await ClockMiddleware().awrap_model_call(_Req(None), handler)
        assert "## Current date" in seen[0].system_message.text

    def test_switched_off_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from olano.core.clock_middleware import build_clock_middleware

        monkeypatch.setenv("LIONCLAW_CLOCK_STAMP", "0")
        assert build_clock_middleware() == []
        monkeypatch.setenv("LIONCLAW_CLOCK_STAMP", "1")
        assert len(build_clock_middleware()) == 1

    def test_the_builder_includes_it(self) -> None:
        source = Path("olano/core/builder.py").read_text(encoding="utf-8")
        assert "*build_clock_middleware()," in source


class TestSchedulerSettingsBlock:
    def test_defaults_and_clamps(self) -> None:
        from olano.config.system_settings import SchedulerSettings, load_scheduler_settings

        assert load_scheduler_settings() == SchedulerSettings()
        assert SchedulerSettings().misfire_grace_minutes == 360
        assert SchedulerSettings(misfire_grace_minutes=0).misfire_grace_minutes == 1
        assert SchedulerSettings(misfire_grace_minutes=99_999).misfire_grace_minutes == 1440

    def test_round_trip_preserves_other_keys(self) -> None:
        from olano.config.system_settings import load_scheduler_settings, set_scheduler_settings

        set_scheduler_settings({"misfire_grace_minutes": 120})
        set_scheduler_settings({"catch_up_on_boot": False})
        loaded = load_scheduler_settings()
        assert loaded.misfire_grace_minutes == 120
        assert loaded.catch_up_on_boot is False

    def test_admin_section_writer_knows_the_block(self) -> None:
        from olano.admin.config import _SECTION_WRITERS

        assert "scheduler" in _SECTION_WRITERS


# ───────────────────────────── a run that never ends ─────────────────────────


class TestRunTimeLimit:
    """A scheduled turn is bounded by ``scheduler.max_run_minutes``.

    On 2026-11-02 one box's three campaign runs had been going for five to
    fifteen hours when a restart ended them. Each held its task's only slot,
    so the task's next firing was dropped as "previous run still executing".
    """

    async def test_a_run_over_the_limit_is_stopped_and_recorded(self, db: Any) -> None:
        import asyncio

        from olano.runtime import notifications

        notifications.reset_for_tests()
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="anna")
        runner = MagicMock()

        async def _never_finishes(*_a: Any, **_k: Any) -> str:
            await asyncio.sleep(30)
            return "too late"

        runner.ainvoke = _never_finishes
        sched = OlanoScheduler()
        sched._runners["anna"] = runner
        with patch.object(OlanoScheduler, "_run_time_limit_seconds", return_value=0.05):
            await sched._execute_db_task(db.get_task(task_id), db, manual=True)

        row = db.get_history(task_id)[0]
        assert row["result"] is None
        assert "scheduler.max_run_minutes" in str(row["error"])
        assert "next firing can run" in str(row["error"])
        assert any(
            r["dedupe_key"] == _run_alert_key(task_id) for r in notifications.list_notifications()
        )

    async def test_a_run_inside_the_limit_is_untouched(self, db: Any) -> None:
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="anna")
        sched = OlanoScheduler()
        sched._runners["anna"] = _runner("done")
        with patch.object(OlanoScheduler, "_run_time_limit_seconds", return_value=5.0):
            await sched._execute_db_task(db.get_task(task_id), db, manual=True)
        assert db.get_history(task_id)[0]["result"] == "done"

    async def test_zero_means_no_limit(self) -> None:
        from olano.config.system_settings import set_scheduler_settings

        set_scheduler_settings({"max_run_minutes": 0})
        assert OlanoScheduler._run_time_limit_seconds() is None
        set_scheduler_settings({"max_run_minutes": 45})
        assert OlanoScheduler._run_time_limit_seconds() == 45 * 60

    async def test_config_jobs_and_heartbeats_share_the_bound(self, base_config: Any) -> None:
        import asyncio

        from olano.config.agent_config import ScheduleConfig

        base_config.schedule = [ScheduleConfig(cron="0 10 * * *", message="digest")]
        runner = MagicMock()

        async def _never_finishes(*_a: Any, **_k: Any) -> str:
            await asyncio.sleep(30)
            return "too late"

        runner.ainvoke = _never_finishes
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched.register_agent(base_config, runner, workspace_root=None)
        run = next(
            c.args[0]
            for c in add_job.call_args_list
            if c.kwargs.get("id") == f"cfg:{base_config.name}:0"
        )
        with (
            patch.object(OlanoScheduler, "_run_time_limit_seconds", return_value=0.05),
            patch("olano.scheduler.cron._record_config_run") as rec,
        ):
            await run()
        assert "scheduler.max_run_minutes" in rec.call_args.kwargs["error"]

    def test_settings_clamp(self) -> None:
        from olano.config.system_settings import SchedulerSettings

        assert SchedulerSettings().max_run_minutes == 180
        assert SchedulerSettings(max_run_minutes=-5).max_run_minutes == 0
        assert SchedulerSettings(max_run_minutes=99_999).max_run_minutes == 1440


class TestLiveNextRun:
    """The Scheduler tab's Next column reads the scheduler's own job."""

    async def test_a_registered_task_has_a_next_run_and_an_unknown_one_none(self, db: Any) -> None:
        task_id = db.create_task("leads", "find leads", schedule="0 8 * * *", agent_name="anna")
        sched = OlanoScheduler()
        sched._scheduler.start(paused=True)
        try:
            with patch("olano.scheduler.db.SchedulerDB", return_value=db):
                ok, _ = sched.sync_db_task(task_id)
            assert ok
            assert sched.next_run_for_task(task_id) is not None
            assert sched.next_run_for_task("00000000-not-a-task") is None
        finally:
            sched._scheduler.shutdown(wait=False)

    async def test_the_route_fills_next_run_for_saved_tasks(self, db: Any) -> None:
        source = Path("olano/runtime/routes/scheduler.py").read_text(encoding="utf-8")
        assert "next_run_for_task" in source
        assert 't["live"]' in source
