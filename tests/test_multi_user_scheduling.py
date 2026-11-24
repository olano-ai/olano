"""Tests for multi-user scheduling: the phantom-identity guard, Type-1
``for_each_customer`` fan-out, Type-2 customer-owned DB tasks, and — because
the user called it absolutely critical — that a normal (single-user) agent's
scheduling path is untouched by all of it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from olano.config.agent_config import AgentConfig, ScheduleConfig
from olano.memory.customer_memory import (
    IDENTITY_FILENAME,
    active_customer_id,
    active_customer_platform,
    customers_base,
    load_customer_identity,
    make_customer_memory_tools,
)
from olano.scheduler.db import SchedulerDB
from olano.security.request_context import get_request_context, push_request_context

AGENT = "supportbot"


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


class FakeRunner:
    """Captures the request context each ainvoke ran under."""

    def __init__(self, config: AgentConfig | None = None, reply: str = "done") -> None:
        self.config = config
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, prompt: str, thread_id: str | None = None, **overrides: Any) -> str:
        ctx = get_request_context()
        self.calls.append(
            {
                "prompt": prompt,
                "thread_id": thread_id,
                # The per-job model / effort overrides the scheduler forwards.
                "override_model": overrides.get("override_model"),
                "override_reasoning_effort": overrides.get("override_reasoning_effort"),
                "channel": ctx.channel,
                "user_id": ctx.user_id,
                "is_owner": ctx.is_owner,
                "source": ctx.source,
                "customer_id": active_customer_id(),
            }
        )
        return self.reply


# ─────────────────────── phase 0: the phantom-identity guard ──────────────────


def test_scheduler_context_resolves_no_customer():
    """`channel="scheduler"` + default user_id used to resolve to the phantom
    `scheduler-owner` store; now there is simply no customer."""
    with push_request_context(source="scheduled", channel="scheduler", authenticated=True):
        assert active_customer_platform() is None
        assert active_customer_id() is None


def test_customer_tools_refuse_under_scheduler_context(lionclaw_home):
    tools = make_customer_memory_tools(AGENT)
    with push_request_context(source="scheduled", channel="scheduler", authenticated=True):
        assert "No identified customer" in _tool(tools, "remember").invoke({"fact": "x"})
        assert "No identified customer" in _tool(tools, "forget_me").invoke({})
    assert not customers_base(AGENT).exists()


def test_real_channel_context_still_resolves():
    with push_request_context(source="channel", channel="telegram", user_id="449281"):
        assert active_customer_platform() == "telegram"
        assert active_customer_id() == "telegram-449281"


def test_per_user_bound_scheduled_context_resolves():
    """The Type-2 executor binds the REAL platform as the channel, so the
    guard lets scheduled per-user turns through."""
    with push_request_context(
        source="scheduled", channel="telegram", user_id="449281", authenticated=True
    ):
        assert active_customer_id() == "telegram-449281"


# ─────────────────────────── identity sidecar ─────────────────────────────────


def test_live_contact_records_raw_identity(lionclaw_home):
    """A rewritten id (WhatsApp JID) keeps its RAW deliverable address in the
    sidecar — the folder name alone cannot give it back."""
    tools = make_customer_memory_tools(AGENT)
    raw = "50688881234@c.us"
    with push_request_context(source="channel", channel="whatsapp", user_id=raw):
        _tool(tools, "remember").invoke({"fact": "likes tea"})
        cid = active_customer_id()
    folder = customers_base(AGENT) / cid
    sidecar = json.loads((folder / IDENTITY_FILENAME).read_text(encoding="utf-8"))
    assert sidecar["user_id"] == raw
    assert sidecar["platform"] == "whatsapp"
    assert load_customer_identity(folder) == ("whatsapp", raw)


def test_identity_falls_back_to_folder_name(tmp_path):
    folder = tmp_path / "telegram-449281"
    folder.mkdir()
    assert load_customer_identity(folder) == ("telegram", "449281")
    bad = tmp_path / "noseparator"
    bad.mkdir()
    assert load_customer_identity(bad) is None


def test_sidecar_is_not_listed_as_a_user_file(lionclaw_home, tmp_path):
    from olano.tools.customer_actions import make_customer_action_tools

    tools = make_customer_action_tools(AGENT, {}, [tmp_path])
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        _tool(tools, "write_customer_file").invoke({"path": "a.md", "content": "x"})
        listing = _tool(tools, "list_customer_files").invoke({})
    assert "a.md" in listing
    assert IDENTITY_FILENAME not in listing


# ──────────────────────────── scheduler DB columns ────────────────────────────


def test_tasks_carry_optional_customer_identity(tmp_path):
    db = SchedulerDB(tmp_path / "sched.db")
    plain = db.create_task(name="n", prompt="p", schedule="0 9 * * *")
    owned = db.create_task(
        name="mine",
        prompt="remind me",
        schedule="0 9 * * *",
        agent_name=AGENT,
        platform="telegram",
        user_id="449281",
    )
    assert db.get_task(plain)["platform"] is None
    assert db.get_task(plain)["user_id"] is None
    task = db.get_task(owned)
    assert (task["platform"], task["user_id"]) == ("telegram", "449281")


def test_legacy_table_is_migrated_in_place(tmp_path):
    """A pre-multi-user DB (no platform/user_id columns) gains them via the
    ALTER path, and existing rows read back with a NULL identity."""
    import sqlite3

    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
                schedule TEXT, run_at TEXT, timezone TEXT DEFAULT 'UTC',
                enabled INTEGER DEFAULT 1, agent_name TEXT, created_at TEXT NOT NULL,
                last_run TEXT, next_run TEXT, run_count INTEGER DEFAULT 0,
                embedding BLOB, visibility TEXT DEFAULT 'private', created_by TEXT)"""
        )
        conn.execute(
            "INSERT INTO tasks (id, name, prompt, schedule, created_at) "
            "VALUES ('t1', 'old', 'p', '0 9 * * *', '2026-01-01T00:00:00')"
        )
        conn.commit()
    db = SchedulerDB(db_path)
    task = db.get_task("t1")
    assert task is not None
    assert task["platform"] is None and task["user_id"] is None


