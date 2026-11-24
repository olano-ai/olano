"""Tests for `default_internal_tools` — the master switch over everything
olano grants without a `tools:` entry.

The marketplace posture this enables: deny by default, then grant
tool-by-tool via `tools:`, with the multi-user identity-injected set (which
rides its own explicit opt-ins) unaffected.
"""

from __future__ import annotations

from pathlib import Path

from olano.config.agent_config import AgentConfig
from olano.config.internal_tools import effective_internal_tools
from olano.config.ops_defaults import UNIVERSAL_AGENT_TOOL_NAMES
from olano.core.builder import _build_extra_tools

AGENT = "lockedbot"


def _names(tools) -> set[str]:
    return {str(getattr(t, "name", "")) for t in tools}


# ───────────────────────────── config-level merge ─────────────────────────────


def test_default_true_keeps_todays_injection():
    cfg = AgentConfig(name=AGENT, tools=["get_current_time"])
    assert set(UNIVERSAL_AGENT_TOOL_NAMES) <= set(cfg.tools)
    # The ops tools are universal defaults; OpsManager is no longer implicit.
    assert "scheduler_create" in cfg.tools
    assert not any(s.name == "OpsManager" for s in cfg.subagents)


def test_knob_off_binds_exactly_the_explicit_tools():
    cfg = AgentConfig(
        name=AGENT, default_internal_tools=False, tools=["get_current_time", "whoami"]
    )
    assert cfg.tools == ["get_current_time", "whoami"]
    assert not any(s.name == "OpsManager" for s in cfg.subagents)
    assert cfg.builtin_subagents == []


def test_knob_off_still_honors_an_explicit_builtin_subagents():
    cfg = AgentConfig(name=AGENT, default_internal_tools=False, builtin_subagents=["ops"])
    assert any(s.name == "OpsManager" for s in cfg.subagents)


def test_knob_off_disabled_tools_still_filters_explicit_entries():
    cfg = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        tools=["get_current_time", "whoami"],
        disabled_tools=["whoami"],
    )
    assert cfg.tools == ["get_current_time"]


# ───────────────────────────── builder-level grants ───────────────────────────


def test_knob_off_drops_every_implicit_builder_grant(tmp_path: Path):
    """Shared memory, notes, graph, workspace_context and whoami all vanish;
    nothing sensitive is bound implicitly."""
    on = AgentConfig(name=AGENT, tools=[])
    off = AgentConfig(name=AGENT, default_internal_tools=False, tools=[])

    names_on = _names(_build_extra_tools(on, tmp_path))
    names_off = _names(_build_extra_tools(off, tmp_path))

    sensitive = {
        "memory_save",
        "memory_search",
        "notes_add",
        "notes_list",
        "graph_add_entity",
        "graph_search_entities",
        "workspace_context",
        "whoami",
        "magic_link_connect_service",
        "magic_link_request_secret",
    }
    assert sensitive <= names_on  # the default really is this broad
    assert not (sensitive & names_off)
    # Nothing else sneaks in: with no explicit tools and no opt-in modes the
    # locked agent binds NOTHING from the builder.
    assert names_off == set()


def test_knob_off_keeps_the_multi_user_identity_injected_set(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "act.py").write_text("print('{}')", encoding="utf-8")
    cfg = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        tools=[],
        customer_service={"enabled": True, "user_scheduling": True},
        monetization={"enabled": True, "providers": ["stripe"]},
        allowed_actions={"act": "act.py"},
    )
    names = _names(_build_extra_tools(cfg, ws))
    assert {
        "remember",
        "recall",
        "recall_all",
        "forget_me",
        "knowledge_search",
        "execute_action",
        "read_customer_file",
        "write_customer_file",
        "list_customer_files",
        "subscription_status",
        "schedule_my_task",
        "list_my_scheduled_tasks",
        "cancel_my_scheduled_task",
    } <= names
    # …and still none of the implicit grants.
    assert not ({"notes_add", "graph_summary", "memory_save", "whoami"} & names)


def test_knob_off_whoami_comes_back_when_explicitly_listed(tmp_path: Path):
    cfg = AgentConfig(name=AGENT, default_internal_tools=False, tools=["whoami"])
    assert "whoami" in _names(_build_extra_tools(cfg, tmp_path))


