"""Org-chart feature: OrgConfig semantics, talk_to_agent governance, endpoint.

Covers the three relationship layers the org chart composes:
- OrgConfig allow-list + hierarchy rule methods.
- talk_to_agent enforcement of those allow-lists (both ends must agree;
  hierarchy neighbours bypass; empty lists stay unrestricted).
- GET /api/org-chart graph assembly (agents, hierarchy, talk edges, humans).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.agent_config import AgentConfig, OrgConfig
from olano.runtime.api import create_api
from olano.tools import talk_to as tt

# ───────────────────────────── OrgConfig semantics ──────────────────────────


def test_orgconfig_empty_is_unrestricted():
    org = OrgConfig()
    assert org.allows_output("anyone")
    assert org.allows_input("anyone")


def test_orgconfig_output_allowlist_is_case_insensitive():
    org = OrgConfig(output_agents=["finance", "Legal"])
    assert org.allows_output("finance")
    assert org.allows_output("LEGAL")
    assert not org.allows_output("random")


def test_orgconfig_input_allowlist():
    org = OrgConfig(input_agents=["mgr"])
    assert org.allows_input("mgr")
    assert not org.allows_input("stranger")


def test_orgconfig_hierarchy_neighbours_bypass_allowlists():
    org = OrgConfig(
        output_agents=["finance"], input_agents=["ceo"], manager="Boss", reports=["Writer"]
    )
    # Manager + reports are always reachable / accepted, regardless of the lists.
    assert org.allows_output("boss")  # manager, case-insensitive
    assert org.allows_output("writer")  # report
    assert org.allows_input("boss")
    assert org.allows_input("writer")
    # Anything else still obeys the explicit lists.
    assert org.allows_output("finance")
    assert not org.allows_output("stranger")


def test_agentconfig_default_org_is_unrestricted():
    cfg = AgentConfig(name="x")
    assert cfg.org.allows_output("y")
    assert cfg.org.allows_input("y")


def test_agentconfig_parses_org_block():
    cfg = AgentConfig(
        name="mgr",
        org={"title": "Marketing Manager", "reports": ["writer"], "output_agents": ["finance"]},
    )
    assert cfg.org.title == "Marketing Manager"
    assert cfg.org.reports == ["writer"]
    assert cfg.org.allows_output("finance")


# ─────────────────────── tri-state modes (everyone/none/list) ────────────────


def test_effective_mode_inference_is_backcompat():
    assert OrgConfig().effective_mode("input") == "everyone"
    assert OrgConfig().effective_mode("output") == "everyone"
    assert OrgConfig(input_agents=["a"]).effective_mode("input") == "list"
    assert OrgConfig(output_mode="none").effective_mode("output") == "none"


def test_backcompat_listonly_behaviour_unchanged():
    # A legacy config with only a list (no mode) behaves exactly as before.
    org = OrgConfig(output_agents=["finance"])
    assert org.allows_output("finance")
    assert not org.allows_output("random")


def test_none_mode_blocks_all_but_hierarchy():
    org = OrgConfig(input_mode="none", manager="boss", reports=["writer"])
    assert org.allows_input("boss")  # manager line always passes
    assert org.allows_input("writer")  # report line always passes
    assert not org.allows_input("stranger")


def test_everyone_mode_overrides_stale_list():
    org = OrgConfig(output_mode="everyone", output_agents=["x"])
    assert org.allows_output("anyone")


def test_autogenerate_derives_from_org_structure():
    org = OrgConfig(
        autogenerate=True,
        manager="boss",
        reports=["writer"],
        coworkers=["peer"],
        output_agents=["ignored"],  # manual list is ignored when autogenerate
    )
    assert org.allows_output("boss")
    assert org.allows_output("writer")
    assert org.allows_output("peer")
    assert not org.allows_output("ignored")
    assert not org.allows_output("stranger")


# ──────────────────────────── talk_to governance ────────────────────────────


@dataclass
class _Cfg:
    name: str
    org: OrgConfig = field(default_factory=OrgConfig)
    description: str = "peer"
    emoji: str = "🦁"
    tools: list = field(default_factory=lambda: ["talk_to_agent"])


class _Runner:
    def __init__(self, name: str, org: OrgConfig | None = None) -> None:
        self.config = _Cfg(name=name, org=org or OrgConfig())
        self.calls: list[tuple[str, str | None]] = []

    async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
        self.calls.append((message, thread_id))
        return f"OK from {self.config.name}"


class _Runtime:
    def __init__(self, runners: dict[str, _Runner]) -> None:
        self._runners = runners


@pytest.fixture(autouse=True)
def _reset_talk_to():
    tt._RUNTIME = None
    tt._PUBLISHER = None
    tt._pair_links.clear()
    tt._inflight.clear()
    tt._completed.clear()
    tt.current_agent.set("")
    yield
    tt._RUNTIME = None
    tt._PUBLISHER = None
    tt._pair_links.clear()
    tt._inflight.clear()
    tt._completed.clear()
    tt.current_agent.set("")


async def _say(sender: str, target: str, runtime: _Runtime) -> str:
    tt._RUNTIME = runtime
    tt.current_agent.set(sender)
    return await tt.talk_to_agent.ainvoke({"agent_name": target, "message": "hi"})


async def test_talk_allowed_when_unrestricted():
    a, b = _Runner("a"), _Runner("b")
    reply = await _say("a", "b", _Runtime({"a": a, "b": b}))
    assert reply == "OK from b"
    assert b.calls  # dispatched


async def test_talk_blocked_by_sender_output_allowlist():
    a = _Runner("a", org=OrgConfig(output_agents=["c"]))
    b = _Runner("b")
    reply = await _say("a", "b", _Runtime({"a": a, "b": b}))
    assert reply.startswith("[talk_to_agent error]")
    assert "Org policy" in reply
    assert not b.calls  # never reached the peer


async def test_talk_blocked_by_target_input_allowlist():
    a = _Runner("a")
    b = _Runner("b", org=OrgConfig(input_agents=["c"]))
    reply = await _say("a", "b", _Runtime({"a": a, "b": b}))
    assert reply.startswith("[talk_to_agent error]")
    assert not b.calls


async def test_talk_none_mode_blocks_non_hierarchy():
    a = _Runner("a")
    b = _Runner("b", org=OrgConfig(input_mode="none"))
    reply = await _say("a", "b", _Runtime({"a": a, "b": b}))
    assert reply.startswith("[talk_to_agent error]")
    assert not b.calls


async def test_talk_none_mode_allows_manager():
    # `b` accepts from no one — except its hierarchy line (`a` is its manager).
    a = _Runner("a")
    b = _Runner("b", org=OrgConfig(input_mode="none", manager="a"))
    reply = await _say("a", "b", _Runtime({"a": a, "b": b}))
    assert reply == "OK from b"
    assert b.calls


async def test_talk_hierarchy_bypasses_output_allowlist():
    # `a` may normally only message `c`, but `b` is a's direct report → allowed.
    a = _Runner("a", org=OrgConfig(output_agents=["c"], reports=["b"]))
    b = _Runner("b")
    reply = await _say("a", "b", _Runtime({"a": a, "b": b}))
    assert reply == "OK from b"
    assert b.calls


async def test_can_message_helper():
    a = _Runner("a", org=OrgConfig(output_agents=["b"]))
    b, c = _Runner("b"), _Runner("c")
    tt._RUNTIME = _Runtime({"a": a, "b": b, "c": c})
    assert tt.can_message("a", "b") is True
    assert tt.can_message("a", "c") is False


# ────────────────────────────── /api/org-chart ──────────────────────────────


def _api_runner(cfg: AgentConfig) -> MagicMock:
    r = MagicMock()
    r.config = cfg
    r.ainvoke = AsyncMock(return_value="ok")
    return r


@pytest.fixture
def org_app(lionclaw_home):
    # lionclaw_home isolates LIONCLAW_HOME to a tmp dir so list_agents() (and the
    # central org store) don't leak real on-disk agents into the chart.
    runtime = MagicMock()
    runtime._runners = {
        "boss": _api_runner(
            AgentConfig(name="boss", org={"title": "Manager", "reports": ["worker"]})
        ),
        "worker": _api_runner(
            AgentConfig(name="worker", org={"manager": "boss", "output_agents": ["finance"]})
        ),
        "finance": _api_runner(AgentConfig(name="finance")),
    }
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return create_api(runtime)


def test_org_chart_assembles_graph(org_app):
    client = TestClient(org_app)
    data = client.get("/api/org-chart").json()

    names = {a["name"] for a in data["agents"]}
    assert {"boss", "worker", "finance"} <= names

    worker = next(a for a in data["agents"] if a["name"] == "worker")
    assert worker["manager"] == "boss"
    assert worker["output_agents"] == ["finance"]

    # Hierarchy edge boss → worker (from both reports + manager sides, deduped).
    assert {"manager": "boss", "report": "worker"} in data["edges"]["manager"]
    # Explicit talk edge worker → finance from the output allow-list.
    assert {"from": "worker", "to": "finance"} in data["edges"]["talk"]


def test_org_chart_reports_each_agents_connectors(lionclaw_home):
    """Every agent record carries its configured connectors with a live status,
    resolved from the runtime's in-memory adapter bookkeeping."""
    from types import SimpleNamespace

    runtime = MagicMock()
    runtime._runners = {
        "support": _api_runner(
            AgentConfig(
                name="support",
                channels=["platform:whatsapp", {"type": "platform:telegram"}, "slack"],
            )
        ),
        "quiet": _api_runner(AgentConfig(name="quiet")),
    }
    runtime._agent_resources = {
        "support": {
            "channels": [],
            "platforms": [
                SimpleNamespace(
                    platform=SimpleNamespace(value="whatsapp"),
                    is_connected=True,
                    fatal_error_message=None,
                ),
                SimpleNamespace(
                    platform=SimpleNamespace(value="telegram"),
                    is_connected=False,
                    fatal_error_message=None,
                ),
            ],
        },
        "quiet": {"channels": [], "platforms": []},
    }
    runtime._skipped_channels = {"support": {"slack": "Set SLACK_BOT_TOKEN in the vault."}}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    data = TestClient(create_api(runtime)).get("/api/org-chart").json()

    by_name = {a["name"]: a for a in data["agents"]}
    assert by_name["support"]["channels"] == [
        {"type": "platform:whatsapp", "status": "connected", "detail": None},
        {"type": "platform:telegram", "status": "connecting", "detail": None},
        {"type": "slack", "status": "error", "detail": "Set SLACK_BOT_TOKEN in the vault."},
    ]
    assert by_name["quiet"]["channels"] == []