def test_user_task_helpers_scope_to_the_exact_identity(tmp_path):
    db = SchedulerDB(tmp_path / "sched.db")
    mine = db.create_task(
        name="a",
        prompt="p",
        schedule="0 9 * * *",
        agent_name=AGENT,
        platform="telegram",
        user_id="alice",
    )
    db.create_task(
        name="b",
        prompt="p",
        schedule="0 9 * * *",
        agent_name=AGENT,
        platform="telegram",
        user_id="bob",
    )
    db.create_task(
        name="c",
        prompt="p",
        schedule="0 9 * * *",
        agent_name=AGENT,
        platform="whatsapp",
        user_id="alice",
    )
    assert [t["id"] for t in db.list_user_tasks(AGENT, "telegram", "alice")] == [mine]
    assert db.count_user_tasks(AGENT, "telegram", "alice") == 1
    deleted = db.delete_user_tasks(AGENT, "telegram", "alice")
    assert deleted == [mine]
    # The other identities' rows are untouched.
    assert db.count_user_tasks(AGENT, "telegram", "bob") == 1
    assert db.count_user_tasks(AGENT, "whatsapp", "alice") == 1


# ─────────────────────────── config-schema invariants ─────────────────────────


def test_classic_schedule_entry_parses_exactly_as_before():
    sched = ScheduleConfig(cron="0 7 * * *", message="Run the morning sweep.")
    assert sched.message == "Run the morning sweep."
    assert sched.for_each_customer is False
    assert sched.action is None


def test_schedule_entry_without_message_still_refuses_to_parse():
    with pytest.raises(Exception):
        ScheduleConfig(cron="0 7 * * *")


