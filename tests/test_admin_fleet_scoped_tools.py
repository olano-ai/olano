"""Fleet-scoped admin tools stay AgentFather's, and stay admin-gated.

``olano.log`` is one file for the whole deployment and ``olano_read_session``
reads any agent's transcript, so the log / session / diagnostics block of the
``olano_admin`` toolkit is cross-agent by construction. Two rules keep it from
leaking, and each had a hole:

* **Nothing grants it by default.** Verified against the real default-tool
  resolution rather than by reading the lists — a new entry in any of the
  ``ops_defaults`` / ``internal_tools`` grant paths would otherwise put a
  fleet-wide log reader on every agent.
* **Holding it is not enough — the turn needs an admin.** These tools carried
  no ``@tool_metadata`` at all, so ``ToolMetadata``'s permissive defaults made
  them ``read_only`` / ``owner_only=False``: on any agent that *did* hold them
  a non-admin member, a peer over ``talk_to_agent`` or an inbound MCP/A2A
  caller could tail the fleet's logs.

And the write side: ``expand_toolkit`` refuses the deny-listed toolkits by
name, but ``tools:`` holds individual tool names — writing them straight in
walked around the rule rather than through it.
"""

from __future__ import annotations

import json

import pytest

from olano.admin.integrations import (
    PRIVILEGED_TOOLKITS,
    TOOLKIT_DENYLIST,
    privileged_tool_names,
    toolkit_members,
)
from olano.config.agent_config import AgentConfig
from olano.config.ops_defaults import resolve_effective_tool_names
from olano.security.pipeline import evaluate_tool_access
from olano.security.request_context import push_request_context
from olano.security.tool_metadata import get_tool_metadata
from olano.tools import olano_admin

# The cross-agent read surface: the shared log, other agents' transcripts, and
# the live fleet diagnostics.
FLEET_SCOPED_READS = (
    # Both scheduler stores, for every agent: a job's full prompt says what
    # another agent is being told to do, unattended, on a timer.
    "olano_scheduler_list",
    "olano_scheduler_get",
    "olano_tail_log",
    "olano_recent_errors",
    "olano_agent_errors",
    "olano_list_sessions",
    "olano_read_session",
    "olano_runtime_status",
    "olano_agent_status",
    "olano_mcp_status",
    "olano_agent_activity",
    "olano_diagnose_agent",
    "olano_doctor",
)


def test_no_admin_tool_is_granted_to_a_default_agent():
    """A plain agent resolves its full effective tool set with zero ``olano_*``."""
    cfg = AgentConfig(name="plain", model="fake:instant")
    effective = resolve_effective_tool_names(cfg.tools, cfg.disabled_tools, cfg)

    leaked = sorted(n for n in effective if n.startswith("olano_"))
    assert leaked == [], f"admin tools reached a default agent: {leaked}"


@pytest.mark.parametrize("name", FLEET_SCOPED_READS)
def test_fleet_scoped_reads_are_owner_only(name):
    meta = get_tool_metadata(olano_admin.get_tool(name))
    assert meta.owner_only is True, f"{name} must be owner-only"
    # Read-only keeps them out of the HITL approval categories: an admin
    # diagnosing an outage should not have to approve each `tail_log`.
    assert meta.capability == "read_only", f"{name} must stay read_only"


@pytest.mark.parametrize("name", FLEET_SCOPED_READS)
def test_pipeline_denies_fleet_scoped_reads_to_a_non_owner(name):
    """The metadata is only worth what the pipeline does with it."""
    cfg = AgentConfig(name="agentfather", model="fake:instant")
    meta = get_tool_metadata(olano_admin.get_tool(name))

    with push_request_context(source="api", channel="api", is_owner=False, is_admin=False):
        denied = evaluate_tool_access(name, meta, cfg)
    assert denied.allowed is False
    assert denied.layer == 5

    # An admin (is_owner is user.is_admin on the dashboard, True single-user)
    # keeps every one of them.
    with push_request_context(source="api", channel="api", is_owner=True, is_admin=True):
        allowed = evaluate_tool_access(name, meta, cfg)
    assert allowed.allowed is True


def test_peer_agent_cannot_tail_the_fleet_log():
    """A `talk_to_agent` hop into AgentFather is not an administrator."""
    cfg = AgentConfig(name="agentfather", model="fake:instant")
    meta = get_tool_metadata(olano_admin.get_tool("olano_tail_log"))

    with push_request_context(source="peer", channel="peer", is_owner=False, is_admin=False):
        assert evaluate_tool_access("olano_tail_log", meta, cfg).allowed is False


# ── the write side: tools: cannot route around the toolkit deny-list ─────────


def test_privileged_tool_names_is_the_admin_toolkit_and_nothing_default():
    names = privileged_tool_names()
    assert "olano_tail_log" in names
    assert "olano_run_agent_command" in names
    assert "web_search" not in names
    # The deny-list is the wider set — it also refuses toolkits the builder
    # binds by capability. Those are on EVERY agent's default list, so they
    # must never read as fleet-scoped: a badge on `talk_to_agent`, or a refused
    # edit because it re-lists `cortex_list`, would be wrong on every agent.
    assert PRIVILEGED_TOOLKITS < TOOLKIT_DENYLIST
    cfg = AgentConfig(name="plain", model="fake:instant")
    defaults = set(resolve_effective_tool_names(cfg.tools, cfg.disabled_tools, cfg))
    assert not (names & defaults), sorted(names & defaults)
    # And every privileged toolkit resolves its members from the static index —
    # a stale index would leave the guard covering less than it claims.
    members = toolkit_members()
    for toolkit in PRIVILEGED_TOOLKITS:
        assert members.get(toolkit), f"{toolkit}: no members in the static tool index"
        assert set(members[toolkit]) <= names