def test_org_chart_excludes_hidden_agents(lionclaw_home):
    """`hidden: true` agents (internal/test agents) never chart — the record is
    skipped and any edges touching them drop out with it."""
    runtime = MagicMock()
    runtime._runners = {
        "boss": _api_runner(AgentConfig(name="boss", org={"reports": ["ghost"]})),
        "ghost": _api_runner(
            AgentConfig(
                name="ghost", hidden=True, org={"manager": "boss", "output_agents": ["boss"]}
            )
        ),
    }
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    data = TestClient(create_api(runtime)).get("/api/org-chart").json()

    names = {a["name"] for a in data["agents"]}
    assert "boss" in names
    assert "ghost" not in names
    for e in data["edges"]["manager"]:
        assert "ghost" not in (e["manager"], e["report"])
    for e in data["edges"]["talk"]:
        assert "ghost" not in (e["from"], e["to"])
    # The implicit single-user owner's access grants skip it too.
    for h in data["humans"]:
        assert all(g["agent"] != "ghost" for g in h["agents"])


def test_org_chart_single_user_has_owner_human(org_app):
    client = TestClient(org_app)
    data = client.get("/api/org-chart").json()
    assert data["multi_user"] is False
    assert len(data["humans"]) == 1
    owner = data["humans"][0]
    assert owner["is_admin"] is True
    # The implicit owner has access to every visible agent (subset check — the
    # repo ships extra committed agents under ./agents that list_agents() picks up).
    assert {"boss", "worker", "finance"} <= {g["agent"] for g in owner["agents"]}