def test_action_requires_fan_out_flag():
    with pytest.raises(Exception):
        ScheduleConfig(cron="0 7 * * *", message="x", action="alerts")


def test_fan_out_requires_action_or_message():
    with pytest.raises(Exception):
        ScheduleConfig(cron="0 7 * * *", for_each_customer=True)
    ScheduleConfig(cron="0 7 * * *", for_each_customer=True, action="alerts")
    ScheduleConfig(cron="0 7 * * *", for_each_customer=True, message="per-user turn")


def test_fan_out_and_user_scheduling_require_multi_user_mode():
    with pytest.raises(Exception):
        AgentConfig(
            name="t",
            schedule=[{"cron": "0 9 * * *", "for_each_customer": True, "action": "a"}],
        )
    with pytest.raises(Exception):
        AgentConfig(name="t", customer_service={"user_scheduling": True})
    AgentConfig(
        name="t",
        customer_service={"enabled": True, "user_scheduling": True},
        schedule=[{"cron": "0 9 * * *", "for_each_customer": True, "action": "a"}],
    )


# ─────────────────────────── per-user scheduler tools ─────────────────────────


@pytest.fixture
def user_tools(lionclaw_home):
    from olano.config.agent_config import CustomerServiceConfig
    from olano.tools.customer_scheduler import make_customer_scheduler_tools

    cs = CustomerServiceConfig(
        enabled=True, user_scheduling=True, max_jobs_per_user=2, min_job_interval_minutes=60
    )
    return make_customer_scheduler_tools(AGENT, cs)


def _as(platform: str, user: str):
    return push_request_context(source="channel", channel=platform, user_id=user)


def test_schedule_my_task_stores_the_context_identity(user_tools):
    with _as("telegram", "449281"):
        out = _tool(user_tools, "schedule_my_task").invoke(
            {"name": "bills", "prompt": "send my bill summary", "schedule": "0 8 * * *"}
        )
    assert "Scheduled!" in out
    tasks = SchedulerDB().list_user_tasks(AGENT, "telegram", "449281")
    assert len(tasks) == 1
    assert tasks[0]["agent_name"] == AGENT
    assert tasks[0]["schedule"] == "0 8 * * *"


def test_schedule_tools_take_no_identity_parameters(user_tools):
    for name in ("schedule_my_task", "list_my_scheduled_tasks", "cancel_my_scheduled_task"):
        props = set(_tool(user_tools, name).args_schema.model_json_schema().get("properties", {}))
        assert not props & {"user_id", "platform", "customer_id", "target"}


def test_schedule_refuses_without_identity(user_tools):
    with push_request_context(source="scheduled", channel="scheduler"):
        out = _tool(user_tools, "schedule_my_task").invoke(
            {"name": "x", "prompt": "y", "schedule": "0 8 * * *"}
        )
    assert "No identified user" in out


@pytest.mark.parametrize("too_often", ["*/5 * * * *", "* * * * *", "0,30 * * * *"])
def test_frequency_floor_rejects_sub_hourly(user_tools, too_often):
    with _as("telegram", "449281"):
        out = _tool(user_tools, "schedule_my_task").invoke(
            {"name": "spam", "prompt": "p", "schedule": too_often}
        )
    assert "minimum allowed interval" in out
    assert SchedulerDB().count_user_tasks(AGENT, "telegram", "449281") == 0


def test_hourly_passes_the_default_floor(user_tools):
    with _as("telegram", "449281"):
        out = _tool(user_tools, "schedule_my_task").invoke(
            {"name": "hourly", "prompt": "p", "schedule": "0 * * * *"}
        )
    assert "Scheduled!" in out