def test_knob_off_honors_an_explicit_memory_pin(tmp_path: Path):
    """A non-empty top_level_memory_tools pin is the operator naming tools —
    it survives the knob and still narrows to exactly the pinned names."""
    cfg = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        tools=[],
        top_level_memory_tools=["memory_save", "memory_search"],
    )
    names = _names(_build_extra_tools(cfg, tmp_path))
    assert {"memory_save", "memory_search"} <= names
    # The pin is an allow-list, not a floodgate: unpinned memory tools stay off,
    # and the other implicit grants stay off too.
    assert "memory_append_daily" not in names
    assert not ({"notes_add", "graph_summary", "workspace_context", "whoami"} & names)


def test_knob_off_honors_an_explicit_graph_pin(tmp_path: Path):
    cfg = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        tools=[],
        top_level_graph_tools=["graph_summary"],
    )
    names = _names(_build_extra_tools(cfg, tmp_path))
    assert "graph_summary" in names
    assert "graph_add_entity" not in names
    assert not ({"memory_save", "notes_add", "workspace_context"} & names)


def test_knob_off_honors_an_explicitly_written_workspace_context(tmp_path: Path):
    """The field defaults to True, so only a value the config actually set
    counts as an explicit grant under the knob."""
    explicit = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        tools=[],
        top_level_workspace_context=True,
    )
    implicit = AgentConfig(name=AGENT, default_internal_tools=False, tools=[])
    assert "workspace_context" in _names(_build_extra_tools(explicit, tmp_path))
    assert "workspace_context" not in _names(_build_extra_tools(implicit, tmp_path))


def test_knob_off_empty_pin_grants_nothing(tmp_path: Path):
    cfg = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        tools=[],
        top_level_memory_tools=[],
        top_level_graph_tools=[],
        top_level_workspace_context=False,
    )
    assert _names(_build_extra_tools(cfg, tmp_path)) == set()


# ───────────────────────────── catalog reporting ──────────────────────────────


def test_catalog_reflects_the_knob():
    off = AgentConfig(name=AGENT, default_internal_tools=False, tools=["notification_send_text"])
    rows = {r["name"]: r for r in effective_internal_tools(off)}
    # A universal default is off with an actionable note…
    assert rows["web_search"]["on"] is False
    assert "default_internal_tools" in rows["web_search"]["note"]
    # …unless explicitly reclaimed in tools:.
    assert rows["notification_send_text"]["on"] is True
    # Implicit builder grants report off too.
    for name in ("notes_add", "graph_summary", "memory_save", "workspace_context", "whoami"):
        assert rows[name]["on"] is False, name


def test_catalog_reflects_explicit_pins_under_the_knob():
    cfg = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        top_level_memory_tools=["memory_save"],
        top_level_graph_tools=["graph_summary"],
        top_level_workspace_context=True,
    )
    rows = {r["name"]: r for r in effective_internal_tools(cfg)}
    assert rows["memory_save"]["on"] is True
    assert rows["memory_search"]["on"] is False  # excluded by the pin
    assert rows["graph_summary"]["on"] is True
    assert rows["workspace_context"]["on"] is True
    # Notes have no pin field — they stay a pure default grant.
    assert rows["notes_add"]["on"] is False


def test_catalog_reports_the_multi_user_rows():
    cfg = AgentConfig(
        name=AGENT,
        default_internal_tools=False,
        customer_service={"enabled": True, "user_scheduling": True},
        monetization={"enabled": True, "providers": ["stripe"]},
        allowed_actions={"act": "x.py"},
    )
    rows = {r["name"]: r for r in effective_internal_tools(cfg)}
    for name in (
        "execute_action",
        "read_customer_file",
        "subscription_status",
        "schedule_my_task",
    ):
        assert rows[name]["on"] is True, name
    plain = AgentConfig(name="plain")
    plain_rows = {r["name"]: r for r in effective_internal_tools(plain)}
    assert plain_rows["execute_action"]["on"] is False
    assert "Multi-user" in plain_rows["execute_action"]["note"]
