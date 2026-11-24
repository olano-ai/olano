"""Chat self-configuration (`agent_autonomy` + the `self_*` toolkit).

What these tests guard, in order of what would hurt most if it drifted:

* **Self-scoping** — no tool in the toolkit takes an agent parameter. The
  acting agent is a runtime fact (``current_agent``), so a prompt-injected
  "now update that other agent" has nothing to hijack.
* **Call-time gating** — a capability turned off refuses on the very next
  call, with an actionable message; binding is only visibility.
* **The two-tier default** — everything ON when unset, agent override wins
  in both directions, resolved only through ``autonomy_allowed``.
* **HITL** — every mutating self_* name sits in the ``write_external``
  category, so the stricter profiles put an approval card in front of it.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from olano.config.agent_config import AgentConfig
from olano.config.autonomy_defaults import (
    MAGIC_LINK_TOOL_CAPABILITY,
    SELF_CONFIG_MUTATING_TOOL_NAMES,
    SELF_CONFIG_TOOL_CAPABILITY,
    available_magic_link_tool_names,
    available_self_config_tool_names,
)
from olano.config.home import get_olano_paths, write_home_config_text
from olano.config.loader import load_agent_config
from olano.config.system_settings import (
    AUTONOMY_CAPABILITIES,
    AgentAutonomySettings,
    autonomy_allowed,
    load_agent_autonomy_settings,
    set_agent_autonomy_settings,
)
from olano.security.request_context import AGENT_ADMIN_KEY, push_request_context
from olano.tools import self_config_tools
from olano.tools.talk_to import current_agent


def write_config_block(key: str, block: dict | None) -> None:
    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if paths.config_path.exists():
        raw = yaml.safe_load(paths.config_path.read_text()) or {}
    if block is None:
        raw.pop(key, None)
    else:
        raw[key] = block
    write_home_config_text(paths.config_path, yaml.safe_dump(raw, sort_keys=False))


@pytest.fixture
def as_agent():
    """Bind the executing-agent contextvar for the duration of a test."""
    token = current_agent.set("selfie")
    yield "selfie"
    current_agent.reset(token)


def _placeholder_arg(spec: dict):
    """A type-correct dummy for one tool argument, so a gate test can invoke
    every tool generically without pydantic rejecting the input before the
    tool body (and its gate) ever runs."""
    kind = spec.get("type")
    if kind is None:
        for option in spec.get("anyOf") or []:
            if isinstance(option, dict) and option.get("type") not in (None, "null"):
                kind = option["type"]
                break
    return {"object": {}, "array": [], "boolean": False, "integer": 0, "number": 0}.get(kind, "x")


def seed_agent(name: str = "selfie", **fields) -> None:
    """Write a minimal agent.json5 into the tmp home so loaders resolve it."""
    paths = get_olano_paths()
    agent_dir = paths.agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = {"name": name, "model": "fake:instant", **fields}
    (agent_dir / "agent.json5").write_text(json.dumps(body))


# ── settings & resolution ────────────────────────────────────────────────────


class TestSettings:
    def test_absent_block_is_all_on(self):
        settings = load_agent_autonomy_settings()
        assert all(getattr(settings, cap) is True for cap in AUTONOMY_CAPABILITIES)

    def test_explicit_off_wins_globally(self):
        write_config_block("agent_autonomy", {"modify_prompt": False})
        cfg = AgentConfig(name="t", model="fake:instant")
        assert autonomy_allowed("modify_prompt", cfg) is False
        # The other capabilities keep their default.
        assert autonomy_allowed("restart_self", cfg) is True

    def test_agent_override_wins_both_directions(self):
        write_config_block("agent_autonomy", {"connect_mcps": False})
        force_on = AgentConfig(name="t", model="fake:instant", autonomy={"connect_mcps": True})
        force_off = AgentConfig(name="t", model="fake:instant", autonomy={"change_model": False})
        assert autonomy_allowed("connect_mcps", force_on) is True
        assert autonomy_allowed("change_model", force_off) is False

    def test_per_customer_agents_inherit_off(self):
        """A customer-service bot serves strangers on a shared channel — the
        unset default there is OFF, whatever the fleet block says. An explicit
        per-agent true still re-enables a capability deliberately."""
        cs = AgentConfig(name="t", model="fake:instant", memory={"per_customer": True})
        assert all(autonomy_allowed(cap, cs) is False for cap in AUTONOMY_CAPABILITIES)
        assert available_self_config_tool_names(cs) == []
        # Magic links ride the same rule: a support bot never mints links for
        # the strangers it serves.
        assert available_magic_link_tool_names(cs) == []

        opted_in = AgentConfig(
            name="t",
            model="fake:instant",
            memory={"per_customer": True},
            autonomy={"restart_self": True},
        )
        assert autonomy_allowed("restart_self", opted_in) is True
        assert autonomy_allowed("modify_prompt", opted_in) is False

    def test_none_config_uses_global(self):
        assert autonomy_allowed("restart_self", None) is True

    def test_unknown_capability_fails_closed(self):
        assert autonomy_allowed("frobnicate", None) is False

    def test_malformed_block_reads_as_defaults(self):
        write_config_block("agent_autonomy", {"modify_prompt": "sideways"})
        # Pydantic coerces where it can; a truly broken file falls back too.
        paths = get_olano_paths()
        paths.config_path.write_text("{{{ not yaml")
        assert load_agent_autonomy_settings().modify_prompt is True

    def test_writer_merges_and_drops_unknown_keys(self):
        set_agent_autonomy_settings({"restart_self": False, "not_a_capability": True})
        settings = load_agent_autonomy_settings()
        assert settings.restart_self is False
        assert settings.modify_prompt is True
        raw = yaml.safe_load(get_olano_paths().config_path.read_text())
        assert "not_a_capability" not in raw["agent_autonomy"]


# ── default binding ──────────────────────────────────────────────────────────


class TestDefaultBinding:
    def test_all_capabilities_on_binds_the_full_toolkit(self):
        c = AgentConfig(name="t", model="fake:instant")
        assert set(available_self_config_tool_names(c)) == set(SELF_CONFIG_TOOL_CAPABILITY)

    def test_capability_off_drops_its_tools(self):
        c = AgentConfig(name="t", model="fake:instant", autonomy={"connect_platforms": False})
        names = available_self_config_tool_names(c)
        assert "self_connect_whatsapp" not in names
        assert "self_connect_weixin" not in names
        assert "self_connect_platform" not in names
        assert "self_update_prompt" in names

    def test_get_config_binds_while_any_capability_is_on(self):
        every_off = {cap: False for cap in AUTONOMY_CAPABILITIES}
        c = AgentConfig(name="t", model="fake:instant", autonomy=every_off)
        assert available_self_config_tool_names(c) == []
        one_on = {**every_off, "restart_self": True}
        c2 = AgentConfig(name="t", model="fake:instant", autonomy=one_on)
        names = available_self_config_tool_names(c2)
        assert "self_get_config" in names
        assert "self_restart" in names
        assert "self_update_prompt" not in names

    def test_a_bot_made_in_the_dashboard_gets_the_whole_toolkit(self):
        """The reported bug was "my new bot has no self_* tools", so pin the
        literal template `POST /api/agents` writes — `tools: []` and nothing
        else — rather than a config hand-built for the assertion."""
        created = AgentConfig.model_validate(
            {
                "name": "markie",
                "display_name": "markie",
                "model": "fake:instant",
                "system_prompt": "You are markie.",
                "tools": [],
                "subagents": [],
                "builtin_subagents": ["ops"],
                "skills": [],
            }
        )
        assert set(available_self_config_tool_names(created)) == set(SELF_CONFIG_TOOL_CAPABILITY)

    def test_an_archetype_tools_list_does_not_suppress_the_grant(self):
        """Every archetype pins a `tools:` list; the grant is additive, so a
        bot installed from one still gets the toolkit."""
        from_archetype = AgentConfig(
            name="t", model="fake:instant", tools=["safe_shell", "web_search"]
        )
        assert set(available_self_config_tool_names(from_archetype)) == set(
            SELF_CONFIG_TOOL_CAPABILITY
        )

    def test_default_internal_tools_off_means_no_implicit_grant(self):
        """One of exactly two things that suppress the grant outright — the
        other is `memory.per_customer` (see TestSettings). Nothing else does,
        which is what makes "my bot has no self_* tools" a two-line diagnosis.
        """
        c = AgentConfig(name="t", model="fake:instant", default_internal_tools=False)
        assert available_self_config_tool_names(c) == []

    def test_disabled_tools_and_explicit_tools_are_respected(self):
        c = AgentConfig(
            name="t",
            model="fake:instant",
            tools=["self_restart"],
            disabled_tools=["self_update_prompt"],
        )
        names = available_self_config_tool_names(c)
        assert "self_restart" not in names  # explicit — binds via tools:, not here
        assert "self_update_prompt" not in names  # disabled wins


class TestMagicLinkBinding:
    """The magic-link family is granted to every agent under ONE capability,
    kept in its own map so the self_* pins above never move."""

    def test_all_on_binds_the_whole_family(self):
        c = AgentConfig(name="t", model="fake:instant")
        assert set(available_magic_link_tool_names(c)) == set(MAGIC_LINK_TOOL_CAPABILITY)
        assert set(MAGIC_LINK_TOOL_CAPABILITY.values()) == {"magic_links"}

    def test_capability_off_drops_the_family_and_nothing_else(self):
        c = AgentConfig(name="t", model="fake:instant", autonomy={"magic_links": False})
        assert available_magic_link_tool_names(c) == []
        assert "self_update_prompt" in available_self_config_tool_names(c)

    def test_a_self_capability_off_leaves_magic_links_alone(self):
        c = AgentConfig(name="t", model="fake:instant", autonomy={"connect_mcps": False})
        assert "magic_link_connect_service" in available_magic_link_tool_names(c)

    def test_default_internal_tools_off_means_no_implicit_grant(self):
        c = AgentConfig(name="t", model="fake:instant", default_internal_tools=False)
        assert available_magic_link_tool_names(c) == []

    def test_disabled_tools_and_explicit_tools_are_respected(self):
        c = AgentConfig(
            name="t",
            model="fake:instant",
            tools=["magic_link_status"],
            disabled_tools=["magic_link_open_dashboard"],
        )
        names = available_magic_link_tool_names(c)
        assert "magic_link_status" not in names  # explicit — binds via tools:, not here
        assert "magic_link_open_dashboard" not in names  # disabled wins
        assert "magic_link_request_secret" in names

    def test_effective_tool_names_and_the_builder_agree(self):
        from olano.config.ops_defaults import resolve_effective_tool_names

        c = AgentConfig(name="t", model="fake:instant")
        effective = set(resolve_effective_tool_names(c.tools, c.disabled_tools, c))
        assert set(MAGIC_LINK_TOOL_CAPABILITY) <= effective
        off = AgentConfig(name="t", model="fake:instant", autonomy={"magic_links": False})
        effective_off = set(resolve_effective_tool_names(off.tools, off.disabled_tools, off))
        assert not (set(MAGIC_LINK_TOOL_CAPABILITY) & effective_off)

    def test_magic_link_toolkit_names_match_the_capability_map(self):
        from olano.tools import magic_link_tools

        names = {t.name for t in magic_link_tools.get_tools()}
        assert names == set(MAGIC_LINK_TOOL_CAPABILITY)


# ── the toolkit itself ───────────────────────────────────────────────────────


class TestSelfScoping:
    def test_no_tool_takes_an_agent_parameter(self):
        """The identity is a runtime fact — a tool with an `agent` argument
        would hand prompt injection a way to point these at another agent."""
        for t in self_config_tools.get_tools():
            params = set(inspect.signature(t.func or t.coroutine).parameters)
            assert "agent" not in params, t.name
            assert "agent_name" not in params, t.name

    def test_toolkit_names_match_the_capability_map(self):
        names = {t.name for t in self_config_tools.get_tools()}
        assert names == set(SELF_CONFIG_TOOL_CAPABILITY)

    def test_no_context_is_an_error(self):
        out = json.loads(self_config_tools.self_get_config.invoke({}))
        assert "error" in out


class TestGating:
    def test_denied_capability_refuses_with_guidance(self, as_agent):
        seed_agent(as_agent, autonomy={"modify_prompt": False})
        out = json.loads(
            self_config_tools.self_update_prompt.invoke({"system_prompt": "be different"})
        )
        assert "turned off" in out["error"]
        assert "how_to_enable" in out

    def test_global_off_denies_and_agent_on_reopens(self, as_agent):
        write_config_block("agent_autonomy", {"restart_self": False})
        seed_agent(as_agent)
        out = json.loads(self_config_tools.self_restart.invoke({}))
        assert "turned off" in out["error"]

        seed_agent(as_agent, autonomy={"restart_self": True})
        with patch("olano.admin.restart_agent") as restart:
            out = json.loads(self_config_tools.self_restart.invoke({}))
            assert out["agent"] == as_agent
            # Scheduled, not inline — restarting mid-turn would cut the reply.
            restart.assert_not_called()

    def test_update_prompt_goes_through_admin(self, as_agent):
        seed_agent(as_agent)
        with patch("olano.admin.update_agent", return_value={"name": as_agent}) as upd:
            out = json.loads(
                self_config_tools.self_update_prompt.invoke({"system_prompt": "New rules."})
            )
        upd.assert_called_once_with(as_agent, {"system_prompt": "New rules."})
        assert "applies" in out

    def test_empty_prompt_is_refused(self, as_agent):
        seed_agent(as_agent)
        out = json.loads(self_config_tools.self_update_prompt.invoke({"system_prompt": "   "}))
        assert "error" in out

    def test_set_model_refuses_unconnected_provider(self, as_agent):
        seed_agent(as_agent)
        with patch("olano.admin.model_provider_connected", return_value=(False, "no key for acme")):
            out = json.loads(self_config_tools.self_set_model.invoke({"model": "acme:big"}))
        assert "not usable" in out["error"]

    def test_connect_platform_replaces_same_type_and_keeps_others(self, as_agent):
        seed_agent(
            as_agent,
            channels=["telegram", {"type": "platform:slack", "channel": "#general"}],
        )
        with (
            patch(
                "olano.admin.list_platforms",
                return_value={"platforms": [{"type": "platform:slack"}]},
            ),
            patch("olano.admin.update_agent", return_value={"name": as_agent}) as upd,
        ):
            out = json.loads(
                self_config_tools.self_connect_platform.invoke(
                    {"platform_type": "platform:slack", "settings": {"channel": "#new"}}
                )
            )
        assert "applies" in out
        channels = upd.call_args[0][1]["channels"]
        assert "telegram" in channels  # untouched sibling, original shape
        slack = [c for c in channels if isinstance(c, dict) and c["type"] == "platform:slack"]
        assert slack == [{"type": "platform:slack", "channel": "#new"}]

    def test_connect_platform_refuses_unknown_type(self, as_agent):
        seed_agent(as_agent)
        with patch("olano.admin.list_platforms", return_value={"platforms": []}):
            out = json.loads(
                self_config_tools.self_connect_platform.invoke(
                    {"platform_type": "platform:carrier-pigeon"}
                )
            )
        assert "unknown platform" in out["error"]


class TestToolkits:
    """`self_add_toolkit` / `self_remove_toolkit` / `self_find_integration`.

    A toolkit is granted by expanding it to its member TOOL names (the
    `tools:` list holds names, never bundles), the deny-list keeps an agent
    from handing itself `olano_admin`, and the finder stays open to a
    non-owner — it only reads catalogs."""

    @pytest.fixture(autouse=True)
    def no_credentials(self, monkeypatch):
        monkeypatch.setattr(
            "olano.tools._credentials.get_credential", lambda name, default="": default
        )

    def test_add_expands_to_member_names_and_remove_takes_only_those(self, as_agent):
        seed_agent("selfie", tools=["web_search"])
        out = json.loads(self_config_tools.self_add_toolkit.invoke({"name": "notion"}))
        assert "notion_search" in out["added"]
        assert out["requires_restart"] is True
        assert out["next"] == "call self_restart"
        assert {"name": "NOTION_API_KEY", "resolved": False} in out["credentials"]
        tools = load_agent_config("selfie").tools
        assert tools[0] == "web_search" and "notion_create_page" in tools
        assert "notion" not in tools  # never the bundle name itself

        out = json.loads(self_config_tools.self_remove_toolkit.invoke({"name": "notion"}))
        assert "notion_search" in out["removed"]
        tools = load_agent_config("selfie").tools
        assert "web_search" in tools
        assert not any(t.startswith("notion_") for t in tools)

    @pytest.mark.parametrize("name", ["olano_admin", "self_config", "magic_link", "secrets"])
    def test_denylisted_toolkits_are_refused(self, as_agent, name):
        seed_agent("selfie")
        before = load_agent_config("selfie").tools
        out = json.loads(self_config_tools.self_add_toolkit.invoke({"name": name}))
        assert "cannot be granted" in out["error"]
        assert load_agent_config("selfie").tools == before

    def test_unknown_toolkit_is_refused_with_a_pointer(self, as_agent):
        seed_agent("selfie")
        out = json.loads(self_config_tools.self_add_toolkit.invoke({"name": "no-such"}))
        assert "unknown toolkit" in out["error"]
        assert "self_find_integration" in out["error"]

    def test_finder_is_open_to_a_non_owner(self, as_agent):
        seed_agent("selfie")
        with push_request_context(source="api", channel="api", metadata={AGENT_ADMIN_KEY: False}):
            out = json.loads(self_config_tools.self_find_integration.invoke({"query": "notion"}))
        assert "own it" not in json.dumps(out)
        assert out["lead"].startswith("Supported ways to connect")
        assert [o["class"] for o in out["options"]] == sorted(
            (o["class"] for o in out["options"]), key=["channel", "mcp", "toolkit"].index
        )
        assert out["policy"]

    def test_finder_respects_the_capability_switch(self, as_agent):
        seed_agent("selfie", autonomy={"connect_mcps": False})
        out = json.loads(self_config_tools.self_find_integration.invoke({"query": "notion"}))
        assert "turned off" in out["error"]

    def test_stdio_mcp_server_persists_command_args_and_vault_env(self, as_agent):
        seed_agent("selfie")
        out = json.loads(
            self_config_tools.self_add_mcp_server.invoke(
                {
                    "url": "acme-stdio",
                    "transport": "stdio",
                    "command": "npx",
                    "command_args": ["-y", "@acme/mcp-server"],
                    "env": {"ACME_API_KEY": "${vault:ACME_API_KEY}", "LOG_LEVEL": "debug"},
                }
            )
        )
        assert out["added"] is True
        server = load_agent_config("selfie").mcp_servers[0]
        assert server.transport == "stdio"
        assert server.command == "npx"
        assert server.args == ["-y", "@acme/mcp-server"]
        assert server.env == {"ACME_API_KEY": "${vault:ACME_API_KEY}", "LOG_LEVEL": "debug"}

    @pytest.mark.parametrize(
        "env",
        [
            {"ACME_API_KEY": "sk-live-1234567890abcdef"},  # gitleaks:allow
            {"ACME_TOKEN": "x"},
            {"WHATEVER": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
        ],
    )
    def test_a_literal_secret_in_env_is_refused(self, as_agent, env):
        seed_agent("selfie")
        out = json.loads(
            self_config_tools.self_add_mcp_server.invoke(
                {"url": "acme", "transport": "stdio", "command": "npx", "env": env}
            )
        )
        assert "magic_link_request_secret" in out["error"]
        assert load_agent_config("selfie").mcp_servers == []

    def test_a_stdio_server_needs_a_command(self, as_agent):
        seed_agent("selfie")
        out = json.loads(
            self_config_tools.self_add_mcp_server.invoke({"url": "acme", "transport": "stdio"})
        )
        assert "command" in out["error"]


class TestSkillSwitches:
    """``self_list_skills`` / ``self_enable_skill`` / ``self_disable_skill``.

    The agent-facing half of the Skills tab's per-skill switches. What matters
    here beyond the shared gating (covered generically above) is that the
    switch is only ever a switch: it never installs, edits or deletes a skill,
    and it can only reach skills this agent actually has.
    """

    def _seed_skill(self, agent: str, name: str) -> None:
        skill_dir = get_olano_paths().agent_workspace_dir(agent) / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: does {name}\n---\n\nBody.\n", encoding="utf-8"
        )

    def test_list_reports_every_skill_with_its_switch_state(self, as_agent):
        seed_agent(as_agent, disabled_skills=["beta"])
        self._seed_skill(as_agent, "alpha")
        self._seed_skill(as_agent, "beta")

        out = json.loads(self_config_tools.self_list_skills.invoke({}))
        states = {s["name"]: s["enabled"] for s in out["skills"]}
        # The disabled one is still listed — it is the one the agent can turn
        # back on, and hiding it would make that impossible to discover.
        assert states == {"alpha": True, "beta": False}
        assert out["enabled_count"] == 1
        assert out["total"] == 2

    def test_disable_then_enable_round_trips_through_admin(self, as_agent):
        seed_agent(as_agent)
        self._seed_skill(as_agent, "noisy")
        skill_md = get_olano_paths().agent_workspace_dir(as_agent) / "skills" / "noisy" / "SKILL.md"

        off = json.loads(self_config_tools.self_disable_skill.invoke({"skill": "noisy"}))
        assert off["enabled"] is False
        assert load_agent_config(as_agent, source="home").disabled_skills == ["noisy"]
        # A switch, not a delete: the skill is untouched on disk.
        assert skill_md.exists()

        on = json.loads(self_config_tools.self_enable_skill.invoke({"skill": "noisy"}))
        assert on["enabled"] is True
        assert load_agent_config(as_agent, source="home").disabled_skills == []

    def test_a_skill_this_agent_does_not_have_is_refused(self, as_agent):
        seed_agent(as_agent)
        self._seed_skill(as_agent, "mine")

        out = json.loads(self_config_tools.self_disable_skill.invoke({"skill": "not-mine"}))
        assert "no skill named 'not-mine'" in out["error"]
        assert load_agent_config(as_agent, source="home").disabled_skills == []


class TestQrRendering:
    def test_save_qr_png_writes_into_the_workspace(self):
        seed_agent("selfie")
        path = self_config_tools._save_qr_png("selfie", "1@abcdef,mock-pairing-data")
        ws = get_olano_paths().agent_workspace_dir("selfie")
        assert path.startswith(str(ws))
        with open(path, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"  # a real PNG, not a stub


class TestConnectWeixin:
    """`self_connect_weixin` — QR-first WeChat pairing from chat.

    QR is the DEFAULT connect path for WeChat (the token is a manual
    fallback), so the tool must hand back a scannable QR image immediately and,
    once the scan lands, save the channel pinned to the scanned account —
    the model never has to know the `extra.bot_id` convention.
    """

    @pytest.fixture
    def wechat_connector(self, monkeypatch):
        """A fake 'wechat' connector writing into the real pairing registry."""
        from olano.platforms import channel_setup

        class FakeConnector(channel_setup.ChannelConnector):
            platform = "wechat"
            label = "WeChat"
            channel_type = "platform:weixin"
            mode = "qr"
            instructions = "Scan with WeChat (扫一扫)."
            next_state: channel_setup.PairingState | None = None

            async def start_pairing(self, key, agent, extra):
                state = self.next_state or channel_setup.PairingState(key=key)
                state.key, state.agent = key, agent
                channel_setup._PAIRINGS[key] = state

        fake = FakeConnector()
        monkeypatch.setitem(channel_setup._CONNECTORS, "wechat", fake)
        yield fake
        channel_setup._PAIRINGS.pop("selfcfg:selfie:wechat", None)

    async def test_qr_path_returns_a_scannable_image(self, as_agent, wechat_connector):
        from olano.platforms import channel_setup

        seed_agent(as_agent)
        state = channel_setup.PairingState()
        state.status = "qr"
        state.qr_data = "https://wxaurl.cn/mock-liteapp-token"
        wechat_connector.next_state = state
        out = json.loads(await self_config_tools.self_connect_weixin.ainvoke({}))
        assert out["status"] == "scan_the_qr"
        assert out["qr_image"].endswith(".png")
        with open(out["qr_image"], "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"
        assert "扫一扫" in out["instructions"]

    async def test_done_path_pins_the_account_into_the_channel(self, as_agent, wechat_connector):
        from olano.platforms import channel_setup

        seed_agent(
            as_agent,
            channels=["telegram", {"type": "platform:weixin", "group_listen": "mention"}],
        )
        state = channel_setup.PairingState()
        state.status = "done"
        state.identity = "ilink-bot-42"
        wechat_connector.next_state = state
        with patch("olano.admin.update_agent", return_value={"name": as_agent}) as upd:
            out = json.loads(await self_config_tools.self_connect_weixin.ainvoke({}))
        assert out["status"] == "connected"
        assert out["identity"] == "ilink-bot-42"
        channels = upd.call_args[0][1]["channels"]
        assert "telegram" in channels  # untouched sibling, original shape
        wx = [c for c in channels if isinstance(c, dict) and c["type"] == "platform:weixin"]
        # Existing settings survive; the scanned login is pinned as the bot id.
        assert wx == [
            {
                "type": "platform:weixin",
                "group_listen": "mention",
                "extra": {"bot_id": "ilink-bot-42"},
            }
        ]

    async def test_unavailable_connector_is_an_actionable_error(self, as_agent, monkeypatch):
        from olano.platforms import channel_setup

        seed_agent(as_agent)
        monkeypatch.setattr(channel_setup, "_CONNECTORS", {})
        out = json.loads(await self_config_tools.self_connect_weixin.ainvoke({}))
        assert "not available" in out["error"]


class TestHitl:
    def test_every_mutating_tool_is_hitl_gated(self):
        from olano.security.hitl_policy import CATEGORY_TOOLS

        assert SELF_CONFIG_MUTATING_TOOL_NAMES <= CATEGORY_TOOLS["write_external"]
        # And the read-only ones are deliberately not.
        readonly = set(SELF_CONFIG_TOOL_CAPABILITY) - SELF_CONFIG_MUTATING_TOOL_NAMES
        assert not (readonly & CATEGORY_TOOLS["write_external"])

    def test_the_mutating_set_matches_the_tools_own_metadata(self):
        """Three lists name the same tools and must never drift apart: the
        constant, the HITL category above, and the `@tool_metadata` tags the
        security pipeline reads. Derive the third from the tools themselves so
        a new tool tagged `write_external` fails here until it is added to the
        constant — otherwise it would ship past the ownership gate."""
        from olano.security.tool_metadata import get_tool_metadata

        tagged = {
            t.name
            for t in self_config_tools._ALL_TOOLS
            if get_tool_metadata(t).capability == "write_external"
        }
        assert tagged == SELF_CONFIG_MUTATING_TOOL_NAMES

    async def test_every_mutating_tool_actually_calls_the_ownership_gate(self, as_agent):
        """The gate is opt-in per call site (`_gate(..., mutating=True)`), so a
        tool that forgets the flag looks fine everywhere except in production.
        Drive each one for real with a non-owner context and require a refusal
        — this is the test that catches a missed call site. `ainvoke` because
        the toolkit mixes sync and async tools; it serves both."""
        seed_agent("selfie")
        for name in sorted(SELF_CONFIG_MUTATING_TOOL_NAMES):
            tool = self_config_tools.get_tool(name)
            args = {k: _placeholder_arg(spec) for k, spec in tool.args.items()}
            with push_request_context(
                source="api", channel="api", metadata={AGENT_ADMIN_KEY: False}
            ):
                out = json.loads(await tool.ainvoke(args))
            assert "own it" in out.get("error", ""), f"{name} skipped the ownership gate"

    async def test_read_only_tools_do_not_call_the_ownership_gate(self, as_agent):
        """A user who may chat but not administer still gets the catalog."""
        seed_agent("selfie")
        with push_request_context(source="api", channel="api", metadata={AGENT_ADMIN_KEY: False}):
            out = json.loads(await self_config_tools.get_tool("self_list_models").ainvoke({}))
        assert "own it" not in json.dumps(out)


# ── ownership: who may ask, as opposed to what the agent may change ──────────


class TestOwnershipGate:
    """`_ownership_gate` answers "is this human entitled to reconfigure this
    agent". It is a different axis from the capability switch, and from
    `ctx.is_owner` — which on the dashboard is `user.is_admin` and on a
    messenger is channel-allowlist membership, so neither can answer it."""

    def test_owner_passes_and_non_owner_is_refused(self):
        with push_request_context(source="api", channel="api", metadata={AGENT_ADMIN_KEY: True}):
            assert self_config_tools._ownership_gate() is None
        with push_request_context(source="api", channel="api", metadata={AGENT_ADMIN_KEY: False}):
            assert self_config_tools._ownership_gate() is not None

    def test_refusal_names_the_remedy(self):
        with push_request_context(source="api", channel="api", metadata={AGENT_ADMIN_KEY: False}):
            out = json.loads(self_config_tools._ownership_gate() or "{}")
        assert "own it" in out["error"]
        assert "Admin → Access" in out["how_to_enable"]

    @pytest.mark.parametrize("source", ["peer", "scheduled", "cognition", "webhook"])
    def test_no_human_at_the_keyboard_is_refused(self, source):
        """These run BEFORE the metadata read on purpose: push_request_context
        merges metadata, so a peer hop or a Cortex run started from an owner's
        dashboard turn inherits that turn's answer. Simulate exactly that."""
        with push_request_context(source="api", channel="api", metadata={AGENT_ADMIN_KEY: True}):
            with push_request_context(source=source):
                refusal = self_config_tools._ownership_gate()
        assert refusal is not None
        # And the message fits the case: "ask an owner to grant you Owner" is
        # nonsense advice for a cron job or a peer agent.
        assert "a human" in json.loads(refusal)["error"]

    def test_cli_is_allowed_without_a_bound_answer(self):
        """Whoever holds a shell can edit agent.json5 directly, so refusing
        here would protect nothing."""
        for channel in ("", "local", "cli"):
            with push_request_context(source="local", channel=channel):
                assert self_config_tools._ownership_gate() is None

    def test_unbound_web_surface_falls_back_to_the_deployment_mode(self, monkeypatch):
        """A surface that never bound the answer must NOT ride the fail-open
        RequestContext defaults (`is_owner=True`) on a multi-user box."""
        import olano.runtime.auth as auth_mod

        monkeypatch.setattr(auth_mod, "multi_user_enabled", lambda: True)
        with push_request_context(source="api", channel="webhook"):
            assert self_config_tools._ownership_gate() is not None

        monkeypatch.setattr(auth_mod, "multi_user_enabled", lambda: False)
        with push_request_context(source="api", channel="webhook"):
            assert self_config_tools._ownership_gate() is None

    def test_capability_refusal_wins_over_ownership(self, as_agent):
        """A capability the operator turned off is the same answer for
        everybody, so it is the more useful one — and reporting it first means
        a non-owner never learns the capability state from the ordering."""
        seed_agent("selfie", autonomy={"connect_mcps": False})
        with push_request_context(source="api", channel="api", metadata={AGENT_ADMIN_KEY: False}):
            _, denial = self_config_tools._gate("connect_mcps", mutating=True)
        assert "turned off" in json.loads(denial or "{}")["error"]