def test_per_user_cap_is_enforced(user_tools):
    with _as("telegram", "449281"):
        for i in range(2):
            assert "Scheduled!" in _tool(user_tools, "schedule_my_task").invoke(
                {"name": f"j{i}", "prompt": "p", "schedule": "0 8 * * *"}
            )
        out = _tool(user_tools, "schedule_my_task").invoke(
            {"name": "j3", "prompt": "p", "schedule": "0 9 * * *"}
        )
    assert "limit" in out
    # …and the cap is per user, not global.
    with _as("telegram", "bob"):
        assert "Scheduled!" in _tool(user_tools, "schedule_my_task").invoke(
            {"name": "bobs", "prompt": "p", "schedule": "0 8 * * *"}
        )


def test_list_and_cancel_are_scoped_to_the_asker(user_tools):
    with _as("telegram", "alice"):
        _tool(user_tools, "schedule_my_task").invoke(
            {"name": "alices", "prompt": "p", "schedule": "0 8 * * *"}
        )
    alice_id = SchedulerDB().list_user_tasks(AGENT, "telegram", "alice")[0]["id"]

    with _as("telegram", "bob"):
        listing = _tool(user_tools, "list_my_scheduled_tasks").invoke({})
        assert "alices" not in listing
        # Bob cannot cancel Alice's task, even with its exact id.
        out = _tool(user_tools, "cancel_my_scheduled_task").invoke({"task_id": alice_id})
        assert "No scheduled task of yours" in out
    assert SchedulerDB().get_task(alice_id) is not None

    with _as("telegram", "alice"):
        out = _tool(user_tools, "cancel_my_scheduled_task").invoke({"task_id": alice_id[:8]})
        assert "Canceled" in out
    assert SchedulerDB().get_task(alice_id) is None


def test_one_time_task_must_be_in_the_future(user_tools):
    with _as("telegram", "449281"):
        out = _tool(user_tools, "schedule_my_task").invoke(
            {"name": "x", "prompt": "p", "run_at": "2020-01-01T00:00:00"}
        )
    assert "future" in out


def test_exactly_one_of_schedule_or_run_at(user_tools):
    with _as("telegram", "449281"):
        both = _tool(user_tools, "schedule_my_task").invoke(
            {"name": "x", "prompt": "p", "schedule": "0 8 * * *", "run_at": "299-01-01T00:00:00"}
        )
        neither = _tool(user_tools, "schedule_my_task").invoke({"name": "x", "prompt": "p"})
    assert "exactly one" in both
    assert "exactly one" in neither


def test_builder_binds_scheduler_tools_only_when_opted_in(lionclaw_home, tmp_path):
    from olano.core.builder import _build_memory_tools

    ws = tmp_path / "ws"
    ws.mkdir()
    on = AgentConfig(name=AGENT, customer_service={"enabled": True, "user_scheduling": True})
    # Scheduler tools live in the "customer" category: exempt from the
    # top_level_memory_tools pin, governed by their own opt-in only.
    names = {t.name for t in _build_memory_tools(on, ws, categorized=True)["customer"]}
    assert {"schedule_my_task", "list_my_scheduled_tasks", "cancel_my_scheduled_task"} <= names

    off = AgentConfig(name=AGENT, customer_service={"enabled": True})
    cats = _build_memory_tools(off, ws, categorized=True)
    names = {t.name for t in [*cats["memory"], *cats["customer"]]}
    assert "schedule_my_task" not in names


# ─────────────────────────── forget_me erases queued work ─────────────────────


def test_forget_me_cancels_the_users_scheduled_tasks(lionclaw_home):
    db = SchedulerDB()
    kept = db.create_task(
        name="bobs",
        prompt="p",
        schedule="0 8 * * *",
        agent_name=AGENT,
        platform="telegram",
        user_id="bob",
    )
    gone = db.create_task(
        name="alices",
        prompt="p",
        schedule="0 8 * * *",
        agent_name=AGENT,
        platform="telegram",
        user_id="alice",
    )
    tools = make_customer_memory_tools(AGENT)
    with _as("telegram", "alice"):
        _tool(tools, "remember").invoke({"fact": "x"})
        out = _tool(tools, "forget_me").invoke({})
    assert "Deleted" in out
    assert db.get_task(gone) is None
    assert db.get_task(kept) is not None