def _denial(agent: str, requested, **kw):
    return olano_admin._privileged_tools_denial(agent, {"tools": requested}, **kw)


def test_agent_driven_grant_of_a_fleet_tool_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    with push_request_context(source="peer", channel="peer"):
        denial = _denial("newbie", ["web_search", "olano_tail_log"])
    assert denial is not None
    payload = json.loads(denial)
    assert payload["refused_tools"] == ["olano_tail_log"]


def test_ordinary_tools_are_never_gated(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    with push_request_context(source="peer", channel="peer"):
        assert _denial("newbie", ["web_search", "github_create_issue"]) is None
    assert _denial("newbie", None) is None
    assert olano_admin._privileged_tools_denial("newbie", "not-a-dict", None) is None


def test_every_route_a_tool_name_can_take_is_gated(tmp_path, monkeypatch):
    """`tools` is not the only door: create's `extra` and both writes'
    `subagents[*].tools` carry tool names too, and a subagent's tools ARE the
    agent's tools (it runs inside the parent's graph)."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    guard = olano_admin._privileged_tools_denial
    with push_request_context(source="peer", channel="peer"):
        # create: extra={"tools": [...]}
        assert guard("newbie", {"tools": None}, {"tools": ["olano_read_session"]}) is not None
        # create/update: subagents carrying the tool
        via_sub = {"subagents": [{"name": "helper", "tools": ["olano_tail_log"]}]}
        assert guard("newbie", via_sub) is not None
        # a fresh subagent: every name is new, even on AgentFather itself
        assert guard("agentfather", {"tools": ["olano_tail_log"]}, existing=False) is not None


def test_self_add_subagent_cannot_self_grant_a_fleet_tool(tmp_path, monkeypatch):
    """The self-configuration route: "add yourself a helper with the log reader".

    Staged as the realistic case the ownership gate lets through — a
    multi-user box, a member who OWNS this agent (``agent_admin``) but is not
    a platform admin — so the refusal proves the privileged-tool guard, not
    the ownership gate in front of it.
    """
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    from olano import admin
    from olano.runtime import auth as runtime_auth
    from olano.security.request_context import AGENT_ADMIN_KEY
    from olano.tools import self_config_tools
    from olano.tools.talk_to import current_agent

    monkeypatch.setattr(runtime_auth, "multi_user_enabled", lambda: True)
    admin.create_agent("plain", model="fake:instant", system_prompt="x", tools=["web_search"])
    token = current_agent.set("plain")
    try:
        with push_request_context(
            source="api",
            channel="api",
            user_id="member-1",
            is_owner=False,
            is_admin=False,
            metadata={AGENT_ADMIN_KEY: True},
        ):
            out = self_config_tools.self_add_subagent.invoke(
                {
                    "name": "helper",
                    "description": "d",
                    "system_prompt": "p" * 250,
                    "tools": ["olano_tail_log"],
                }
            )
            # Same member, ordinary tools: the ownership gate is satisfied and
            # nothing else stands in the way — the refusal above was the guard.
            ok = self_config_tools.self_add_subagent.invoke(
                {
                    "name": "helper2",
                    "description": "d",
                    "system_prompt": "p" * 250,
                    "tools": ["web_search"],
                }
            )
    finally:
        current_agent.reset(token)
    payload = json.loads(out)
    assert payload.get("refused_tools") == ["olano_tail_log"]
    assert json.loads(ok).get("added") is True
    names = {s["name"] for s in admin.get_agent("plain").get("subagents") or []}
    assert "helper2" in names
    assert "helper" not in names  # the refused one was never written


def test_doctor_names_the_agents_holding_fleet_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    from olano import admin
    from olano.admin.system import fleet_scoped_tool_holders

    admin.create_agent("plain", model="fake:instant", system_prompt="x", tools=["web_search"])
    admin.create_agent(
        "ops", model="fake:instant", system_prompt="x", tools=["olano_tail_log", "web_search"]
    )
    admin.create_agent("nested", model="fake:instant", system_prompt="x")
    admin.add_subagent(
        "nested", "helper", description="d", system_prompt="p", tools=["olano_read_session"]
    )

    holders = {h["agent"]: h["tools"] for h in fleet_scoped_tool_holders()}
    assert holders == {"ops": ["olano_tail_log"], "nested": ["olano_read_session"]}
    assert "fleet_scoped_tool_holders" in admin.doctor()


def test_admin_human_may_still_grant(tmp_path, monkeypatch):
    """The gate is 'who is asking', not a blanket ban — an operator can wire
    an ops agent up from the dashboard."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    with push_request_context(source="api", channel="dashboard", is_owner=True, is_admin=True):
        assert _denial("newbie", ["olano_tail_log"]) is None


def test_resaving_an_existing_grant_is_not_a_new_grant(tmp_path, monkeypatch):
    """AgentFather's own config lists these, and every dashboard save resends
    the whole list — re-saving must not fail."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    from olano import admin

    admin.create_agent(
        "agentfather",
        model="fake:instant",
        system_prompt="x",
        tools=["olano_tail_log", "web_search"],
    )
    with push_request_context(source="peer", channel="peer"):
        assert _denial("agentfather", ["olano_tail_log", "web_search"]) is None
        # …but adding a *second* one on the same turn is still a new grant.
        assert _denial("agentfather", ["olano_tail_log", "olano_read_session"]) is not None
