"""Tests for per-customer delivery preferences (subscriber mute + operator
block) and for the memory-pin scope fix: ``top_level_memory_tools`` governs
the per-customer MEMORY/knowledge tools only — it must never strip the rest
of the multi-user set (files, actions, subscription, scheduling, mute), which
is exactly the accident that once left the reference agent with no
``forget_me`` and no ``execute_action`` at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from olano.config.agent_config import AgentConfig, ScheduleConfig
from olano.memory.customer_memory import (
    active_customer_id,
    customer_key_for,
    customers_base,
    make_customer_memory_tools,
)
from olano.memory.customer_prefs import (
    delivery_suppressed,
    is_blocked,
    is_muted,
    make_customer_pref_tools,
    read_prefs,
    set_blocked,
)
from olano.security.request_context import push_request_context

AGENT = "supportbot"


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _seed_customer(agent: str, platform: str, user: str) -> Path:
    with push_request_context(source="channel", channel=platform, user_id=user):
        tools = make_customer_memory_tools(agent)
        _tool(tools, "remember").invoke({"fact": "seeded"})
        cid = active_customer_id()
    return customers_base(agent) / cid


class FakeRunner:
    def __init__(self, config: AgentConfig | None = None, reply: str = "done") -> None:
        self.config = config
        self.reply = reply
        self.calls: list[str] = []

    async def ainvoke(self, prompt: str, thread_id: str | None = None) -> str:
        self.calls.append(str(active_customer_id()))
        return self.reply


# ─────────────────────────── the mute/resume tools ───────────────────────────


def test_mute_and_resume_round_trip(lionclaw_home):
    folder = _seed_customer(AGENT, "telegram", "alice")
    tools = make_customer_pref_tools(AGENT)
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(tools, "mute_my_notifications").invoke({})
    assert "paused" in out
    assert is_muted(folder) and delivery_suppressed(folder) == "muted"
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        out = _tool(tools, "resume_my_notifications").invoke({})
    assert "back on" in out
    assert not is_muted(folder) and delivery_suppressed(folder) is None
    # The audit trail survives the round trip.
    prefs = read_prefs(folder)
    assert prefs["muted_at"] and prefs["resumed_at"]


def test_pref_tools_refuse_without_an_identified_customer(lionclaw_home):
    tools = make_customer_pref_tools(AGENT)
    with push_request_context(source="scheduled", channel="scheduler", authenticated=True):
        assert "No identified customer" in _tool(tools, "mute_my_notifications").invoke({})
    assert not customers_base(AGENT).exists()


def test_blocked_is_operator_only(lionclaw_home):
    """set_blocked works on existing folders; no model tool can touch it."""
    assert set_blocked(AGENT, "telegram-ghost", True) is False  # never contacted
    folder = _seed_customer(AGENT, "telegram", "alice")
    assert set_blocked(AGENT, folder.name, True) is True
    assert is_blocked(folder) and delivery_suppressed(folder) == "blocked"
    # The customer's own tools cannot lift it: resume touches only `muted`.
    tools = make_customer_pref_tools(AGENT)
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        _tool(tools, "resume_my_notifications").invoke({})
    assert is_blocked(folder)
    assert set_blocked(AGENT, folder.name, False) is True
    assert not is_blocked(folder)


# ──────────────────────── scheduled work honors the flags ─────────────────────

FAN_ACTION = 'import json,sys; json.load(sys.stdin); print(json.dumps({"notify": "hi"}))\n'


def _fan_out_config(tmp_path: Path) -> AgentConfig:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "fan.py").write_text(FAN_ACTION, encoding="utf-8")
    return AgentConfig(
        name=AGENT,
        workspace=str(ws),
        customer_service={"enabled": True},
        allowed_actions={"alerts": "fan.py"},
    )


@pytest.fixture
def deliveries(monkeypatch):
    from olano.scheduler import multi_user

    sent: list[tuple[str, str, str, str]] = []

    async def fake_deliver(agent_name, platform, user_id, text):
        sent.append((agent_name, platform, user_id, text))
        return True

    monkeypatch.setattr(multi_user, "deliver_text", fake_deliver)
    return sent


def test_fan_out_action_skips_muted_and_blocked(lionclaw_home, tmp_path, deliveries):
    from olano.scheduler.multi_user import run_for_each_customer

    _seed_customer(AGENT, "telegram", "alice")
    muted = _seed_customer(AGENT, "telegram", "bob")
    blocked = _seed_customer(AGENT, "telegram", "carol")
    tools = make_customer_pref_tools(AGENT)
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        _tool(tools, "mute_my_notifications").invoke({})
    assert set_blocked(AGENT, blocked.name, True)

    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(cron="0 9 * * *", for_each_customer=True, action="alerts")
    summary = asyncio.get_event_loop().run_until_complete(
        run_for_each_customer("cfg:supportbot:0", config, sched, FakeRunner())
    )
    assert "1 ran" in summary and "2 skipped" in summary
    assert [u for _, _, u, _ in deliveries] == ["alice"]
    # No action even ran for the muted/blocked folders.
    assert not (muted / "ran.txt").exists() and not (blocked / "ran.txt").exists()


def test_fan_out_turn_skips_muted_without_a_model_turn(lionclaw_home, tmp_path, deliveries):
    from olano.scheduler.multi_user import run_for_each_customer

    _seed_customer(AGENT, "telegram", "alice")
    _seed_customer(AGENT, "telegram", "bob")
    tools = make_customer_pref_tools(AGENT)
    with push_request_context(source="channel", channel="telegram", user_id="bob"):
        _tool(tools, "mute_my_notifications").invoke({})

    config = _fan_out_config(tmp_path)
    sched = ScheduleConfig(cron="0 9 * * *", for_each_customer=True, message="weekly plan")
    runner = FakeRunner(config=config, reply="your plan")
    summary = asyncio.get_event_loop().run_until_complete(
        run_for_each_customer("cfg:supportbot:0", config, sched, runner)
    )
    assert "1 ran" in summary and "1 skipped" in summary
    assert runner.calls == ["telegram-alice"]
    assert [u for _, _, u, _ in deliveries] == ["alice"]


def test_customer_task_turn_skips_muted(lionclaw_home, tmp_path):
    """Type-2 (customer-owned) tasks respect the mute at fire time."""
    from olano.scheduler.multi_user import CustomerTaskSkipped, run_customer_task_turn

    _seed_customer(AGENT, "telegram", "alice")
    tools = make_customer_pref_tools(AGENT)
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        _tool(tools, "mute_my_notifications").invoke({})

    runner = FakeRunner(config=_fan_out_config(tmp_path))
    with pytest.raises(CustomerTaskSkipped, match="muted"):
        asyncio.get_event_loop().run_until_complete(
            run_customer_task_turn(runner, AGENT, "telegram", "alice", "p", "t1", "task-1")
        )
    assert runner.calls == []


def test_deliver_text_is_the_last_line_of_defense(lionclaw_home, monkeypatch):
    """Any future delivery caller inherits the suppression at the boundary."""
    from olano.scheduler.multi_user import deliver_text

    folder = _seed_customer(AGENT, "telegram", "alice")
    sent: list[Any] = []

    class FakeRegistry:
        def get(self, agent, platform):
            return object()

        async def send(self, envelope, raise_on_error=True):
            sent.append(envelope)
            return True

    import olano.platforms as platforms

    monkeypatch.setattr(platforms, "get_channel_registry", lambda: FakeRegistry())

    tools = make_customer_pref_tools(AGENT)
    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        _tool(tools, "mute_my_notifications").invoke({})
    ok = asyncio.get_event_loop().run_until_complete(deliver_text(AGENT, "telegram", "alice", "hi"))
    assert ok is False and sent == []

    with push_request_context(source="channel", channel="telegram", user_id="alice"):
        _tool(tools, "resume_my_notifications").invoke({})
    ok = asyncio.get_event_loop().run_until_complete(deliver_text(AGENT, "telegram", "alice", "hi"))
    assert ok is True and len(sent) == 1
    # Sanity: the key derivation the boundary check uses names the real folder.
    assert customer_key_for("telegram", "alice") == folder.name


# ─────────────────── the memory pin no longer strips the MU set ───────────────


def _names(tools) -> set[str]:
    return {str(getattr(t, "name", "")) for t in tools}


_MU_NON_MEMORY = {
    "read_customer_file",
    "write_customer_file",
    "list_customer_files",
    "execute_action",
    "mute_my_notifications",
    "resume_my_notifications",
}


def test_empty_memory_pin_keeps_the_rest_of_the_multi_user_set(lionclaw_home, tmp_path):
    from olano.core.builder import _build_extra_tools

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "act.py").write_text("print('{}')", encoding="utf-8")
    cfg = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        tools=[],
        customer_service={"enabled": True, "user_scheduling": True},
        allowed_actions={"act": "act.py"},
        top_level_memory_tools=[],
    )
    names = _names(_build_extra_tools(cfg, ws))
    assert _MU_NON_MEMORY | {"schedule_my_task"} <= names
    # The pin still does its one job: no memory/knowledge tools.
    assert not ({"remember", "recall", "recall_all", "forget_me", "knowledge_search"} & names)


def test_standard_memory_pin_composes_with_the_multi_user_set(lionclaw_home, tmp_path):
    from olano.core.builder import _build_extra_tools

    cfg = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        tools=[],
        customer_service={"enabled": True},
        top_level_memory_tools=["remember", "recall", "recall_all", "forget_me"],
    )
    names = _names(_build_extra_tools(cfg, tmp_path))
    assert {"remember", "recall", "recall_all", "forget_me"} <= names
    assert {"read_customer_file", "mute_my_notifications"} <= names
    assert "knowledge_search" not in names  # excluded by the pin, as written


def test_disabled_tools_still_drops_a_multi_user_tool(lionclaw_home, tmp_path):
    """disabled_tools is the one per-agent way to drop a pin-exempt tool."""
    from olano.core.builder import _build_extra_tools

    cfg = AgentConfig(
        name=AGENT,
        customer_service={"enabled": True},
        disabled_tools=["mute_my_notifications"],
    )
    names = _names(_build_extra_tools(cfg, tmp_path))
    assert "mute_my_notifications" not in names
    assert "resume_my_notifications" in names


def test_catalog_reports_the_mute_switches(lionclaw_home):
    from olano.config.internal_tools import effective_internal_tools

    mu = AgentConfig(name=AGENT, customer_service={"enabled": True})
    rows = {r["name"]: r for r in effective_internal_tools(mu)}
    assert rows["mute_my_notifications"]["on"] is True
    assert rows["resume_my_notifications"]["on"] is True
    plain = AgentConfig(name="plain")
    rows = {r["name"]: r for r in effective_internal_tools(plain)}
    assert rows["mute_my_notifications"]["on"] is False