# ─────────────────────────────── Type-1 fan-out ───────────────────────────────

#: Writes a per-user file and asks the runtime to notify the user — enough to
#: prove which folder/identity each fan-out iteration ran as.
FAN_ACTION = """\
import json, os, sys
payload = json.load(sys.stdin)
open("ran.txt", "w").write(os.environ.get("LIONCLAW_CUSTOMER_ID", ""))
print(json.dumps({"notify": "hello " + os.environ.get("LIONCLAW_CUSTOMER_ID", "")}))
"""


def _seed_customer(agent: str, platform: str, user: str) -> Path:
    """Create a customer folder the way a live contact would."""
    with push_request_context(source="channel", channel=platform, user_id=user):
        tools = make_customer_memory_tools(agent)
        _tool(tools, "remember").invoke({"fact": "seeded"})
        cid = active_customer_id()
    return customers_base(agent) / cid


@pytest.fixture
def deliveries(monkeypatch):
    """Capture deliver_text calls instead of touching the channel registry."""
    from olano.scheduler import multi_user

    sent: list[tuple[str, str, str, str]] = []

    async def fake_deliver(agent_name, platform, user_id, text):
        sent.append((agent_name, platform, user_id, text))
        return True

    monkeypatch.setattr(multi_user, "deliver_text", fake_deliver)
    return sent


def _fan_out_config(tmp_path: Path, **extra) -> AgentConfig:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "fan.py").write_text(FAN_ACTION, encoding="utf-8")
    return AgentConfig(
        name=AGENT,
        workspace=str(ws),
        customer_service={"enabled": True},
        allowed_actions={"alerts": "fan.py"},
        **extra,
    )


def test_fan_out_action_runs_once_per_customer(lionclaw_home, tmp_path, deliveries):
    from olano.scheduler.multi_user import run_for_each_customer

    a = _seed_customer(AGENT, "telegram", "alice")
    b = _seed_customer(AGENT, "telegram", "bob")
    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(cron="0 9 * * *", for_each_customer=True, action="alerts")

    summary = asyncio.get_event_loop().run_until_complete(
        run_for_each_customer("cfg:supportbot:0", config, sched, FakeRunner())
    )
    assert "2 ran" in summary
    # Each script ran in its own user's folder with that user's raw id.
    assert (a / "ran.txt").read_text() == "alice"
    assert (b / "ran.txt").read_text() == "bob"
    # And the notify contract delivered to each raw address on their platform.
    assert sorted((p, u, t) for _, p, u, t in deliveries) == [
        ("telegram", "alice", "hello alice"),
        ("telegram", "bob", "hello bob"),
    ]


def test_fan_out_uses_the_sidecars_raw_address(lionclaw_home, tmp_path, deliveries):
    """A rewritten id's folder name addresses nobody — the sidecar's raw JID
    is what reaches the delivery hook."""
    from olano.scheduler.multi_user import run_for_each_customer

    raw = "50688881234@c.us"
    _seed_customer(AGENT, "whatsapp", raw)
    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(cron="0 9 * * *", for_each_customer=True, action="alerts")

    asyncio.get_event_loop().run_until_complete(
        run_for_each_customer("cfg:supportbot:0", config, sched, FakeRunner())
    )
    assert [(p, u) for _, p, u, _ in deliveries] == [("whatsapp", raw)]


