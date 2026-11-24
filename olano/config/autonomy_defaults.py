"""Default grant of the ``self_config`` and ``magic_link`` toolkits.

The sibling of :mod:`olano.config.multimodal_defaults`, for a different gate:
where the multimodal tools bind when a *credential* resolves, the ``self_*``
tools bind when the matching **autonomy capability** resolves for this agent
(``agent_autonomy:`` in ``config.yaml`` overridden by ``agent.json5 →
autonomy``, everything defaulting ON — see ``system_settings.autonomy_allowed``).

The ``magic_link_*`` tools ride the same mechanism under one capability,
``magic_links``, kept in a **second map** so the self-configuration constants
(and the tests that pin them against the HITL policy) stay exactly what they
were. Magic links are how an agent gets a human through anything that needs a
browser — an OAuth consent screen, a key typed straight into the vault, a QR
pairing — so they are the default route for "connect me to X" on every agent,
not an AgentFather privilege.

Binding is a *visibility* decision only: every tool re-checks its capability
at call time, so a knob flipped off after boot refuses immediately, and a knob
flipped on is picked up by the next restart's binding. The same predicate
feeds the builder (`core/builder._build_extra_tools`), the internal-tools
catalog (dashboard reporting) and ``whoami``, so no surface disagrees about
which self-configuration tools an agent has.

``default_internal_tools: false`` turns the implicit grant off, exactly like
every other builder default; an explicit ``tools:`` entry binds
unconditionally (the operator named it — the call-time capability check still
governs whether it *acts*).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

# Tool name → the autonomy capability that grants it. `self_get_config` is the
# read-only "know thyself" summary; it binds when ANY capability is on, since
# it is how the agent learns what it may change (the sentinel "*" below).
SELF_CONFIG_TOOL_CAPABILITY: dict[str, str] = {
    "self_get_config": "*",
    "self_list_archetypes": "explore_archetypes",
    "self_read_archetype": "explore_archetypes",
    "self_list_models": "change_model",
    "self_set_model": "change_model",
    "self_update_prompt": "modify_prompt",
    "self_set_external_access": "external_access",
    "self_restart": "restart_self",
    "self_list_skills": "manage_skills",
    "self_enable_skill": "manage_skills",
    "self_disable_skill": "manage_skills",
    "self_list_subagents": "add_subagents",
    "self_add_subagent": "add_subagents",
    "self_search_mcp_directory": "connect_mcps",
    "self_list_mcp_servers": "connect_mcps",
    "self_list_mcp_tools": "connect_mcps",
    "self_reload_mcp_servers": "connect_mcps",
    "self_add_mcp_server": "connect_mcps",
    "self_remove_mcp_server": "connect_mcps",
    "self_authorize_mcp_server": "connect_mcps",
    "self_find_integration": "connect_mcps",
    "self_add_toolkit": "connect_mcps",
    "self_remove_toolkit": "connect_mcps",
    "self_inspect_external": "connect_mcps",
    "self_list_platforms": "connect_platforms",
    "self_connect_platform": "connect_platforms",
    "self_connect_whatsapp": "connect_platforms",
    "self_connect_weixin": "connect_platforms",
}

SELF_CONFIG_TOOL_NAMES: tuple[str, ...] = tuple(SELF_CONFIG_TOOL_CAPABILITY)

# The subset that WRITES — config, channels, or the process itself. These carry
# a second gate on top of the capability: the human asking must own the agent
# (``self_config_tools._ownership_gate``). The read-only siblings stay open to
# anyone who may chat with the agent, so it can still answer "what could you
# connect to" for a user who is not entitled to make it happen.
#
# Three lists must agree on exactly these names, and
# ``tests/test_agent_autonomy.py`` pins all three together: this constant, the
# ``write_external`` block of ``security/hitl_policy.CATEGORY_TOOLS``, and the
# ``@tool_metadata(capability="write_external")`` tags in the toolkit itself.
SELF_CONFIG_MUTATING_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "self_set_model",
        "self_update_prompt",
        "self_set_external_access",
        "self_restart",
        "self_add_subagent",
        "self_enable_skill",
        "self_disable_skill",
        "self_add_mcp_server",
        "self_remove_mcp_server",
        "self_authorize_mcp_server",
        "self_add_toolkit",
        "self_remove_toolkit",
        "self_connect_platform",
        "self_connect_whatsapp",
        "self_connect_weixin",
    }
)

# One-liners for the internal-tools catalog (dashboard tool browser).
SELF_CONFIG_TOOL_DESCRIPTIONS: dict[str, str] = {
    "self_get_config": "Report this agent's own configuration and what it may change about itself.",
    "self_list_archetypes": "Browse the archetype library of ready-made experts and teams.",
    "self_read_archetype": "Read one archetype's full definition (prompt, tools, description).",
    "self_list_models": "List the models this agent could switch itself to, cost-ranked.",
    "self_set_external_access": (
        "Publish or unpublish this agent for outside callers over MCP / A2A (admin human only)."
    ),
    "self_set_model": "Switch this agent's own model (validated; applies after restart).",
    "self_update_prompt": "Rewrite this agent's own system prompt (revision-logged).",
    "self_restart": "Restart this agent so saved configuration changes take effect.",
    "self_list_skills": "List this agent's skills and which of them are switched on.",
    "self_enable_skill": "Switch one installed skill back on for this agent.",
    "self_disable_skill": "Switch one skill off for this agent (never deletes it).",
    "self_list_subagents": "List this agent's own subagents.",
    "self_add_subagent": "Add a subagent to this agent (custom, or drafted from an archetype).",
    "self_search_mcp_directory": "Search the MCP directory for services to connect.",
    "self_list_mcp_servers": "List the MCP servers configured on this agent.",
    "self_list_mcp_tools": (
        "List the tools one of this agent's MCP servers offers, enabled and switched off."
    ),
    "self_reload_mcp_servers": (
        "Reconnect this agent's MCP servers now and re-bind their tools, without a restart."
    ),
    "self_add_mcp_server": "Add an MCP server to this agent's own configuration.",
    "self_remove_mcp_server": "Remove an MCP server from this agent's configuration.",
    "self_authorize_mcp_server": "Start OAuth sign-in for one of this agent's MCP servers.",
    "self_find_integration": (
        "Find every supported way to connect a service (channel, MCP server, "
        "built-in toolkit), ranked, with the exact connect steps."
    ),
    "self_add_toolkit": "Add a built-in toolkit's tools to this agent's own tools list.",
    "self_remove_toolkit": "Remove a built-in toolkit's tools from this agent's tools list.",
    "self_inspect_external": (
        "Read an external project's documentation and classify it (MCP server, "
        "skill, CLI) — nothing is installed or run."
    ),
    "self_list_platforms": "List the messaging platforms this agent could be connected to.",
    "self_connect_platform": "Wire a messaging channel (Telegram, Slack, …) onto this agent.",
    "self_connect_whatsapp": "Pair this agent with WhatsApp via a QR code shown in the chat.",
    "self_connect_weixin": "Pair this agent with WeChat (Weixin) via a QR code shown in the chat.",
}

# The knob that turns a capability's tools back on, for the catalog's
# "requires" note (how-to-enable, not just "off").
SELF_CONFIG_TOOL_REQUIRES: dict[str, str] = {
    name: (
        "any Autonomy capability (agent Autonomy settings / Config → Agent self-configuration)"
        if cap == "*"
        else f"Autonomy capability '{cap}' (agent Autonomy settings / Config → Agent self-configuration)"
    )
    for name, cap in SELF_CONFIG_TOOL_CAPABILITY.items()
}


# ── Magic links ─────────────────────────────────────────────────────────────
# One capability for the whole family: every magic-link tool mints a page
# that acts on behalf of a human, so "may this bot send links?" is one
# question, not seven. Kept apart from SELF_CONFIG_TOOL_CAPABILITY on purpose.
MAGIC_LINK_CAPABILITY = "magic_links"

MAGIC_LINK_TOOL_CAPABILITY: dict[str, str] = {
    "magic_link_connect_service": MAGIC_LINK_CAPABILITY,
    "magic_link_request_secret": MAGIC_LINK_CAPABILITY,
    "magic_link_edit_instructions": MAGIC_LINK_CAPABILITY,
    "magic_link_request_file_edit": MAGIC_LINK_CAPABILITY,
    "magic_link_connect_channels": MAGIC_LINK_CAPABILITY,
    "magic_link_open_dashboard": MAGIC_LINK_CAPABILITY,
    "magic_link_mint_access_token": MAGIC_LINK_CAPABILITY,
    "magic_link_status": MAGIC_LINK_CAPABILITY,
}

MAGIC_LINK_TOOL_NAMES: tuple[str, ...] = tuple(MAGIC_LINK_TOOL_CAPABILITY)

MAGIC_LINK_TOOL_DESCRIPTIONS: dict[str, str] = {
    "magic_link_connect_service": (
        "Send a PIN-protected link that signs the user in to a service "
        "(OAuth / device flow) from their own browser."
    ),
    "magic_link_request_secret": (
        "Send a secure form where the user types an API key straight into the "
        "vault — the value never passes through the chat."
    ),
    "magic_link_mint_access_token": (
        "Send an admin a PIN-protected page that mints a bearer token for outside "
        "callers (MCP / A2A) and shows it once — never in the chat."
    ),
    "magic_link_edit_instructions": (
        "Send a one-page editor for an agent's system prompt; saving applies live."
    ),
    "magic_link_request_file_edit": (
        "Send a one-page editor for one workspace file (absolute paths: admin only)."
    ),
    "magic_link_connect_channels": (
        "Send a setup page that connects messengers (WhatsApp/WeChat by QR, "
        "Telegram/Discord/Slack by bot token) onto an agent."
    ),
    "magic_link_open_dashboard": (
        "Send a one-time link that signs the owner into the dashboard on their "
        "phone, focused on one section."
    ),
    "magic_link_status": "Check whether a magic link you sent has been used.",
}

_MAGIC_LINK_REQUIRES = (
    "Autonomy capability 'magic_links' (agent Autonomy settings / Config → Agent "
    "self-configuration); links also need Config → Magic links to be enabled"
)
MAGIC_LINK_TOOL_REQUIRES: dict[str, str] = dict.fromkeys(
    MAGIC_LINK_TOOL_NAMES, _MAGIC_LINK_REQUIRES
)


def capability_on(name: str, config: AgentConfig) -> bool:
    """Whether the autonomy capability behind tool ``name`` resolves ON."""
    from olano.config.system_settings import (
        AUTONOMY_CAPABILITIES,
        autonomy_allowed,
        load_agent_autonomy_settings,
    )

    cap = SELF_CONFIG_TOOL_CAPABILITY.get(name) or MAGIC_LINK_TOOL_CAPABILITY.get(name)
    if cap is None:
        return False
    settings = load_agent_autonomy_settings()
    if cap == "*":
        return any(autonomy_allowed(c, config, settings=settings) for c in AUTONOMY_CAPABILITIES)
    return autonomy_allowed(cap, config, settings=settings)


def available_self_config_tool_names(config: AgentConfig) -> list[str]:
    """The ``self_*`` names to grant ``config`` implicitly, capabilities permitting.

    Empty when ``default_internal_tools`` is off — that knob turns off every
    implicit grant; an explicit ``tools:`` entry binds unconditionally (and is
    skipped here so it isn't granted twice), and ``disabled_tools`` wins as
    always.
    """
    if not config.default_internal_tools:
        return []
    explicit = set(config.tools or [])
    disabled = set(config.disabled_tools or [])
    return [
        name
        for name in SELF_CONFIG_TOOL_NAMES
        if name not in explicit and name not in disabled and capability_on(name, config)
    ]


def available_magic_link_tool_names(config: AgentConfig) -> list[str]:
    """The ``magic_link_*`` names to grant ``config`` implicitly.

    Same rules as :func:`available_self_config_tool_names`: nothing when
    ``default_internal_tools`` is off, an explicit ``tools:`` entry is skipped
    (it binds unconditionally — AgentFather lists the family explicitly), and
    ``disabled_tools`` wins. One capability decides for the whole family.
    """
    if not config.default_internal_tools:
        return []
    explicit = set(config.tools or [])
    disabled = set(config.disabled_tools or [])
    return [
        name
        for name in MAGIC_LINK_TOOL_NAMES
        if name not in explicit and name not in disabled and capability_on(name, config)
    ]
