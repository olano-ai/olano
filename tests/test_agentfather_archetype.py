"""Tests for the AgentFather archetype + olano_admin toolkit wiring."""

from __future__ import annotations

from olano.archetypes.installer import load_archetype_template
from olano.config.agent_config import AgentConfig
from olano.tools import olano_admin
from olano.tools.registry import resolve_tools


def test_archetype_loads_and_validates() -> None:
    tmpl = load_archetype_template("agentfather")
    cfg = AgentConfig.model_validate({**tmpl, "name": "agentfather"})
    assert cfg.human_only is True
    assert "talk_to_agent" in cfg.disabled_tools
    assert cfg.security.hitl_enabled is True
    assert cfg.security.hitl_write_tools is True


def test_archetype_exposes_platform_command() -> None:
    """AgentFather gets the owner-only /olano command by default; it stays
    sensitive (default-off) for every other agent."""
    from olano.config.commands_catalog import default_exposed

    tmpl = load_archetype_template("agentfather")
    cfg = AgentConfig.model_validate({**tmpl, "name": "agentfather"})
    assert cfg.commands.is_exposed("olano") is True
    assert default_exposed("olano") is False


def test_archetype_tools_all_resolve() -> None:
    """Every tool the archetype lists must resolve (guards against drift)."""
    tmpl = load_archetype_template("agentfather")
    tools = tmpl["tools"]
    resolved = resolve_tools(tools)
    resolved_names = {t.name for t in resolved}
    for name in tools:
        assert name in resolved_names, f"archetype tool {name!r} did not resolve"


def test_archetype_includes_full_admin_toolkit() -> None:
    """The archetype must list every tool the toolkit exposes."""
    tmpl = load_archetype_template("agentfather")
    toolkit_names = {t.name for t in olano_admin.get_tools()}
    listed = set(tmpl["tools"])
    missing = toolkit_names - listed
    assert not missing, f"archetype is missing admin tools: {sorted(missing)}"


def test_install_archetype_scaffolds_agent() -> None:
    from olano.admin import scaffold_from_archetype
    from olano.config.loader import load_agent_config

    scaffold_from_archetype("agentfather")
    cfg = load_agent_config("agentfather", source="home")
    assert cfg.human_only is True


# ── Discovery tools (catalog the platform's built-ins) ───────────────────────


def test_archetype_includes_discovery_tools() -> None:
    """AgentFather must be able to enumerate every kind of built-in."""
    tmpl = load_archetype_template("agentfather")
    listed = set(tmpl["tools"])
    for name in (
        "olano_list_integrations",  # tools / toolkits
        "olano_list_platforms",  # connectors
        "olano_list_builtin_agents",  # teams + experts + integration agents
        "olano_list_builtin_skills",  # built-in skills
        "olano_read_archetype",  # one archetype's real prompt/tools/config
        "olano_find_tools",  # tools by discovery tag, incl. builder-injected
    ):
        assert name in listed, f"archetype missing discovery tool {name!r}"


def test_list_integrations_shape() -> None:
    from olano import admin

    out = admin.list_integrations()
    assert set(out) == {"toolkits", "composite_bundles"}
    names = {row["name"] for row in out["toolkits"]}
    # A few stable per-service toolkits should always be present.
    assert {"google", "slack", "stripe", "sql"} <= names
    for row in out["toolkits"]:
        assert row["tool_count"] >= 0
        assert row["enabled_count"] <= row["tool_count"]


def test_list_platforms_includes_messaging_connectors() -> None:
    from olano import admin

    out = admin.list_platforms()
    assert out["count"] == len(out["platforms"])
    types = {row["type"] for row in out["platforms"]}
    # Legacy bare types and prefixed platform adapters both surface.
    assert "telegram" in types
    assert "platform:whatsapp" in types
    assert "platform:signal" in types
    for row in out["platforms"]:
        assert row["kind"] in {"legacy", "platform"}
        assert isinstance(row["env_vars"], list)


def test_platform_catalog_matches_bootable_maps() -> None:
    """The catalog must list exactly what the runtime can boot (no drift)."""
    from olano.platforms.catalog import build_platform_catalog
    from olano.runtime.server import CHANNEL_MAP, PLATFORM_MAP

    catalog_types = {row["type"] for row in build_platform_catalog()}
    expected = set(CHANNEL_MAP) | {f"platform:{p}" for p in PLATFORM_MAP}
    assert catalog_types == expected