# ── config API ───────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    return TestClient(create_api(runtime))


class TestConfigApi:
    def test_get_reports_the_block_all_on(self, client):
        block = client.get("/api/system/config").json()["agent_autonomy"]
        assert set(block) == set(AUTONOMY_CAPABILITIES)
        assert all(block.values())
        assert block["magic_links"] is True

    def test_put_round_trips_and_drops_unknown_keys(self, client):
        saved = client.put(
            "/api/system/config",
            json={"agent_autonomy": {"connect_platforms": False, "bogus": True}},
        )
        assert saved.status_code == 200
        block = client.get("/api/system/config").json()["agent_autonomy"]
        assert block["connect_platforms"] is False
        assert block["modify_prompt"] is True
        assert "bogus" not in block

    def test_junk_shape_is_a_400(self, client):
        resp = client.put("/api/system/config", json={"agent_autonomy": ["on"]})
        assert resp.status_code == 400


def test_settings_model_matches_the_capability_tuple():
    """The tuple, the model and the per-agent override must never drift."""
    from olano.config.agent_config import AutonomyConfig

    assert set(AgentAutonomySettings.model_fields) == set(AUTONOMY_CAPABILITIES)
    assert set(AutonomyConfig.model_fields) == set(AUTONOMY_CAPABILITIES)
    granted = set(SELF_CONFIG_TOOL_CAPABILITY.values()) | set(MAGIC_LINK_TOOL_CAPABILITY.values())
    assert granted - {"*"} == set(AUTONOMY_CAPABILITIES)