def test_org_chart_exposes_modes_and_coworker_edges(lionclaw_home):
    runtime = MagicMock()
    runtime._runners = {
        "a": _api_runner(AgentConfig(name="a", org={"coworkers": ["b"], "input_agents": ["b"]})),
        "b": _api_runner(AgentConfig(name="b")),
    }
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    data = TestClient(create_api(runtime)).get("/api/org-chart").json()

    a = next(x for x in data["agents"] if x["name"] == "a")
    assert a["input_mode"] == "list"  # has an input allow-list
    assert a["output_mode"] == "everyone"  # nothing set ⇒ everyone
    assert a["coworkers"] == ["b"]
    assert a["autogenerate"] is False
    # Co-worker edge is undirected/deduped.
    assert {"a": "a", "b": "b"} in data["edges"]["coworker"]


# ─────────────── admin implicit access edges (multi-user mode) ───────────────


def _three_agent_runtime() -> MagicMock:
    runtime = MagicMock()
    runtime._runners = {
        "boss": _api_runner(AgentConfig(name="boss")),
        "worker": _api_runner(AgentConfig(name="worker")),
        "finance": _api_runner(AgentConfig(name="finance")),
    }
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return runtime


def _admin_client(store, admin) -> TestClient:
    """A TestClient authenticated as ``admin`` against a real multi-user runtime."""
    from olano.runtime import auth as auth_layer

    auth_layer.set_multi_user_enabled(True)
    client = TestClient(create_api(_three_agent_runtime()))
    client.cookies.set(auth_layer.SESSION_COOKIE, store.issue_session(admin))
    return client