def test_list_builtin_agents_unifies_teams_experts_and_integrations() -> None:
    from olano import admin

    out = admin.list_builtin_agents()
    assert out["count"] == len(out["agents"])
    agents = out["agents"]

    # Every entry is referenceable and declares how it can be used.
    assert all(row.get("ref") for row in agents)
    for row in agents:
        assert row["kind"] in {"team", "expert", "integration", "ops"}
        assert set(row["usable_as"]) <= {"agent", "subagent"}
        assert row["usable_as"]  # non-empty

    kinds = {row["kind"] for row in agents}
    # All three catalog kinds plus the ops manager are present in one list.
    assert {"expert", "integration", "ops"} <= kinds
    refs = {row["ref"] for row in agents}
    assert "ops" in refs  # OpsManager (subagent-only)

    # Archetype-backed agents can be used both ways; ops is subagent-only.
    ops = next(r for r in agents if r["ref"] == "ops")
    assert ops["usable_as"] == ["subagent"]
    integrations = [r for r in agents if r["kind"] == "integration"]
    assert integrations and all("agent" in r["usable_as"] for r in integrations)
    assert len(agents) > 5


def test_list_builtin_skills_returns_catalog() -> None:
    from olano import admin

    out = admin.list_builtin_skills()
    assert isinstance(out, list)
    # The olano-admin skill ships with the package.
    assert any(s["name"] == "olano-admin" for s in out)
    for s in out:
        assert s["name"] and "location" in s


def test_vault_tools_wired_and_resolve() -> None:
    """AgentFather can list/set secrets but cannot read raw values."""
    from olano.tools.registry import resolve_tools

    tmpl = load_archetype_template("agentfather")
    listed = set(tmpl["tools"])
    assert "secret_list" in listed
    assert "secret_set" in listed
    # secret_get is intentionally omitted (no need to read raw secrets).
    assert "secret_get" not in listed
    resolved = {t.name for t in resolve_tools(["secret_list", "secret_set"])}
    assert {"secret_list", "secret_set"} <= resolved


# ── Model selection + connector credentials (the operator's two blind spots) ──


def test_archetype_can_see_which_models_this_install_can_run() -> None:
    """Without this tool AgentFather creates agents pointed at providers that
    aren't connected — the AgentConfig default is Anthropic."""
    tmpl = load_archetype_template("agentfather")
    assert "olano_list_models" in set(tmpl["tools"])
    assert "olano_list_models" in {t.name for t in resolve_tools(["olano_list_models"])}


def test_prompt_requires_the_catalog_before_choosing_a_model() -> None:
    prompt = load_archetype_template("agentfather")["system_prompt"]
    assert "olano_list_models" in prompt
    assert "never assume Anthropic" in prompt
    # The specific failure this guards: model names written from memory.
    assert "Never write a model string from memory" in prompt


def test_prompt_forbids_credentials_in_agent_json5() -> None:
    prompt = load_archetype_template("agentfather")["system_prompt"]
    assert "NEVER put a credential in `agent.json5`" in prompt
    # Both halves of the correct flow must be spelled out.
    assert 'secret_set(name=\\"TELEGRAM_BOT_TOKEN\\"' in prompt or "secret_set(name=" in prompt
    assert "per-agent override" in prompt


def test_archetype_config_carries_no_channel_credential() -> None:
    """The template must model what it preaches."""
    tmpl = load_archetype_template("agentfather")
    for channel in tmpl.get("channels") or []:
        if isinstance(channel, dict):
            assert "token" not in channel


def test_secret_tools_can_scope_to_one_agent() -> None:
    """A per-agent override is the whole mechanism; the tool must expose it."""
    import inspect

    from olano.tools.secret_tools import secret_list, secret_set

    assert "agent" in inspect.signature(secret_set.func).parameters
    assert "agent" in inspect.signature(secret_list.func).parameters


# ── Team accounts (human-user management) ────────────────────────────────────


def test_user_admin_tools_wired() -> None:
    """AgentFather manages the deployment's humans: the tools are listed and
    resolve through the static index."""
    tmpl = load_archetype_template("agentfather")
    listed = set(tmpl["tools"])
    user_tools = {
        "olano_list_users",
        "olano_get_user",
        "olano_create_user",
        "olano_reset_user_password",
        "olano_set_user_role",
        "olano_update_user",
        "olano_delete_user",
        "olano_grant_agent_access",
        "olano_revoke_agent_access",
        "olano_access_overview",
    }
    assert user_tools <= listed
    resolved = {t.name for t in resolve_tools(sorted(user_tools))}
    assert user_tools <= resolved


def test_prompt_teaches_the_password_less_invite_flow() -> None:
    prompt = load_archetype_template("agentfather")["system_prompt"]
    assert "olano_create_user" in prompt
    # The rule the flow exists for: credentials never pass through chat.
    assert "never set, ask for, or accept a password" in prompt
