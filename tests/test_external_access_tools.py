"""External access from the agent's side: the admin-human gate, the admin and
self tools that flip an agent's exposure, and the token-minting magic link."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.config.agent_config import AgentConfig
from olano.config.loader import save_agent_config
from olano.security.request_context import ORIGIN_KEY, make_origin, push_request_context
from olano.tools._human_gate import admin_human_denial


@pytest.fixture
def agent(lionclaw_home: Path) -> str:
    save_agent_config(AgentConfig(name="merli", description="roster text"))
    return "merli"


def _dashboard_admin(**over):
    kw = {
        "source": "dashboard",
        "channel": "dashboard",
        "user_id": "u-admin",
        "authenticated": True,
        "is_owner": True,
        "is_admin": True,
        "metadata": {ORIGIN_KEY: make_origin("human", True, root_user_id="u-admin")},
    }
    kw.update(over)
    return push_request_context(**kw)


# ── the gate ──────────────────────────────────────────────────────────────────


class TestAdminHumanGate:
    def test_dashboard_admin_passes(self):
        with _dashboard_admin():
            assert admin_human_denial() is None

    @pytest.mark.parametrize("source", ["peer", "scheduled", "cognition", "webhook", "mcp", "a2a"])
    def test_no_human_sources_are_refused(self, source):
        with push_request_context(source=source, is_admin=True):
            denial = json.loads(admin_human_denial())
        assert "no administrator" in denial["error"]
        assert "Do not retry" in denial["agent_guidance"]

    def test_non_human_origin_is_refused_even_on_a_dashboard_source(self):
        with _dashboard_admin(metadata={ORIGIN_KEY: make_origin("scheduled", False)}):
            assert "did not start with a person" in json.loads(admin_human_denial())["error"]

    def test_member_is_refused_when_multi_user_is_on(self, monkeypatch):
        import olano.runtime.auth as auth

        monkeypatch.setattr(auth, "multi_user_enabled", lambda: True)
        with _dashboard_admin(is_admin=False):
            assert "only an administrator" in json.loads(admin_human_denial())["error"]
        with _dashboard_admin(is_admin=True):
            assert admin_human_denial() is None

    def test_single_user_messenger_needs_the_trusted_sender(self, monkeypatch):
        import olano.runtime.auth as auth

        monkeypatch.setattr(auth, "multi_user_enabled", lambda: False)
        with _dashboard_admin(channel="telegram", is_admin=False, is_owner=False):
            assert "trusted-senders" in json.loads(admin_human_denial())["error"]
        with _dashboard_admin(channel="telegram", is_admin=False, is_owner=True):
            assert admin_human_denial() is None
        with _dashboard_admin(channel="cli", is_admin=False, is_owner=False):
            assert admin_human_denial() is None


# ── admin tool ────────────────────────────────────────────────────────────────


class TestAdminTool:
    def test_sets_flags_and_requires_a_description(self, agent):
        from olano.config.loader import load_agent_config
        from olano.tools.olano_admin import olano_set_agent_external_access

        with _dashboard_admin():
            out = json.loads(olano_set_agent_external_access.invoke({"name": agent, "mcp": True}))
            assert "description is required" in out["error"]
            out = json.loads(
                olano_set_agent_external_access.invoke(
                    {"name": agent, "mcp": True, "description": "Answers order questions."}
                )
            )
        assert out["external_access"] == {
            "mcp": True,
            "a2a": False,
            "description": "Answers order questions.",
        }
        assert out["applies"] == "after restart"
        assert "off home-wide" in out["note"]  # the surfaces default to off
        cfg = load_agent_config(agent)
        assert cfg.external_access.mcp is True and cfg.external_access.exposed
        with _dashboard_admin():
            out = json.loads(olano_set_agent_external_access.invoke({"name": agent, "mcp": False}))
        assert out["external_access"]["mcp"] is False
        assert load_agent_config(agent).external_access.exposed is False

    def test_refused_without_an_admin_human(self, agent):
        from olano.config.loader import load_agent_config
        from olano.tools.olano_admin import olano_set_agent_external_access

        with push_request_context(source="peer"):
            out = json.loads(
                olano_set_agent_external_access.invoke(
                    {"name": agent, "a2a": True, "description": "x"}
                )
            )
        assert out["error"].startswith("refused")
        assert load_agent_config(agent).external_access.a2a is False

    def test_registered_and_hitl_gated(self):
        from olano.security.hitl_policy import CATEGORY_TOOLS
        from olano.tools.registry import resolve_tools

        assert {t.name for t in resolve_tools(["olano_set_agent_external_access"])} == {
            "olano_set_agent_external_access"
        }
        assert {
            "olano_set_agent_external_access",
            "self_set_external_access",
            "magic_link_mint_access_token",
        } <= CATEGORY_TOOLS["write_external"]


# ── self tool ─────────────────────────────────────────────────────────────────


class TestSelfTool:
    def test_sets_own_exposure_for_an_admin(self, agent):
        from olano.config.loader import load_agent_config
        from olano.tools.self_config_tools import self_set_external_access
        from olano.tools.talk_to import current_agent

        token = current_agent.set(agent)
        try:
            with _dashboard_admin():
                out = json.loads(
                    self_set_external_access.invoke(
                        {"a2a": True, "description": "Answers questions about the roadmap."}
                    )
                )
        finally:
            current_agent.reset(token)
        assert out["external_access"]["a2a"] is True
        assert "self_restart" in out["applies"]
        assert load_agent_config(agent).external_access.a2a is True

    def test_capability_and_admin_gates(self, agent, monkeypatch):
        from olano.config.autonomy_defaults import SELF_CONFIG_TOOL_CAPABILITY
        from olano.tools.self_config_tools import self_set_external_access
        from olano.tools.talk_to import current_agent

        assert SELF_CONFIG_TOOL_CAPABILITY["self_set_external_access"] == "external_access"
        token = current_agent.set(agent)
        try:
            # A peer asking → refused before anything is written.
            with push_request_context(source="peer"):
                out = json.loads(self_set_external_access.invoke({"mcp": True, "description": "x"}))
            assert "error" in out
            # Capability off for this agent → refused too.
            save_agent_config(
                AgentConfig(name=agent, description="d", autonomy={"external_access": False})
            )
            with _dashboard_admin():
                out = json.loads(self_set_external_access.invoke({"mcp": True, "description": "x"}))
            assert "error" in out
        finally:
            current_agent.reset(token)


# ── magic link ────────────────────────────────────────────────────────────────


class TestAccessTokenMagicLink:
    def test_flow_is_registered_with_a_form(self, agent):
        from olano.magic_links import flows
        from olano.magic_links.store import LinkRecord

        flow = flows.get_flow("access-token")
        assert flow is not None and flow.kind == "form"
        save_agent_config(
            AgentConfig(
                name=agent,
                description="d",
                external_access={"mcp": True, "description": "Answers order questions."},
            )
        )
        record = LinkRecord(
            id="hs1", token_hash="x", flow="access-token", params={"label": "Zapier", "agent": ""}
        )
        desc = flow.descriptor(record)
        names = {f["name"] for f in desc["fields"]}
        assert names == {"label", "agents", "ttl"}
        agents_field = next(f for f in desc["fields"] if f["name"] == "agents")
        assert [o["value"] for o in agents_field["options"]] == ["merli", "*"]
        assert next(f for f in desc["fields"] if f["name"] == "label")["value"] == "Zapier"

    async def test_submit_mints_and_reveals_once(self, agent, monkeypatch):
        from olano.magic_links import flows, service, store
        from olano.magic_links.store import LinkRecord
        from olano.mcp_server.tokens import get_token_store

        flow = flows.get_flow("access-token")
        record = LinkRecord(
            id="hs2", token_hash="x", flow="access-token", params={"label": "", "agent": ""}
        )
        with pytest.raises(ValueError, match="label"):
            flow.submit(record, {"label": "", "agents": "merli", "ttl": "default"})
        live = flow.submit(record, {"label": "Claude — Ana", "agents": "merli", "ttl": "30"})
        assert live["status"] == flows.STATUS_DONE
        assert live["reveal"].startswith("olano_mcp_")
        assert "olano_mcp_" not in live["result"]  # the stored text never carries the secret
        rec = get_token_store().verify(live["reveal"])
        assert rec is not None and rec.agents == ["merli"] and rec.label == "Claude — Ana"
        assert rec.created_by == "magic-link:hs2" and rec.expires_at is not None

        # Through the service, the reveal rides only the submit response.
        monkeypatch.setattr(store, "mutate", lambda link_id, fn: (fn(record), record)[1])
        monkeypatch.setattr(store, "finish", lambda link_id, **kw: record)
        monkeypatch.setattr(service, "_schedule_finalize", lambda *a, **k: None)
        record.status = (
            store.STATUS_UNLOCKED if hasattr(store, "STATUS_UNLOCKED") else record.status
        )
        monkeypatch.setattr(LinkRecord, "serves_actions", lambda self: True)
        payload = await service.act_on_link(
            record, "submit", {"label": "Second", "agents": "*", "ttl": "0"}
        )
        assert payload["reveal"].startswith("olano_mcp_")
        assert "reveal" not in service.link_state(record)
        assert "olano_mcp_" not in json.dumps(record.public())

    def test_tool_is_admin_gated_and_registered(self, agent):
        from olano.config.autonomy_defaults import MAGIC_LINK_TOOL_CAPABILITY, MAGIC_LINK_TOOL_NAMES
        from olano.tools.magic_link_tools import magic_link_mint_access_token
        from olano.tools.talk_to import current_agent

        assert "magic_link_mint_access_token" in MAGIC_LINK_TOOL_NAMES
        assert MAGIC_LINK_TOOL_CAPABILITY["magic_link_mint_access_token"] == "magic_links"
        token = current_agent.set(agent)
        try:
            with push_request_context(source="mcp"):
                out = json.loads(magic_link_mint_access_token.invoke({"label": "x"}))
        finally:
            current_agent.reset(token)
        assert out["error"].startswith("refused")