def test_fan_out_isolates_one_customers_failure(lionclaw_home, tmp_path, deliveries, monkeypatch):
    from olano.scheduler import multi_user

    _seed_customer(AGENT, "telegram", "alice")
    _seed_customer(AGENT, "telegram", "bob")
    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(cron="0 9 * * *", for_each_customer=True, action="alerts")

    real_run = (
        multi_user.run_customer_action if hasattr(multi_user, "run_customer_action") else None
    )
    from olano.tools import customer_actions

    original = customer_actions.run_customer_action

    def flaky(script, inputs, **kwargs):
        if kwargs["user_id"] == "alice":
            raise OSError("alice's run explodes")
        return original(script, inputs, **kwargs)

    monkeypatch.setattr(customer_actions, "run_customer_action", flaky)
    summary = asyncio.get_event_loop().run_until_complete(
        multi_user.run_for_each_customer("cfg:supportbot:0", config, sched, FakeRunner())
    )
    assert "1 ran" in summary and "1 failed" in summary
    assert [(u) for _, _, u, _ in deliveries] == ["bob"]
    assert real_run is None  # documentation: fan-out imports from customer_actions


def test_fan_out_turn_binds_each_customer(lionclaw_home, tmp_path, deliveries):
    """Message mode: one agent turn per customer, identity bound — inside the
    turn, active_customer_id() is that user, and the reply is delivered."""
    from olano.scheduler.multi_user import run_for_each_customer

    _seed_customer(AGENT, "telegram", "alice")
    _seed_customer(AGENT, "telegram", "bob")
    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(
        cron="0 19 * * 0", for_each_customer=True, message="Draft this user's weekly plan."
    )
    runner = FakeRunner(config=config, reply="your plan")

    summary = asyncio.get_event_loop().run_until_complete(
        run_for_each_customer("cfg:supportbot:0", config, sched, runner)
    )
    assert "2 ran" in summary
    assert sorted(c["customer_id"] for c in runner.calls) == [
        "telegram-alice",
        "telegram-bob",
    ]
    for call in runner.calls:
        assert call["channel"] == "telegram"
        assert call["is_owner"] is False
        assert call["source"] == "scheduled"
    assert sorted(u for _, _, u, _ in deliveries) == ["alice", "bob"]
    assert all(t == "your plan" for *_, t in deliveries)


def test_fan_out_turn_message_substitutes_customer_placeholders(
    lionclaw_home, tmp_path, deliveries
):
    """{customer_key}/{platform}/{user_id} render per customer, so a turn can
    reference owner-keyed tools (kaki_matches) without the model guessing an
    identity. Unknown braces in prose pass through untouched."""
    from olano.scheduler.multi_user import run_for_each_customer

    _seed_customer(AGENT, "telegram", "alice")
    _seed_customer(AGENT, "telegram", "bob")
    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(
        cron="0 9 * * *",
        for_each_customer=True,
        message=(
            'Call kaki_matches(owner="{customer_key}") for the {platform} user '
            "{user_id}. Keep {this} literal."
        ),
    )
    runner = FakeRunner(config=config, reply="digest")
    asyncio.get_event_loop().run_until_complete(
        run_for_each_customer("cfg:supportbot:0", config, sched, runner)
    )
    prompts = {c["user_id"]: c["prompt"] for c in runner.calls}
    assert 'kaki_matches(owner="telegram-alice") for the telegram user alice' in prompts["alice"]
    assert 'kaki_matches(owner="telegram-bob") for the telegram user bob' in prompts["bob"]
    assert all("{this} literal" in p for p in prompts.values())
    assert all("{customer_key}" not in p for p in prompts.values())


def test_no_message_sentinel_suppresses_delivery(lionclaw_home, tmp_path, deliveries):
    """A turn replying exactly NO_MESSAGE sends nothing — quiet-unless-
    actionable digests need an explicit spelling for silence."""
    from olano.scheduler.multi_user import run_for_each_customer

    _seed_customer(AGENT, "telegram", "alice")
    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(cron="0 9 * * *", for_each_customer=True, message="Digest.")
    runner = FakeRunner(config=config, reply="NO_MESSAGE")
    summary = asyncio.get_event_loop().run_until_complete(
        run_for_each_customer("cfg:supportbot:0", config, sched, runner)
    )
    assert "1 ran" in summary
    assert deliveries == []
    # …and the preamble tells the model the sentinel exists.
    assert "NO_MESSAGE" in runner.calls[0]["prompt"]