def test_org_chart_admin_reaches_every_agent(lionclaw_home):
    """Admins implicitly reach every agent (users.store.can_chat / list_agent_names),
    so the chart draws an access edge to each one even with no explicit agent_access
    grants. Owners/members keep only their explicit grants."""
    from olano.users.store import get_user_store, reset_user_store

    reset_user_store()
    store = get_user_store()
    admin = store.create_user(email="admin@x.com", password="pw", name="Carlos", is_admin=True)
    owner = store.create_user(email="owner@x.com", password="pw", name="Emily")
    store.db.grant_access(owner.id, "boss", role="owner")  # owns exactly one agent

    data = _admin_client(store, admin).get("/api/org-chart").json()
    assert data["multi_user"] is True

    visible = {a["name"] for a in data["agents"]}
    assert {"boss", "worker", "finance"} <= visible

    humans = {h["id"]: h for h in data["humans"]}
    admin_rec = humans[admin.id]
    assert admin_rec["is_admin"] is True
    # Reaches *every* visible agent, all defaulting to the "admin" role.
    assert {g["agent"] for g in admin_rec["agents"]} == visible
    assert {g["role"] for g in admin_rec["agents"]} == {"admin"}
    admin_edges = {e["agent"] for e in data["edges"]["access"] if e["user_id"] == admin.id}
    assert admin_edges == visible

    # The non-admin owner connects only to the single agent they own.
    owner_rec = humans[owner.id]
    assert owner_rec["is_admin"] is False
    assert [(g["agent"], g["role"]) for g in owner_rec["agents"]] == [("boss", "owner")]
    owner_edges = [e for e in data["edges"]["access"] if e["user_id"] == owner.id]
    assert owner_edges == [{"user_id": owner.id, "agent": "boss", "role": "owner"}]


def test_org_chart_admin_keeps_explicit_owner_role(lionclaw_home):
    """An admin who also explicitly owns an agent keeps the owner role on that edge
    while still reaching everything else as 'admin'."""
    from olano.users.store import get_user_store, reset_user_store

    reset_user_store()
    store = get_user_store()
    admin = store.create_user(email="admin@x.com", password="pw", is_admin=True)
    store.db.grant_access(admin.id, "boss", role="owner")  # explicit ownership of boss

    data = _admin_client(store, admin).get("/api/org-chart").json()
    visible = {a["name"] for a in data["agents"]}
    admin_rec = {h["id"]: h for h in data["humans"]}[admin.id]
    roles = {g["agent"]: g["role"] for g in admin_rec["agents"]}
    assert set(roles) == visible  # still reaches all
    assert roles["boss"] == "owner"  # explicit grant preserved
    assert roles["worker"] == "admin"  # implicit admin access
    assert roles["finance"] == "admin"
