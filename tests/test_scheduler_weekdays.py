"""Schedules are standard cron, and the tools say which days and when next.

Found on a managed box on 2026-11-23: six campaign tasks an agent had created
with ``* * 1-5`` (cron for Monday to Friday) ran Tuesday to Saturday, because
the field was handed to APScheduler as written and APScheduler counts from
Monday = 0. Asked why Monday's runs were missing, the agent could not see it:
its tools showed the raw cron and "Next run: unscheduled" for every task, the
latter read from a ``tasks.next_run`` column nothing writes. The Cortex
presets written ``* * 0`` for Sunday ran on Mondays for the same reason.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from olano.scheduler import clock
from olano.scheduler.cron import OlanoScheduler
from olano.scheduler.db import SchedulerDB
from olano.tools import scheduler_tools


@pytest.mark.parametrize(
    ("expr", "days"),
    [
        ("0 8 * * 1-5", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("0 8 * * mon-fri", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("0 8 * * 0", ["Sun"]),
        ("0 8 * * 7", ["Sun"]),
        ("0 8 * * sun", ["Sun"]),
        ("0 8 * * 1,4", ["Mon", "Thu"]),
        ("0 8 * * 6-7", ["Sat", "Sun"]),
        ("0 8 * * fri-mon", ["Mon", "Fri", "Sat", "Sun"]),
        ("0 8 * * 1-5/2", ["Mon", "Wed", "Fri"]),
        ("0 8 * * *", None),
        ("0 8 * * 0-6", None),
        ("0 8 * * 9", None),
        ("not a cron", None),
    ],
)
def test_cron_weekdays_follow_standard_cron(expr, days):
    assert clock.cron_weekdays(expr) == days


@pytest.mark.parametrize(
    ("field", "translated"),
    [
        ("1-5", "mon,tue,wed,thu,fri"),
        ("0", "sun"),
        ("7", "sun"),
        ("0-6", "*"),
        ("*", "*"),
        ("", "*"),
        ("mon-fri", "mon,tue,wed,thu,fri"),
        ("garbage", "garbage"),
    ],
)
def test_standard_weekdays_is_what_the_trigger_is_given(field, translated):
    assert clock.standard_weekdays(field) == translated


def _fires(trigger, start, count):
    cursor, out = start, []
    for _ in range(count):
        cursor = trigger.get_next_fire_time(cursor, cursor)
        out.append(cursor.strftime("%a"))
    return out


def test_a_saved_task_written_1_5_runs_monday_to_friday():
    tz = ZoneInfo("Asia/Singapore")
    trigger, _ = OlanoScheduler._db_task_trigger(
        {"id": "t", "schedule": "0 8 * * 1-5", "timezone": "Asia/Singapore"}
    )
    friday = datetime(2026, 11, 20, 9, 0, tzinfo=tz)  # after that day's 08:00 fire
    # Monday runs, Saturday does not: the opposite of the report from the box.
    assert _fires(trigger, friday, 5) == ["Mon", "Tue", "Wed", "Thu", "Fri"]


def test_config_entries_and_the_heartbeat_read_the_same_numbering():
    from olano.config.agent_config import ScheduleConfig
    from olano.scheduler.cron import _cron_matches

    sunday = datetime(2026, 11, 22, 12, 0)
    by_cron = OlanoScheduler._make_trigger(ScheduleConfig(cron="0 9 * * 0", message="m"))
    by_field = OlanoScheduler._make_trigger(
        ScheduleConfig(minute="0", hour="9", day_of_week="0", message="m")
    )
    for trigger in (by_cron, by_field):
        nxt = trigger.get_next_fire_time(None, sunday.astimezone())
        assert nxt.strftime("%a") == "Sun"
    aware = sunday.astimezone()
    assert _cron_matches("0 12 * * 0", aware) is True
    assert _cron_matches("0 12 * * 1", aware) is False


def test_cortex_presets_run_on_the_days_they_are_written_for():
    """``* * 0`` is Sunday and ``1,4`` is Monday and Thursday, as the guide says."""
    from olano.cognition import orchestrator

    source = __import__("inspect").getsource(orchestrator)
    assert '"0 4 * * 0"' in source and '"0 10 * * 1,4"' in source
    assert clock.cron_weekdays("0 4 * * 0") == ["Sun"]
    assert clock.cron_weekdays("0 10 * * 1,4") == ["Mon", "Thu"]


def test_weekday_note():
    assert clock.weekday_note("0 8 * * 1-5") == "Runs on: Mon, Tue, Wed, Thu, Fri"
    assert clock.weekday_note("0 8 * * *") == ""
    assert clock.weekday_note(None) == ""


def _task(**over):
    db = SchedulerDB()
    task_id = db.create_task(
        name="Discovery",
        prompt="find leads",
        schedule=over.pop("schedule", "0 8 * * 1-5"),
        timezone="Asia/Singapore",
        agent_name="anna",
        created_by="anna",
        **over,
    )
    scheduler_tools._db = db
    return db, task_id


@pytest.fixture(autouse=True)
def _fresh_tool_db():
    scheduler_tools._db = None
    yield
    scheduler_tools._db = None


def test_format_task_shows_a_real_next_run_and_the_days(monkeypatch):
    monkeypatch.setattr("olano.scheduler.cron.get_active_scheduler", lambda: None)
    db, task_id = _task()
    text = scheduler_tools._format_task(db.get_task(task_id))
    assert "unscheduled" not in text
    assert "Runs on: Mon, Tue, Wed, Thu, Fri" in text
    next_line = next(line for line in text.splitlines() if line.startswith("Next run: "))
    # A weekday, a date and the task's own timezone, on a day the cron fires.
    assert next_line.split()[2] in {"Mon", "Tue", "Wed", "Thu", "Fri"}
    assert next_line.endswith("Asia/Singapore")


def test_next_run_comes_from_the_live_job(monkeypatch):
    class _Live:
        def __init__(self, value):
            self.value = value

        def next_run_for_task(self, _task_id):
            return self.value

    db, task_id = _task(schedule="0 8 * * mon-fri")
    task = db.get_task(task_id)
    monkeypatch.setattr(
        "olano.scheduler.cron.get_active_scheduler", lambda: _Live("2026-11-24 08:00:00+08:00")
    )
    assert scheduler_tools._next_run_text(task) == "Tue 2026-11-24 08:00 Asia/Singapore"
    # A running scheduler with no job for the task is the one case that means
    # "this will not fire", and it says so instead of printing a time.
    monkeypatch.setattr("olano.scheduler.cron.get_active_scheduler", lambda: _Live(None))
    assert "not registered" in scheduler_tools._next_run_text(task)


def test_a_disabled_task_has_no_next_run(monkeypatch):
    monkeypatch.setattr("olano.scheduler.cron.get_active_scheduler", lambda: None)
    db, task_id = _task()
    db.update_task(task_id, enabled=0)
    assert "disabled" in scheduler_tools._next_run_text(db.get_task(task_id))


def test_create_tells_the_agent_which_days_it_booked(monkeypatch):
    monkeypatch.setattr("olano.scheduler.cron.get_active_scheduler", lambda: None)
    scheduler_tools._db = SchedulerDB()
    out = scheduler_tools.scheduler_create.invoke(
        {
            "name": "Outreach",
            "prompt": "send the batch",
            "schedule": "0 9 * * 1-5",
            "timezone": "Asia/Singapore",
        }
    )
    assert "Task created!" in out
    assert "Runs on: Mon, Tue, Wed, Thu, Fri" in out
    listing = scheduler_tools.scheduler_list.invoke({})
    assert "Next run: " in listing and "unscheduled" not in listing
    assert "Runs on: Mon, Tue, Wed, Thu, Fri" in listing


# ── migration 0030: existing schedules keep the days they run on ────────────


def _migration():
    import importlib

    return importlib.import_module("olano.migrations.0030_standard_cron_weekdays")


@pytest.mark.parametrize(
    "field",
    ["1-5", "0-4", "0", "6", "0,1,2,3,4", "1,4", "*/2", "2-6/2", "mon-fri", "*", "7", "5-1"],
)
def test_migrated_field_fires_on_the_same_days_as_before(field):
    """Old meaning (APScheduler, as written) == new meaning of the rewrite."""
    from apscheduler.triggers.cron import CronTrigger

    def days(dow):
        try:
            trigger = CronTrigger(minute="0", hour="0", day_of_week=dow, timezone="UTC")
        except Exception:
            return None
        monday = datetime(2024, 1, 1, tzinfo=ZoneInfo("UTC"))
        cursor, out = monday.replace(year=2023, month=12, day=31, hour=23), []
        for _ in range(7):
            cursor = trigger.get_next_fire_time(cursor, cursor)
            if cursor >= monday.replace(day=8):
                break
            out.append(cursor.strftime("%a"))
        return out

    before = days(field)
    rewritten = _migration().legacy_weekday_names(field)
    if before is None:  # the old scheduler rejected it: left exactly as written
        assert rewritten == field
        return
    assert days(clock.standard_weekdays(rewritten)) == before


def test_migration_rewrites_tasks_configs_and_heartbeats(tmp_path):
    import json
    import sqlite3

    from olano.config.loader import _load_json5

    home = tmp_path
    db = SchedulerDB(home / "scheduler" / "scheduler.db")
    agent_written = db.create_task(name="a", prompt="p", schedule="0 8 * * 1-5", agent_name="anna")
    picker_built = db.create_task(
        name="b", prompt="p", schedule="0 9 * * 0,1,2,3,4", agent_name="anna"
    )
    daily = db.create_task(name="c", prompt="p", schedule="0 7 * * *", agent_name="anna")
    agent_dir = home / "agents" / "anna"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": "anna",
                "schedule": [
                    {"cron": "0 9 * * 0-4", "message": "m"},
                    {"minute": "0", "hour": "7", "day_of_week": "0", "message": "m"},
                ],
                "cognition": {"workspace_optimize_cron": "0 4 * * 6"},
            }
        )
    )
    (agent_dir / "workspace" / "HEARTBEAT.md").write_text(
        "# when: 0 9 * * 1-5\n- task: inbox\n  when: 0 9 * * 0-4\n- task: x\n  when: always\n"
    )

    migration = _migration()
    migration.apply(home)
    migration.apply(home)  # idempotent: names are left alone the second time

    rows = dict(
        sqlite3.connect(home / "scheduler" / "scheduler.db").execute(
            "SELECT id, schedule FROM tasks"
        )
    )
    assert rows[agent_written] == "0 8 * * tue,wed,thu,fri,sat"
    assert rows[picker_built] == "0 9 * * mon,tue,wed,thu,fri"
    assert rows[daily] == "0 7 * * *"
    data = _load_json5((agent_dir / "agent.json5").read_text())
    assert data["schedule"][0]["cron"] == "0 9 * * mon,tue,wed,thu,fri"
    assert data["schedule"][1]["day_of_week"] == "mon"
    assert data["cognition"]["workspace_optimize_cron"] == "0 4 * * sun"
    assert (agent_dir / "workspace" / "HEARTBEAT.md").read_text() == (
        "# when: 0 9 * * 1-5\n- task: inbox\n  when: 0 9 * * mon,tue,wed,thu,fri\n"
        "- task: x\n  when: always\n"
    )