def test_no_message_sentinel_on_customer_task(lionclaw_home, tmp_path, deliveries):
    db = SchedulerDB(tmp_path / "sched.db")
    task_id = db.create_task(
        name="check",
        prompt="only message me if something changed",
        schedule="0 8 * * *",
        agent_name=AGENT,
        platform="telegram",
        user_id="449281",
    )
    runner = FakeRunner(reply="NO_MESSAGE")
    scheduler = _make_scheduler_with_runner(runner)
    asyncio.get_event_loop().run_until_complete(
        scheduler._execute_db_task(db.get_task(task_id), db)
    )
    assert deliveries == []
    history = db.get_history(task_id)
    assert history and history[0]["result"] == "completed"


def test_fan_out_with_unresolvable_action_runs_nothing(lionclaw_home, tmp_path, deliveries):
    from olano.scheduler.multi_user import run_for_each_customer

    _seed_customer(AGENT, "telegram", "alice")
    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(cron="0 9 * * *", for_each_customer=True, action="ghost")
    summary = asyncio.get_event_loop().run_until_complete(
        run_for_each_customer("cfg:supportbot:0", config, sched, FakeRunner())
    )
    assert "does not resolve" in summary
    assert deliveries == []


def test_fan_out_with_no_customers_is_a_quiet_no_op(lionclaw_home, tmp_path, deliveries):
    from olano.scheduler.multi_user import run_for_each_customer

    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(cron="0 9 * * *", for_each_customer=True, action="alerts")
    summary = asyncio.get_event_loop().run_until_complete(
        run_for_each_customer("cfg:supportbot:0", config, sched, FakeRunner())
    )
    assert "no customers" in summary


# ─────────────────────────── Type-2 firing path ───────────────────────────────


def _make_scheduler_with_runner(runner) -> Any:
    from olano.scheduler.cron import OlanoScheduler

    scheduler = OlanoScheduler()
    scheduler._runners[AGENT] = runner
    return scheduler


def test_customer_task_fires_as_the_owning_user(lionclaw_home, tmp_path, deliveries):
    db = SchedulerDB(tmp_path / "sched.db")
    task_id = db.create_task(
        name="bills",
        prompt="send my bill summary",
        schedule="0 8 * * *",
        agent_name=AGENT,
        platform="telegram",
        user_id="449281",
    )
    runner = FakeRunner(reply="here are your bills")
    scheduler = _make_scheduler_with_runner(runner)
    asyncio.get_event_loop().run_until_complete(
        scheduler._execute_db_task(db.get_task(task_id), db)
    )
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["channel"] == "telegram"
    assert call["user_id"] == "449281"
    assert call["customer_id"] == "telegram-449281"
    assert call["is_owner"] is False
    # Reply delivered back to the owning user on their platform.
    assert deliveries == [(AGENT, "telegram", "449281", "here are your bills")]
    history = db.get_history(task_id)
    assert history and history[0]["error"] is None


def test_lapsed_subscription_skips_the_run_but_keeps_the_task(lionclaw_home, tmp_path, deliveries):
    from olano.monetization.store import (
        EntitlementRecord,
        EntitlementStatus,
        get_entitlement_store,
    )

    config = AgentConfig(
        name=AGENT,
        customer_service={"enabled": True},
        monetization={"enabled": True, "providers": ["stripe"]},
    )
    get_entitlement_store().upsert(
        EntitlementRecord(
            agent=AGENT,
            channel="telegram",
            user_id="449281",
            status=EntitlementStatus.EXPIRED,
        )
    )
    db = SchedulerDB(tmp_path / "sched.db")
    task_id = db.create_task(
        name="bills",
        prompt="p",
        schedule="0 8 * * *",
        agent_name=AGENT,
        platform="telegram",
        user_id="449281",
    )
    runner = FakeRunner(config=config)
    scheduler = _make_scheduler_with_runner(runner)
    asyncio.get_event_loop().run_until_complete(
        scheduler._execute_db_task(db.get_task(task_id), db)
    )
    assert runner.calls == []  # no model turn was spent
    assert deliveries == []
    history = db.get_history(task_id)
    assert history and "subscription inactive" in (history[0]["error"] or "")
    assert db.get_task(task_id)["enabled"] is True  # resumes on resubscribe


# ──────────────── the absolutely-critical invariant: normal mode ──────────────


def test_normal_db_task_still_binds_the_scheduler_context(lionclaw_home, tmp_path):
    """A task with NULL identity dispatches exactly as before: synthetic
    scheduler channel, implicit owner, autonomous preamble."""
    db = SchedulerDB(tmp_path / "sched.db")
    task_id = db.create_task(name="sweep", prompt="run the sweep", schedule="0 7 * * *")
    runner = FakeRunner(reply="swept")
    scheduler = _make_scheduler_with_runner(runner)
    # Normal tasks fall back to the default agent; point it at our runner.
    import olano.scheduler.cron as cron_mod

    task = db.get_task(task_id)
    task["agent_name"] = AGENT
    asyncio.get_event_loop().run_until_complete(scheduler._execute_db_task(task, db))
    call = runner.calls[0]
    assert call["channel"] == "scheduler"
    assert call["user_id"] == "owner"
    assert call["is_owner"] is True
    assert call["customer_id"] is None  # the phase-0 guard, not a phantom
    assert call["prompt"].startswith("[Autonomous scheduled task —")
    history = db.get_history(task_id)
    assert history and history[0]["result"] == "swept"
    assert cron_mod is not None


def test_classic_config_jobs_register_through_the_unchanged_path(lionclaw_home, monkeypatch):
    """An agent with only classic entries never touches the fan-out machinery:
    same job ids, `_add_job` used, no multi-user config retained."""
    from olano.scheduler.cron import OlanoScheduler

    scheduler = OlanoScheduler()
    seen: list[str] = []
    original = OlanoScheduler._add_job

    def spy(self, agent_name, sched, runner, *, index):
        seen.append(f"cfg:{agent_name}:{index}")
        return original(self, agent_name, sched, runner, index=index)

    monkeypatch.setattr(OlanoScheduler, "_add_job", spy)
    monkeypatch.setattr(
        OlanoScheduler,
        "_add_fan_out_job",
        lambda *a, **k: pytest.fail("fan-out path used for a classic entry"),
    )
    config = AgentConfig(
        name="merli",
        schedule=[
            {"cron": "0 7 * * *", "message": "morning"},
            {"cron": "0 19 * * *", "message": "evening"},
        ],
    )
    job_ids = scheduler._register_config_schedule(config, FakeRunner())
    assert job_ids == ["cfg:merli:0", "cfg:merli:1"] == seen
    assert scheduler._multi_user_configs == {}


def test_fan_out_entries_register_beside_the_classic_path(lionclaw_home, tmp_path):
    from olano.scheduler.cron import OlanoScheduler

    scheduler = OlanoScheduler()
    config = _fan_out_config(
        tmp_path,
        schedule=[
            {"cron": "0 7 * * *", "message": "classic"},
            {"cron": "0 9 * * *", "for_each_customer": True, "action": "alerts"},
        ],
    )
    job_ids = scheduler._register_config_schedule(config, FakeRunner())
    assert job_ids == [f"cfg:{AGENT}:0", f"cfg:{AGENT}:1"]
    assert AGENT in scheduler._multi_user_configs
    descriptors = scheduler._config_jobs[AGENT]
    assert descriptors[0]["for_each_customer"] is False
    assert descriptors[1]["for_each_customer"] is True
    assert descriptors[1]["action"] == "alerts"
