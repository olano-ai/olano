"""``olano_admin`` toolkit — manage olano from inside an agent.

These tools wrap :mod:`olano.admin` (the validated management service layer)
so the **AgentFather** agent can create/update/delete agents, scaffold
archetypes, wire tools and MCP servers, author skills, read/patch the home
config, manage the deployment's human users and per-agent access, watch the
disk, inspect logs, and update olano itself.

The human-user tools are additionally ``owner_only`` via ``@tool_metadata``
(reads included — a member with chat access to AgentFather must not be able to
enumerate accounts, let alone mint an invite link), and their mutations carry
``capability="mutating"`` so the group-chat and member read-only layers of the
security pipeline apply beneath the owner gate.

**The logs / sessions / diagnostics block is ``owner_only`` for the same
reason.** ``olano.log`` is one file for the whole deployment, so tailing it
hands the caller every *other* agent's errors, MCP endpoints and chat ids —
and ``olano_read_session`` hands them another agent's transcript verbatim.
Without the gate those inferred as ``read_only``/``owner_only=False`` (there
is no metadata to inherit; ``ToolMetadata`` defaults are permissive), so on any
agent holding the toolkit a non-admin member, a peer over ``talk_to_agent`` or
an inbound MCP/A2A caller could read the fleet's logs. ``is_owner`` is
``user.is_admin`` on the dashboard and ``True`` on a single-user box, so an
operator loses nothing; ``RequestContext`` also defaults it ``True``, so an
unattributed scheduled/cognition turn is unaffected.

All mutating tools (``olano_create_agent``, ``olano_add_mcp_server`` …)
are registered in :data:`olano.security.hitl_policy.CATEGORY_TOOLS` under
``write_external``, and ``olano_update`` under ``shell`` — so an agent with
``hitl_write_tools`` / ``hitl_shell`` enabled surfaces a human approval card
before any change lands. Read-only tools never trigger HITL.
``olano_run_agent_command`` (run a command inside another agent's
workspace/sandbox) sits in its own ``cross_agent_exec`` category, which is in
every security profile and cannot be un-gated except by the ``hitl_enabled``
master switch — each command is approved by a human individually.

Each tool returns a JSON string so the model gets structured, parseable output.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from langchain_core.tools import tool

from olano import admin
from olano.security.tool_metadata import tool_metadata

logger = logging.getLogger(__name__)


def _ok(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _err(exc: Exception | str) -> str:
    msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return json.dumps({"error": msg}, ensure_ascii=False)


def _tool_names_in(payload: Any) -> set[str]:
    """Every tool name a config payload would grant: its own ``tools`` and each
    ``subagents[*].tools``. A subagent runs inside the parent's graph with its
    own tool list, so a fleet tool on a subagent is a fleet tool on the agent."""
    names: set[str] = set()
    if not isinstance(payload, dict):
        return names
    tools = payload.get("tools")
    if isinstance(tools, list):
        names.update(str(t) for t in tools)
    subs = payload.get("subagents")
    if isinstance(subs, list):
        for sub in subs:
            # A dict from a tool call, or a SubAgentConfig from a config dump.
            sub_tools = sub.get("tools") if isinstance(sub, dict) else getattr(sub, "tools", None)
            if isinstance(sub_tools, list):
                names.update(str(t) for t in sub_tools)
    return names


def _privileged_tools_denial(agent: str, *payloads: Any, existing: bool = True) -> str | None:
    """Refuse a config write that NEWLY grants a deny-listed toolkit's tools,
    unless an administrator is the person on this turn.

    ``integrations.expand_toolkit`` already refuses those toolkits by *name*, so
    neither ``self_add_toolkit`` nor ``olano_add_toolkit`` can hand out
    ``olano_admin``. But ``tools:`` holds individual tool names, and nothing
    checked them — so ``olano_update_agent(name, {"tools": [...,
    "olano_tail_log"]})`` granted an ordinary agent the fleet-wide log reader,
    around the rule rather than through it. The same names can arrive through
    ``extra`` on create, and through ``subagents[*].tools`` on either — so every
    payload is walked (``_tool_names_in``), not just a ``tools`` argument.

    Only *newly added* names are gated: AgentFather's own config legitimately
    lists this toolkit, and re-saving it (any dashboard save resends the whole
    list) must not fail. ``existing=False`` treats every name as new (a fresh
    subagent). ``None`` when there is nothing to gate or an admin is present;
    otherwise the human gate's JSON refusal, naming the tools.
    """
    try:
        from olano.admin.integrations import operator_granted_tool_names, privileged_tool_names

        fleet_scoped = privileged_tool_names()
        operator_granted = operator_granted_tool_names()
    except Exception:  # a stale/unreadable index must not block an ordinary edit
        logger.debug("privileged-tool guard unavailable", exc_info=True)
        return None
    wanted: set[str] = set()
    for payload in payloads:
        wanted |= _tool_names_in(payload)
    wanted &= fleet_scoped | operator_granted
    if not wanted:
        return None
    if existing:
        try:
            wanted -= _tool_names_in(admin.get_agent(agent))
        except Exception:
            pass  # new agent (or unreadable) — every name counts as newly added
    if not wanted:
        return None

    from olano.tools._human_gate import admin_human_denial

    denial = admin_human_denial()
    if denial is None:
        return None
    try:
        payload = json.loads(denial)
    except Exception:
        payload = {"error": denial}
    payload["refused_tools"] = sorted(wanted)
    if wanted & fleet_scoped:
        payload["why"] = (
            "these tools are fleet-scoped (deployment logs, other agents' sessions, "
            "credentials, cross-agent execution). Granting them to an agent is an "
            "administrator's decision, made from the dashboard's Tools tab."
        )
    else:
        payload["why"] = (
            "these tools manage the WhatsApp Business account itself (on Twilio or Meta's "
            "Cloud API: its WhatsApp templates, numbers, business profile, and on Twilio its "
            "balance and usage), which every agent on that account shares. "
            "Granting them to an agent is an administrator's decision, made from the "
            "dashboard's connector panel or Tools tab."
        )
    return json.dumps(payload, ensure_ascii=False)


def _fleet_access_denial(agent: str, payload: Any) -> str | None:
    """Refuse a config write that NEWLY turns on ``fleet_access``, unless an
    administrator is the person on this turn.

    ``fleet_access`` opens every agent's directory, the common skills and the
    logs to the agent's shell (``security/fleet_access.py``), which is the same
    class of grant as the ``olano_admin`` toolkit, so it takes the same gate as
    ``_privileged_tools_denial``. Re-saving a config that already has it on
    (AgentFather's own, from the dashboard) and turning it off are not gated.
    """
    if not isinstance(payload, dict) or not payload.get("fleet_access"):
        return None
    try:
        if bool((admin.get_agent(agent) or {}).get("fleet_access")):
            return None
    except Exception:
        pass  # new agent (or unreadable): the grant counts as newly added

    from olano.tools._human_gate import admin_human_denial

    denial = admin_human_denial()
    if denial is None:
        return None
    try:
        body = json.loads(denial)
    except Exception:
        body = {"error": denial}
    body["refused_fields"] = ["fleet_access"]
    body["why"] = (
        "fleet_access lets an agent's shell and file tools reach every agent's "
        "directory, the common skills and the deployment logs. Granting it is an "
        "administrator's decision."
    )
    return json.dumps(body, ensure_ascii=False)


def _observability_denial(agent: str, payload: Any) -> str | None:
    """Refuse a config write that CHANGES ``observability``, unless an
    administrator is the person on this turn.

    The block decides whether an agent's conversations are exported to an
    outside Langfuse project, and in ``full`` capture with their text. That is
    an administrator's decision in either direction (switching tracing off
    hides what an agent does from whoever reviews its traces), so any change
    is gated; re-sending the block unchanged (a dashboard save, AgentFather
    re-listing a config) is not.
    """
    if not isinstance(payload, dict) or "observability" not in payload:
        return None
    from olano.config.agent_config import AgentObservabilityConfig

    def _norm(value: Any) -> Any:
        try:
            return AgentObservabilityConfig.model_validate(value or {}).model_dump()
        except Exception:
            return {"invalid": repr(value)}

    try:
        current = (admin.get_agent(agent) or {}).get("observability")
    except Exception:
        current = None  # new agent (or unreadable): any non-default block is a change
    if _norm(payload.get("observability")) == _norm(current):
        return None

    from olano.tools._human_gate import admin_human_denial

    denial = admin_human_denial()
    if denial is None:
        return None
    try:
        body = json.loads(denial)
    except Exception:
        body = {"error": denial}
    body["refused_fields"] = ["observability"]
    body["why"] = (
        "observability decides whether this agent's conversations are sent to an "
        "outside Langfuse project, and how much of them. Changing it is an "
        "administrator's decision, in the dashboard (Config → Observability, or the "
        "agent's settings)."
    )
    return json.dumps(body, ensure_ascii=False)


# ── Agents ───────────────────────────────────────────────────────────────────


@tool
def olano_list_agents() -> str:
    """List all configured olano agents with a short summary of each."""
    try:
        return _ok(admin.list_agents())
    except Exception as exc:
        return _err(exc)


@tool
def olano_get_agent(name: Annotated[str, "Agent name"]) -> str:
    """Return the full validated agent.json5 config for one agent."""
    try:
        return _ok(admin.get_agent(name))
    except Exception as exc:
        return _err(exc)


@tool
def olano_create_agent(
    name: Annotated[str, "Unique agent name (slug). Permanent — cannot be renamed later"],
    display_name: Annotated[
        str, "Editable display name, free text (e.g. 'Merli the Analyst'). Empty ⇒ the slug"
    ] = "",
    title: Annotated[
        str,
        "The agent's position, shown under its name and told to the model "
        "(e.g. 'Recruitment Specialist'). Editable later via org.title. Empty ⇒ none",
    ] = "",
    description: Annotated[str, "One-line description"] = "",
    model: Annotated[str, "provider:model, e.g. anthropic:claude-sonnet-4-6"] = "",
    system_prompt: Annotated[str, "System prompt / persona"] = "",
    tools: Annotated[
        list[str] | None,
        "Individual TOOL names to enable, e.g. ['github_create_issue', 'web_search']. "
        "A toolkit/bundle name ('github', 'web') is NOT accepted here — add a whole "
        "toolkit with olano_add_toolkit after creation.",
    ] = None,
    extra: Annotated[
        dict[str, Any] | None,
        "Any other agent.json5 fields (security, memory, channels, backend, …)",
    ] = None,
    overwrite: Annotated[bool, "Replace an existing agent of the same name"] = False,
) -> str:
    """Create a new agent config from scratch (validated before saving).

    ``tools`` takes individual tool names only (the registry resolves names,
    never bundle names); grant a whole toolkit with ``olano_add_toolkit``.
    Fleet-scoped tools (the ``olano_admin`` toolkit, the vault, …) are refused
    here unless an administrator is on the turn — see ``_privileged_tools_denial``.
    """
    denial = (
        _privileged_tools_denial(name, {"tools": tools}, extra)
        or _fleet_access_denial(name, extra)
        or _observability_denial(name, extra)
    )
    if denial:
        return denial
    try:
        return _ok(
            admin.create_agent(
                name,
                model=model or None,
                display_name=display_name,
                title=title,
                description=description,
                system_prompt=system_prompt,
                tools=tools,
                extra=extra,
                overwrite=overwrite,
            )
        )
    except Exception as exc:
        return _err(exc)


@tool
def olano_update_agent(
    name: Annotated[str, "Agent name"],
    updates: Annotated[dict[str, Any], "Top-level agent.json5 keys to replace"],
) -> str:
    """Merge updates into an existing agent config and re-validate it.

    A ``tools`` key that newly grants a fleet-scoped tool, ``fleet_access``
    newly set to true, or any change to ``observability`` needs an
    administrator on the turn: see ``_privileged_tools_denial``,
    ``_fleet_access_denial`` and ``_observability_denial``.
    """
    denial = (
        _privileged_tools_denial(name, updates)
        or _fleet_access_denial(name, updates)
        or _observability_denial(name, updates)
    )
    if denial:
        return denial
    try:
        return _ok(admin.update_agent(name, updates))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_get_browser_auto_approvals(name: Annotated[str, "Agent name"]) -> str:
    """An agent's browser auto-approval rules: which kinds of action (send,
    submit, signup) on which sites its browser does without asking. Everything
    else, and every purchase, payment or deletion, asks every time.
    """
    try:
        from olano.browser.auto_approve import KIND_LABELS, current_rules

        return _ok({"agent": name, "rules": current_rules(name), "action_kinds": KIND_LABELS})
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="high", requires_hitl=False, owner_only=True)
@tool
def olano_set_browser_auto_approvals(
    name: Annotated[str, "Agent name"],
    rules: Annotated[
        list[dict[str, Any]],
        "The complete new list (replaces the current one; [] removes all). Each rule: "
        "{site: 'linkedin.com', actions: ['send'|'submit'|'signup', ...], in_background: false}",
    ],
) -> str:
    """Replace an agent's browser auto-approval rules.

    Refused unless the person on this turn is an admin or an owner of that agent,
    in a conversation they started, and they are asked to confirm the change in
    an approval card. Purchases, payments, transfers, donations and deletions
    always ask regardless. Applies to the agent's next browser action.
    """
    from olano.browser import auto_approve, consequence

    denial = auto_approve.editor_denial(name)
    if denial:
        return _err(f"refused: {denial}")
    try:
        before = auto_approve.current_rules(name)
        after = auto_approve.validate_rules(rules)
    except Exception as exc:
        return _err(exc)
    if after == before:
        return _ok({"agent": name, "rules": before, "changed": False})
    stop = consequence.ask_rule_change(
        "olano_set_browser_auto_approvals",
        action=f"change which browser actions {name} may do without asking",
        details=auto_approve.summarize_change(before, after),
    )
    if stop:
        return _err(stop)
    try:
        saved = auto_approve.save_rules(name, after)
    except Exception as exc:
        return _err(exc)
    return _ok({"agent": name, "rules": saved, "changed": True, "applies": "immediately"})


@tool
def olano_set_agent_external_access(
    name: Annotated[str, "Agent name"],
    mcp: Annotated[
        bool | None,
        "True/False to publish/unpublish the agent as a tool on the deployment's MCP endpoint "
        "for outside platforms (Claude, ChatGPT, Cursor, agent frameworks). None = unchanged.",
    ] = None,
    a2a: Annotated[
        bool | None,
        "True/False to publish/unpublish the agent over the A2A protocol for outside agents. "
        "None = unchanged.",
    ] = None,
    description: Annotated[
        str | None,
        "What the agent can do for an OUTSIDE caller, 1-3 plain sentences (capabilities, "
        "inputs it needs, limits). Required while mcp or a2a is on. NOT the system prompt.",
    ] = None,
) -> str:
    """Let systems outside this deployment reach an agent (MCP / A2A), or stop them.

    This is the inbound direction — someone else sending OUR agent messages — not
    the agent's own MCP servers or remote agents. Refused unless an administrator
    is the person on this turn. The home-wide switches (Config → External access)
    must also be on for callers to get through, and callers still need a bearer
    token or an approved OAuth sign-in. Applies on the agent's next restart
    (olano_restart_agent).
    """
    from olano.tools._human_gate import admin_human_denial

    denial = admin_human_denial()
    if denial:
        return denial
    try:
        result = admin.set_external_access(name, mcp=mcp, a2a=a2a, description=description)
        try:
            from olano.config.system_settings import load_external_access_settings

            settings = load_external_access_settings()
            off = [
                label
                for label, on, flag in (
                    ("MCP", settings.mcp.enabled, result["external_access"].get("mcp")),
                    ("A2A", settings.a2a.enabled, result["external_access"].get("a2a")),
                )
                if flag and not on
            ]
            if off:
                result["note"] = (
                    f"{' and '.join(off)} is off home-wide (Config → External access); the agent "
                    "is flagged but unreachable until an admin turns the switch on."
                )
        except Exception:  # pragma: no cover - advisory only
            pass
        return _ok(result)
    except Exception as exc:
        return _err(exc)


@tool
def olano_delete_agent(
    name: Annotated[str, "Agent name"],
    purge_sessions: Annotated[bool, "Also delete JSONL session transcripts"] = True,
) -> str:
    """Delete an agent config (and, by default, its session transcripts)."""
    try:
        return _ok(admin.delete_agent(name, purge_sessions=purge_sessions))
    except Exception as exc:
        return _err(exc)


# ── Archetypes ─────────────────────────────────────────────────────────────--


@tool
def olano_list_archetypes() -> str:
    """List the pre-built archetype catalog (specialists and teams)."""
    try:
        return _ok(admin.list_archetypes())
    except Exception as exc:
        return _err(exc)


@tool
def olano_read_archetype(
    name: Annotated[str, "Archetype name or file stem (e.g. CustomerServiceAgent, lionFinance)"],
) -> str:
    """Read one archetype template in full — its actual prompt, tools, and config.

    Use this whenever you need to know what an archetype really ships with
    (default tools, system prompt, memory/privacy model, backend, security)
    *before* recommending or installing it. ``olano_list_archetypes`` only
    returns the picker fields (label, description, emoji, category); this is the
    read path for everything else. Archetype templates live outside every
    agent's filesystem reach, so this tool is the only way to see them.

    Works for archetypes that are NOT installed as agents — it reads the
    template, never a deployed ``agent.json5`` (use ``olano_get_agent`` for
    that). Read-only: nothing is created or changed.

    The reply carries ``config`` (the template as authored), ``tools`` (the
    effective tool list an installed agent ends up with — the template's tools
    merged with the universal defaults, minus ``disabled_tools``, plus the
    deepagents built-ins) and ``auto_generated_tools`` (memory/RAG/graph tools
    the builder wires from the memory config rather than from ``tools``, e.g.
    the per-customer ``remember`` / ``recall`` / ``forget_me`` surface).
    """
    try:
        return _ok(admin.read_archetype(name))
    except Exception as exc:
        return _err(exc)


@tool
def olano_scaffold_archetype(
    archetype: Annotated[str, "Archetype name (e.g. lionDev, lionFinance)"],
    as_name: Annotated[
        str,
        "Permanent handle (slug) for the installed agent. Leave empty: it is minted "
        "from display_name (e.g. 'Hillary' → hillary), else defaults to the archetype",
    ] = "",
    model: Annotated[str, "Override model (provider:model)"] = "",
    overwrite: Annotated[bool, "Overwrite an existing agent"] = False,
    display_name: Annotated[
        str,
        "What the agent is called, free text (e.g. 'Hillary'). Empty ⇒ the archetype's label",
    ] = "",
    title: Annotated[
        str,
        "The agent's position, shown under its name. Empty ⇒ the archetype's own label "
        "(e.g. 'Recruitment Specialist') — so a named install keeps its role",
    ] = "",
) -> str:
    """Install an archetype as a new agent with a full workspace scaffold.

    Give the person's name for the agent as ``display_name`` and let the
    handle be minted — never ask a person to invent a slug.
    """
    try:
        return _ok(
            admin.scaffold_from_archetype(
                archetype,
                as_name=as_name or None,
                model=model or None,
                overwrite=overwrite,
                display_name=display_name or None,
                title=title or None,
            )
        )
    except Exception as exc:
        return _err(exc)


# ── Tools ──────────────────────────────────────────────────────────────────--


@tool
def olano_list_models(
    connected_only: Annotated[bool, "Hide providers that aren't usable right now"] = True,
) -> str:
    """List usable providers and their models, cost-ranked, with a recommendation.

    Call this **before creating any agent**. It is the only way to know what
    this install can actually run: a provider counts as connected when its API
    key is in the environment *or* the vault, when OpenAI Codex holds a live
    OAuth session, or when Ollama is serving local models.

    Each model carries ``cost_class`` (``included`` for subscription/local
    models with no per-token charge, ``metered`` with per-million-token prices,
    ``unknown`` when the catalog has no price), ``tier``, ``context_window`` and
    a ``cost_rank`` (0 = cheapest). The reply's ``suggested_cheap`` and
    ``suggested_default`` are computed here, so they are stable and do not
    depend on you recognising a model name.

    Never write a model string from memory. Codex model IDs come from the
    user's live account and change without a olano release, so a name you
    "know" may not exist here — and one that does exist may be missing from
    your training data entirely.
    """
    try:
        return _ok(admin.list_models(connected_only=connected_only))
    except Exception as exc:
        return _err(exc)


@tool
def olano_list_tools() -> str:
    """List every known tool name and the set currently globally disabled."""
    try:
        return _ok(admin.list_tools())
    except Exception as exc:
        return _err(exc)


@tool
def olano_list_bundles() -> str:
    """List every tool bundle and the tools it expands to."""
    try:
        return _ok(admin.list_bundles())
    except Exception as exc:
        return _err(exc)


@tool
def olano_list_integrations() -> str:
    """List built-in integration tool-packs (toolkits + bundles) for ``tools``.

    A concise, high-level catalog of the per-service toolkits and curated
    composite bundles you can add to an agent's ``tools`` list, each with a tool
    count. Use this to ground tool recommendations; drill into the member tools
    with ``olano_list_bundles`` / ``olano_describe_tool``. (For ready-made
    agents — experts, teams, and integration specialists — see
    ``olano_list_builtin_agents``.)
    """
    try:
        return _ok(admin.list_integrations())
    except Exception as exc:
        return _err(exc)


@tool
def olano_describe_tool(name: Annotated[str, "Tool name"]) -> str:
    """Return a tool's one-line description, discovery tags, and how it is wired."""
    try:
        return _ok(admin.describe_tool(name))
    except Exception as exc:
        return _err(exc)


@tool
def olano_find_tools(
    tag: Annotated[str, "Discovery tag, e.g. customer_service, privacy, gdpr, memory"],
) -> str:
    """Find tools by discovery tag when you know the *intent*, not the tool name.

    Answers questions like "which tools store customer data privately?" without
    guessing names. Crucially it also surfaces builder-injected tools that
    ``olano_list_tools`` cannot see — tools bound to a live agent by a config
    flag rather than resolved from the registry (the per-customer memory surface
    ``remember`` / ``recall`` / ``recall_all`` / ``forget_me`` is the main
    example). Those carry a ``provided_by`` note naming the config that wires
    them, so you know what to enable rather than which tool name to add.

    Pass an unknown tag to get the list of tags that do exist.
    """
    try:
        return _ok(admin.find_tools(tag))
    except Exception as exc:
        return _err(exc)


@tool
def olano_set_tool_enabled(
    name: Annotated[str, "Tool name"],
    enabled: Annotated[bool, "True to enable, False to globally disable"],
) -> str:
    """Globally enable or disable a tool across all agents."""
    try:
        return _ok(admin.set_tool_enabled(name, enabled))
    except Exception as exc:
        return _err(exc)


# ── MCP servers ──────────────────────────────────────────────────────────────


@tool
def olano_list_mcp_servers(agent: Annotated[str, "Agent name"]) -> str:
    """List the MCP servers configured on an agent."""
    try:
        return _ok(admin.list_mcp_servers(agent))
    except Exception as exc:
        return _err(exc)


@tool
async def olano_test_mcp_server(
    agent: Annotated[str, "Agent name"],
    url: Annotated[str, "MCP server URL already configured on the agent"],
) -> str:
    """Probe a configured MCP server (connect, list tools, detect OAuth)."""
    try:
        return _ok(await admin.test_mcp_server(agent, url))
    except Exception as exc:
        return _err(exc)


@tool
def olano_search_mcp_directory(
    query: Annotated[str, "What to look for, e.g. 'hubspot', 'payments', 'issue tracker'"] = "",
    kind: Annotated[str, "Filter: 'preset' (curated) or 'example'. Empty = both."] = "",
    category: Annotated[str, "Optional category filter, e.g. 'crm'"] = "",
    limit: Annotated[int, "Max results (default 20)"] = 20,
) -> str:
    """Search the MCP server directory for a platform the user wants to connect.

    Use this BEFORE ``olano_add_mcp_server`` — always. It returns the verified
    endpoint URL, transport, auth mode and the credential names each server
    expects. Never write an MCP URL from memory: a URL that is subtly wrong is
    saved happily and only fails later, in front of the user.

    An empty ``query`` lists the directory. Results are trimmed to what informs
    the choice: slug, name, description, transport, auth, oauthClient, url,
    envKeys, and where to get credentials (homepageUrl / appConsoleUrl).
    """
    try:
        return _ok(
            {
                "servers": admin.search_mcp_directory(
                    query,
                    kind=kind or None,
                    category=category or None,
                    limit=limit,
                )
            }
        )
    except Exception as exc:
        return _err(exc)


@tool
def olano_get_mcp_directory_entry(
    slug: Annotated[str, "Directory slug from olano_search_mcp_directory, e.g. 'hubspot'"],
) -> str:
    """Read one MCP directory entry in full before adding it to an agent.

    Not to be confused with ``olano_list_mcp_servers``, which lists the servers
    already configured on an agent. This reads the catalog of servers that
    exist.
    """
    try:
        return _ok(admin.get_mcp_directory_entry(slug))
    except Exception as exc:
        return _err(exc)


@tool
def olano_add_mcp_server(
    agent: Annotated[str, "Agent name"],
    url: Annotated[str, "MCP server URL (or stdio command target)"],
    transport: Annotated[str, "sse | http | stdio"] = "sse",
    auth: Annotated[str, "none | oauth"] = "none",
    oauth_scope: Annotated[str, "Optional OAuth scope"] = "",
    oauth_client_id: Annotated[str, "Pre-registered OAuth client id (servers without DCR)"] = "",
    oauth_client_secret: Annotated[str, "Pre-registered OAuth client secret"] = "",
    command: Annotated[str, "Executable for stdio transport"] = "",
    command_args: Annotated[
        list[str] | None, "Arguments for the stdio command, e.g. ['-y', 'some-mcp-server']"
    ] = None,
    env: Annotated[
        dict[str, str] | None,
        "Environment for a stdio server. Use '${vault:NAME}' to inject a stored "
        "credential instead of a literal, e.g. {'NOTION_API_KEY': '${vault:NOTION_API_KEY}'}.",
    ] = None,
    headers: Annotated[
        dict[str, str] | None,
        "Static headers for an http/sse server — the usual way to pass an API key. "
        "Use '${vault:NAME}', e.g. {'Authorization': 'Bearer ${vault:ACME_API_KEY}'}.",
    ] = None,
) -> str:
    """Add (or replace by URL) an MCP server on an agent.

    Look the server up with ``olano_search_mcp_directory`` first so the URL and
    transport are the verified ones, not remembered ones.

    Replacing is a FULL replace: a second call for the same URL overwrites the
    whole entry, so re-send every field you want kept (``headers``, ``env``,
    OAuth client credentials, …), not just the one you are changing.

    This records the configuration only, and it must happen **before** any
    sign-in step — authorizing a server that is not in the config stores tokens
    the agent will never load.

    **Servers that use OAuth** (``auth="oauth"``): follow this with
    ``olano_authorize_mcp_server(agent, url)``, which returns a provider sign-in
    URL you send the user over any channel, or ``magic_link_connect_service(url, agent)``,
    which wraps the same flow in a PIN-protected link page. Either way the flow
    completes and connects on its own — do NOT tell a messenger user to "use the
    dashboard" or run the CLI. (A human *may* use the dashboard; it is not
    required.)

    **Servers that use an API key**: never put the key in this call as a
    literal. Ask for it with ``magic_link_request_secret(name=…, agent=…)`` so it goes
    from the user's browser into the vault, then reference it here as
    ``${vault:NAME}`` in ``headers`` (http/sse) or ``env`` (stdio). Both blocks
    resolve those refs per agent at connect time.
    """
    try:
        return _ok(
            admin.add_mcp_server(
                agent,
                url,
                transport=transport,
                auth=auth,
                oauth_scope=oauth_scope or None,
                oauth_client_id=oauth_client_id or None,
                oauth_client_secret=oauth_client_secret or None,
                command=command or None,
                args=list(command_args) if command_args else None,
                env=dict(env) if env else None,
                headers=dict(headers) if headers else None,
            )
        )
    except Exception as exc:
        return _err(exc)


@tool
def olano_remove_mcp_server(
    agent: Annotated[str, "Agent name"],
    url: Annotated[str, "MCP server URL to remove"],
) -> str:
    """Remove an MCP server from an agent."""
    try:
        return _ok(admin.remove_mcp_server(agent, url))
    except Exception as exc:
        return _err(exc)


# ── Integrations: channels, MCP servers, built-in toolkits ───────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def olano_find_integration(
    query: Annotated[
        str, "What to connect, in the user's words: 'github', 'notion', 'whatsapp', 'gmail'"
    ],
    agent: Annotated[str, "Agent the connection is for"],
) -> str:
    """Every SUPPORTED way to connect a service to an agent, ranked — call this FIRST for "connect X".

    Returns ``options`` in strict order of preference: (1) a messaging
    **channel**, (2) an **MCP server** from the directory (verified endpoint,
    its own sign-in), (3) a built-in **toolkit** (``auth.best`` ranks official
    Olano sign-in > the operator's own OAuth app > device code > API key).
    Each option says whether it is ``already_connected`` for that agent and
    ``on_agent`` (in its tools list), what it ``enables``, where the credential
    comes from (``how_to_get_it``), and the exact ``connect`` steps as
    ``olano_*`` / ``magic_link_*`` calls with arguments, in order, each with
    ``requires_restart``. Present the ``lead`` sentence, confirm the choice,
    then follow the chosen option's ``connect`` list verbatim.

    Anything not listed is external: it may be installed in the agent's
    sandbox, Olano never authenticates it, and it is at the user's risk — read
    its documentation with ``olano_inspect_external`` first. Read-only.
    """
    try:
        from olano.admin.integrations import lead_sentence

        result = admin.find_integration(query, agent, surface="admin")
        return _ok({"lead": lead_sentence(result), **result})
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def olano_inspect_external(
    url_or_name: Annotated[
        str, "A project URL (GitHub repo, docs page), 'owner/repo', or an npm / PyPI package name"
    ],
    agent: Annotated[
        str, "Agent the integration would be for (used to find a built-in alternative)"
    ] = "",
) -> str:
    """Read an external project's documentation and classify it — BEFORE installing anything.

    For a project ``olano_find_integration`` does not list. Fetches the
    README / package page and reports ``kind`` (``mcp_stdio`` — supported,
    add it with ``olano_add_mcp_server(transport='stdio', …)``; ``mcp_remote``
    — supported, add it as an http/sse server; ``skill``; ``cli``;
    ``unknown``), ``runs_where``, the credential names and OAuth mentions it
    documents, and ``built_in_alternative`` when a supported integration
    already covers the service — prefer that. Anything that is not an MCP
    server is external: it may be installed in the agent's sandbox, Olano never
    authenticates it, and it is at the user's risk. Nothing is installed or
    executed. Read-only.
    """
    try:
        return _ok(admin.inspect_external(url_or_name, agent))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True)
@tool
def olano_add_toolkit(
    agent: Annotated[str, "Agent name"],
    name: Annotated[
        str, "Toolkit name from olano_find_integration / olano_list_integrations, e.g. 'github'"
    ],
) -> str:
    """Add a built-in toolkit (all its tools) to an agent's ``tools`` list.

    ``tools`` holds individual tool names, so the toolkit is expanded through
    the registry and only the names the agent lacks are appended; nothing
    else changes. The result reports ``credentials`` and whether each already
    resolves for that agent — connect the missing ones with
    ``magic_link_connect_service(service, agent)`` or
    ``magic_link_request_secret(name, agent=…)`` as the finder's ``connect``
    steps say, then ``olano_restart_agent``. Refused for ``olano_admin`` (an
    agent must never be handed fleet-admin tools this way) and the
    capability-bound toolkits (``self_config``, ``magic_link``, ``secrets``,
    ``talk_to``, ``cortex``).
    """
    try:
        result = admin.add_toolkit(agent, name)
        result["applies"] = "after restart — olano_restart_agent when the user is ready"
        return _ok(result)
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=True)
@tool
def olano_remove_toolkit(
    agent: Annotated[str, "Agent name"],
    name: Annotated[str, "Toolkit name whose tools to drop, e.g. 'github'"],
) -> str:
    """Remove a built-in toolkit's tools from an agent's ``tools`` list.

    Only that toolkit's member tool names are removed. A toolkit the agent
    holds by default rather than from its ``tools`` list is switched off via
    ``disabled_tools`` (``olano_update_agent``) instead — the result says so
    when nothing was found to remove. Vault credentials are untouched.
    """
    try:
        result = admin.remove_toolkit(agent, name)
        result["applies"] = "after restart — olano_restart_agent when the user is ready"
        return _ok(result)
    except Exception as exc:
        return _err(exc)


def _connect_link_presentation(result: dict[str, Any]) -> dict[str, Any]:
    """Tell the model how this turn's surface presents the sign-in link.

    The dashboard chat (``channel == "api"``) renders any tool result carrying
    an ``authorization_url`` as a "Connect" button card on its own, so pasting
    the raw URL there duplicates it as an ugly wall of text — the model should
    just point at the button. Every other surface (Telegram, WhatsApp, CLI …)
    has no such card, so there the URL must go into the reply as plain text.
    Returns the result augmented in place with ``link_presentation`` guidance;
    best-effort — a missing request context means no guidance, not a failure.
    """
    try:
        if not result.get("authorization_url"):
            return result
        from olano.security.request_context import get_request_context

        if get_request_context().channel == "api":
            result["link_presentation"] = (
                "This chat surface already shows the user a Connect button for this "
                "sign-in link. Do NOT paste the raw authorization_url into your reply "
                "— just tell the user to click the Connect button and sign in."
            )
        else:
            result["link_presentation"] = (
                "Send the authorization_url to the user as plain text on this "
                "conversation so they can open it in their browser."
            )
    except Exception:  # never let presentation hints break the sign-in link
        logger.debug("could not resolve connect-link presentation", exc_info=True)
    return result


def _watch_authorization_to_completion(agent: str, result: dict[str, Any]) -> None:
    """Spawn the background watcher that finalizes a chat-initiated OAuth flow.

    Captures who to notify — the executing agent and the chat this turn arrived
    on — while the request context is still live, then hands off to the
    tools-layer watcher. Once the user finishes signing in out-of-band, that
    watcher hot-reloads ``agent``'s MCP tools and posts a completion notice back
    to the same chat. Best-effort: any failure here must not disturb the sign-in
    link we are about to return, and it no-ops for an already-terminal flow.
    """
    try:
        status = result.get("status")
        session_id = result.get("session_id")
        if not session_id or status in {"authorized", "error", "expired"}:
            return
        from olano.security.request_context import get_request_context
        from olano.tools._mcp_oauth_watch import spawn_watcher
        from olano.tools.talk_to import current_agent

        ctx = get_request_context()
        spawn_watcher(
            str(session_id),
            agent,
            notify_agent=current_agent.get() or "",
            notify_channel=ctx.channel,
            notify_target=ctx.session_id or "owner",
        )
    except Exception:  # never let watcher setup break returning the magic link
        logger.debug("could not start MCP OAuth completion watcher", exc_info=True)


@tool
async def olano_authorize_mcp_server(
    agent: Annotated[str, "Agent name that owns the MCP server"],
    url: Annotated[str, "MCP server URL to authorize (must already be added, auth=oauth)"],
) -> str:
    """Begin OAuth for a configured MCP server and return a sign-in URL.

    The server must already be in the agent's config — add it with
    ``olano_add_mcp_server`` first, or this raises.

    This returns the provider's raw ``authorization_url``: no PIN, no expiry
    page. ``magic_link_connect_service(url, agent)`` runs the same flow behind the standard
    magic-link page (PIN gate, live status) and is the better choice when a
    human is on the other end of a chat; prefer it unless you specifically want
    the bare URL.

    Either is a correct way to do MCP OAuth from chat. NEVER shell out to
    ``npx mcp-remote`` or any ad-hoc OAuth proxy: those bind their callback to
    ``http://127.0.0.1:<port>`` on THIS server, which the user's browser cannot
    reach — the flow looks like it works but can never complete.

    Non-blocking: returns ``session_id`` + ``authorization_url``. Send the URL to
    the user (any channel — Telegram, WhatsApp, Slack, …); they open it and sign
    in, and the callback completes automatically. You do NOT need to poll: when
    the request came from a chat, a background watcher hot-reloads the agent's
    tools and posts a ✅/⚠️ completion notice back to that same chat on its own.
    (``olano_mcp_oauth_status`` is still available if you want to check early.)

    Tokens are stored per (agent, server-url), so each agent authorizes its own
    account for the same MCP. Requires a reachable OAuth redirect — a configured
    central relay (LIONCLAW_MCP_RELAY_URL) or public origin (LIONCLAW_PUBLIC_URL).
    """
    try:
        result = await admin.start_mcp_authorization(agent, url)
        _watch_authorization_to_completion(agent, result)
        return _ok(_connect_link_presentation(result))
    except Exception as exc:
        text = str(exc)
        if "No MCP OAuth redirect target" in text or "relay secret" in text:
            return _ok(
                {
                    "error": text,
                    "remediation": (
                        "This deployment has no browser-reachable OAuth redirect. The "
                        "OPERATOR must configure one (an agent cannot work around this). "
                        "On a self-hosted install the quickest route is to connect an "
                        "Olano API key on Providers -> Olano: the box then registers "
                        "with Olano's sign-in relay within a minute, no domain needed. "
                        "Otherwise set LIONCLAW_PUBLIC_URL to this deployment's public "
                        "HTTPS origin, or LIONCLAW_MCP_RELAY_URL + a relay credential "
                        "for a relay you run, in .env or /opt/olano/olano.env, then "
                        "restart. Cloud-provisioned instances get this automatically."
                    ),
                    "agent_guidance": (
                        "Tell the user MCP sign-in is not configured on this deployment "
                        "and relay the remediation above. Do NOT attempt a workaround: "
                        "no mcp-remote, no npx, no localhost callback, no port "
                        "forwarding — a localhost callback on this server is unreachable "
                        "from the user's browser and the flow will never complete."
                    ),
                }
            )
        return _err(exc)


@tool
def olano_mcp_oauth_status(
    session_id: Annotated[str, "session_id returned by olano_authorize_mcp_server"],
) -> str:
    """Check an in-progress MCP OAuth flow (status: awaiting_authorization | authorized | error)."""
    try:
        return _ok(admin.mcp_authorization_status(session_id))
    except Exception as exc:
        return _err(exc)


# ── Platforms / connectors ─────────────────────────────────────────────────--


@tool
def olano_list_platforms() -> str:
    """List the messaging platforms / connectors an agent can be reached on.

    Returns every bootable connector (Telegram, WhatsApp, Slack, Discord, Email,
    Signal, SMS, …) with the ``type`` to put in ``agent.json5 → channels``, a
    label, the adapter kind, and the credential ``env_vars`` it needs. Entries
    carrying ``pairing: "qr"`` (WhatsApp, WeChat/Weixin) connect by QR code by
    DEFAULT — the dashboard connector form's Pair/Link button, or the agent's
    own ``self_connect_whatsapp`` / ``self_connect_weixin`` — so never lead by
    asking for their env-var credentials; those are the manual fallback. These
    are human-facing surfaces — always lock one down with per-channel
    ``allowed_users`` before wiring it. "WeChat" (``platform:weixin``) is the
    personal messenger and what a plain "connect WeChat" means; "WeCom"
    (``platform:wecom``, Enterprise WeChat / WeChat Work) is the company
    product, offer it only when named. A row's ``variants`` (Feishu's Lark)
    are the same type with the listed ``extra`` preset.
    """
    try:
        return _ok(admin.list_platforms())
    except Exception as exc:
        return _err(exc)


# ── Skills ─────────────────────────────────────────────────────────────────--


@tool
def olano_list_skills() -> str:
    """List skills discoverable in the home-managed skills directory.

    This is the live set agents load (built-ins synced from the package plus any
    authored locally). For the catalog shipped with olano itself, use
    ``olano_list_builtin_skills``.
    """
    try:
        return _ok(admin.list_skills())
    except Exception as exc:
        return _err(exc)


@tool
def olano_list_builtin_skills() -> str:
    """List the catalog of skills shipped with olano (the package built-ins).

    These ship with olano and are synced into ``<home>/skills/`` on install/
    update. Use this to recommend an existing built-in skill before authoring a
    new one with ``olano_create_skill``.
    """
    try:
        return _ok(admin.list_builtin_skills())
    except Exception as exc:
        return _err(exc)


@tool
def olano_create_skill(
    name: Annotated[str, "Skill name"],
    description: Annotated[str, "One-line description"],
    body: Annotated[str, "Markdown body that follows the frontmatter"],
    category: Annotated[str, "Skill category"] = "general",
    tier: Annotated[str, "light (prompt-injected) | subagent (delegatable)"] = "light",
    trigger: Annotated[str, "Optional hint on when to use the skill"] = "",
) -> str:
    """Author a new managed skill (writes a validated SKILL.md)."""
    try:
        return _ok(
            admin.create_skill(
                name,
                description,
                body,
                category=category,
                tier=tier,
                trigger=trigger,
            )
        )
    except Exception as exc:
        return _err(exc)


def _caller_source_roots() -> list[Any]:
    """Where the CALLING agent may install skills from: its workspace + shared.

    Agent shells are isolated from each other's workspaces, so the only trees an
    agent can have legitimately authored a skill in are its own workspace and
    the common shared folder. Restricting the source here keeps this tool from
    becoming a runtime-side "copy any host path" primitive.
    """
    from olano.config.home import get_olano_paths
    from olano.config.loader import load_agent_config
    from olano.config.workspace import get_workspace
    from olano.tools.talk_to import current_agent

    roots = [get_olano_paths().shared_dir]
    caller = current_agent.get()
    if caller:
        try:
            roots.insert(0, get_workspace(load_agent_config(caller).workspace).root)
        except Exception:
            logger.debug("could not resolve caller workspace for %s", caller, exc_info=True)
    return roots


@tool
def olano_install_skill(
    source_dir: Annotated[
        str, "Skill directory (the folder directly containing SKILL.md), inside your workspace"
    ],
    agent: Annotated[str, "Target agent name; empty installs for ALL agents (managed)"] = "",
    overwrite: Annotated[bool, "Replace an existing skill of the same name"] = False,
) -> str:
    """Install a skill DIRECTORY (SKILL.md + scripts/assets) for another agent.

    THE supported way to hand a skill you built or adapted in your own workspace
    (e.g. inside a prototype repo) to a deployed agent. Your shell cannot write
    other agents' directories — that "Permission denied" is the isolation
    boundary, not a bug — so this tool performs the copy runtime-side: it
    creates any missing directories (including the workspace of an agent that
    has never booted), copies the whole skill folder, and hands ownership to the
    target agent. Leave ``agent`` empty to install into the managed skills dir
    every agent sees. The target picks the skill up on its next restart/build.
    """
    try:
        return _ok(
            admin.install_skill(
                source_dir,
                agent=agent or None,
                overwrite=overwrite,
                allowed_source_roots=_caller_source_roots(),
            )
        )
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=True)
@tool
def olano_repair_skill_names(
    agent: Annotated[
        str, "Also scan this agent's workspace skills (empty = the managed skills dir only)"
    ] = "",
    apply: Annotated[bool, "False = dry-run report; True = actually apply the repairs"] = False,
) -> str:
    """Find — and with ``apply=true`` fix — skills whose folder name disagrees
    with their SKILL.md ``name`` (the Agent Skills rule deepagents warns about
    on every boot). A legal name keeps its name and the FOLDER is renamed to
    match; an illegal name (spaces, capitals) is set to its folder's name; a
    `disabled_skills` entry naming the old name follows it. Dry-run by default:
    run it once without ``apply``, show the plan, and only then apply."""
    try:
        return _ok(admin.repair_skill_names(agent or None, apply=apply))
    except Exception as exc:
        return _err(exc)


@tool
def olano_start_agent(name: Annotated[str, "Agent name"]) -> str:
    """Start (boot) an agent and clear its persisted pause.

    Uses the same lifecycle path as the dashboard's Start button. If the
    runtime is not running, the pause is cleared so the agent boots with it
    (the result says ``applied: next_boot``). A failed boot surfaces the real
    cause — including vault-locked / missing-secret hints.
    """
    try:
        return _ok(admin.start_agent(name))
    except Exception as exc:
        return _err(exc)


@tool
def olano_stop_agent(name: Annotated[str, "Agent name"]) -> str:
    """Stop (pause) an agent — it stays offline across runtime restarts.

    The pause persists until ``olano_start_agent`` (or the dashboard's
    Start) clears it. Stopping is non-destructive: config, workspace, memory,
    and thread history are untouched.
    """
    try:
        return _ok(admin.stop_agent(name))
    except Exception as exc:
        return _err(exc)


@tool
def olano_restart_agent(name: Annotated[str, "Agent name"]) -> str:
    """Restart an agent (stop + fresh boot) — how config changes take effect.

    Requires a live runtime. A failed re-boot surfaces the real cause,
    including vault-locked / missing-secret hints.
    """
    try:
        return _ok(admin.restart_agent(name))
    except Exception as exc:
        return _err(exc)


@tool
def olano_list_agent_workspace(
    agent: Annotated[str, "Target agent name"],
    path: Annotated[str, "Directory inside that agent's workspace; empty = its root"] = "",
) -> str:
    """List files inside ANOTHER agent's workspace (read-only, runtime-side).

    Your own shell cannot see other agents' directories — this tool is the
    supported window into them. An agent that has never booted reports
    ``exists: false`` (its workspace is provisioned on first boot); that's the
    answer, not an error.
    """
    try:
        return _ok(admin.list_agent_workspace(agent, path))
    except Exception as exc:
        return _err(exc)


@tool
def olano_read_agent_file(
    agent: Annotated[str, "Target agent name"],
    path: Annotated[str, "File path inside that agent's workspace"],
) -> str:
    """Read a text file from ANOTHER agent's workspace (read-only, size-capped)."""
    try:
        return _ok(admin.read_agent_workspace_file(agent, path))
    except Exception as exc:
        return _err(exc)


@tool
def olano_write_agent_file(
    agent: Annotated[str, "Target agent name"],
    path: Annotated[str, "File path inside that agent's workspace"],
    content: Annotated[str, "Full file content to write (UTF-8 text)"],
    overwrite: Annotated[bool, "Replace the file if it already exists"] = True,
) -> str:
    """Write a text file into ANOTHER agent's workspace (runtime-side, HITL-gated).

    The supported way to seed or fix another agent's files — your shell cannot
    reach their directories by design. Creates missing parents (including the
    workspace of an agent that has never booted) and hands ownership to the
    target agent. For whole skill directories use ``olano_install_skill``.
    """
    try:
        return _ok(admin.write_agent_workspace_file(agent, path, content, overwrite=overwrite))
    except Exception as exc:
        return _err(exc)


@tool
def olano_run_agent_command(
    agent: Annotated[str, "Target agent name — the command runs in ITS workspace/sandbox"],
    command: Annotated[str, "Shell command to run (e.g. 'python3 skills/x/scripts/migrate.py')"],
    cwd: Annotated[str, "Working dir inside that agent's workspace; empty = its root"] = "",
    timeout: Annotated[int, "Timeout in seconds (max 600)"] = 60,
) -> str:
    """Run a shell command INSIDE another agent's workspace and return the receipt.

    The execution verb your file tools were missing: seed/fix a file with
    ``olano_write_agent_file``, then actually RUN a setup script, migration,
    verifier, or cleanup here — and get a real receipt (exit code + stdout +
    stderr), not a prediction of what it would do.

    The command runs *as the target agent*, never as you:
      • If the target has a live isolated sandbox (managed deployments on
        OlanoSandbox), it runs via ``docker exec`` in THAT container — the
        target's uid-1000 user, resource caps, mounts, and no-source/no-secrets
        boundary. Start the agent first if its sandbox isn't up.
      • On an open/self-host deployment (e.g. tc-manual-host), it runs on the
        host with the working dir pinned inside the target's workspace and the
        target's own environment (config.env + its vault credentials) — the
        same shell the target's own backend already is.
      • On a managed box with no live sandbox for the target, it REFUSES rather
        than fall back to the runtime host (which holds source + secrets).

    HITL: every command surfaces an approval card (target agent + exact command
    + cwd) and is approved individually — remembered "always" rules and
    scheduled-turn blanket approvals do not apply. ``cwd`` is containment-
    checked against the target workspace; a destructive command is refused by
    the shell guard. Prefer one focused command per call.
    """
    try:
        return _ok(admin.run_agent_command(agent, command, cwd=cwd, timeout=timeout))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=True, owner_only=True)
@tool
async def olano_test_agent(
    agent: Annotated[str, "The agent to test (its name, as olano_list_agents shows it)"],
    user_message: Annotated[str, "The message to send it, written as a real user would."],
    thread_id: Annotated[
        str | None,
        "Omit to start a fresh test conversation. Pass the thread_id a previous "
        "olano_test_agent returned (same agent) to continue it, for multi-turn tests.",
    ] = None,
    system_prompt: Annotated[
        str | None,
        "Optional: test with these instructions instead of the agent's configured "
        "system prompt. Nothing is saved; its real prompt is unchanged.",
    ] = None,
    system_prompt_mode: Annotated[
        str,
        "'instructions' (default): system_prompt replaces only the agent's configured "
        "instructions, and identity, memory, tools and skills are assembled around it "
        "as in production. 'full': system_prompt is the entire prompt.",
    ] = "instructions",
    reload_config: Annotated[
        bool,
        "True: build the agent from its configuration as saved on disk now (a tool, "
        "MCP server, model or skill switch you just changed) instead of the running "
        "copy. An agent that is not running is always built from disk.",
    ] = False,
    timeout_seconds: Annotated[
        int, "Give up on the test turn after this many seconds (10 to 1800)."
    ] = 600,
) -> str:
    """Send a test message to another agent on a separate test thread and get its reply.

    Use this to TEST an agent you built or changed: a new agent, a prompt
    rewrite, a skill, a toolkit or MCP server you wired onto it. The agent runs
    a real turn on its own test thread (its users' conversations are
    untouched) and you get back its reply (`output`), every tool it called
    with what each returned (`tool_calls`), and the `thread_id`. Pass the
    thread_id back to continue the same conversation; omit it to start clean.

    The agent's tools run for real (anything it needs approval for goes to the
    approvals inbox), so test with safe inputs, and each test costs a normal
    turn of that agent's model. Every test is approved by a human individually:
    say which agent and what message you are about to send. The agent under
    test cannot reconfigure itself or start a test of its own. Administrators
    only.

    Loop: build or change the agent, test it with a realistic message, read
    `tool_calls` and `output`, adjust (olano_update_agent, olano_install_skill,
    ...) and test again with reload_config=true, then report what you tested
    and what happened.
    """
    from olano.tools import _agent_test
    from olano.tools._human_gate import admin_human_denial
    from olano.tools.talk_to import current_agent

    # The gate's own wording is about external access; keep its decision and
    # say what was actually refused.
    if admin_human_denial():
        return json.dumps(
            {
                "error": (
                    "refused: testing an agent needs an administrator in this conversation; "
                    "it is never available to another agent, a scheduled or background run, "
                    "an agent under test, or an outside caller"
                ),
                "agent_guidance": "Do not retry. Ask an administrator to request the test.",
            },
            ensure_ascii=False,
        )
    result = await _agent_test.run_agent_test(
        agent,
        caller=current_agent.get() or "",
        user_message=user_message,
        thread_id=thread_id,
        system_prompt=system_prompt,
        system_prompt_mode=system_prompt_mode,
        reload_config=reload_config,
        timeout_seconds=timeout_seconds,
    )
    return _ok(result)


# ── Subagents ────────────────────────────────────────────────────────────────


@tool
def olano_list_subagents(agent: Annotated[str, "Agent name"]) -> str:
    """List the subagents already defined on an agent.

    For the catalog of ready-made agents you can attach to any agent (or install
    standalone), use ``olano_list_builtin_agents``.
    """
    try:
        return _ok(admin.list_subagents(agent))
    except Exception as exc:
        return _err(exc)


@tool
def olano_list_builtin_agents() -> str:
    """List every ready-made agent that ships with olano — one unified catalog.

    Spans three kinds, each usable EITHER as an independent agent OR as a
    subagent of another agent: ``team`` (archetype teams = a supervisor + expert
    subagents), ``expert`` (archetype specialists), and ``integration`` (one
    ToolkitAgent per toolkit — GoogleAgent, StripeAgent, GitHubAgent…), plus the
    built-in ``ops`` OpsManager (subagent-only). Each row carries a ``ref`` (the
    identifier to reference it by), ``usable_as``, and an ``install`` hint:

      • As an agent:    archetypes via ``olano_scaffold_archetype``;
                        integration agents via ``olano_create_agent`` (+ the
                        toolkit's tools) or the dashboard Integrations tab.
      • As a subagent:  archetypes via a ``subagents`` entry with
                        ``from_archetype: <ref>``; integration agents + ops via
                        the parent's ``builtin_subagents: [<ref>]``.

    Distinct from ``olano_list_subagents`` (an agent's already-attached
    subagents). ``credential_env_vars`` flags the secrets an integration needs.
    """
    try:
        return _ok(admin.list_builtin_agents())
    except Exception as exc:
        return _err(exc)


@tool
def olano_add_subagent(
    agent: Annotated[str, "Parent agent name"],
    name: Annotated[str, "Subagent name"],
    description: Annotated[str, "Subagent purpose"],
    system_prompt: Annotated[str, "Subagent behavior directives"],
    tools: Annotated[list[str] | None, "Tool names for the subagent"] = None,
    model: Annotated[str, "Override model (defaults to parent)"] = "",
    overwrite: Annotated[bool, "Replace an existing subagent of the same name"] = False,
) -> str:
    """Add a subagent to a parent agent.

    A subagent's ``tools`` newly granting a fleet-scoped tool needs an
    administrator on the turn — see ``_privileged_tools_denial``.
    """
    denial = _privileged_tools_denial(agent, {"tools": tools}, existing=False)
    if denial:
        return denial
    try:
        return _ok(
            admin.add_subagent(
                agent,
                name,
                description=description,
                system_prompt=system_prompt,
                tools=tools,
                model=model or None,
                overwrite=overwrite,
            )
        )
    except Exception as exc:
        return _err(exc)


@tool
def olano_toggle_subagent(
    agent: Annotated[str, "Parent agent name"],
    name: Annotated[str, "Subagent name"],
    enabled: Annotated[bool, "True to enable, False to disable"],
) -> str:
    """Enable or disable an existing subagent."""
    try:
        return _ok(admin.toggle_subagent(agent, name, enabled))
    except Exception as exc:
        return _err(exc)


# ── Home config ────────────────────────────────────────────────────────────--


@tool
def olano_get_home_config() -> str:
    """Return the whole olano home config (config.yaml), defaults resolved.

    Root keys (default_agent, ports, multi_user_enabled, …) are at the top
    level; ``blocks`` carries every settings block (speech, media, web_search,
    cognition, default_models, playground, self_serve_agents, agent_autonomy,
    groups, disk_monitor, usage_budgets, magic_links, catalog_sync, credits,
    issues, revisions, prompt_refresh, mcp_health, sandbox, rate_limits,
    heartbeat) as the runtime actually resolves it. ``writable`` lists exactly
    what olano_update_home_config can patch, and ``restart_required`` the root
    keys that only apply after a runtime restart. Per-field reference:
    system/knowledge/USER_GUIDE.md §config.yaml.
    """
    try:
        return _ok(admin.get_home_config())
    except Exception as exc:
        return _err(exc)


@tool
def olano_update_home_config(
    updates: Annotated[
        dict[str, Any],
        "Root keys and/or config.yaml blocks to patch. A block value is a "
        'partial object of that block\'s fields, e.g. {"issues": '
        '{"poll_minutes": 5}, "dashboard_port": 3300}',
    ],
) -> str:
    """Patch the home config (config.yaml): root keys and every settings block.

    Root keys merge directly; a block key (speech, media, web_search,
    cognition, default_models, playground, self_serve_agents, agent_autonomy,
    groups, disk_monitor, usage_budgets, magic_links, catalog_sync, credits,
    issues, revisions, prompt_refresh, mcp_health, sandbox, rate_limits,
    heartbeat) takes a partial object that deep-merges into that block, so
    sibling fields are preserved. Values are validated/clamped the same way a
    dashboard save is. The result is honest: ``applied`` lists what was
    actually written, ``ignored`` every key that was not (with the reason),
    and ``restart_required`` any applied key needing a runtime restart. Check
    ``ignored`` before telling the user a setting changed. Secrets never go
    here — use the vault. Per-field reference: system/knowledge/USER_GUIDE.md
    §config.yaml, or olano_get_home_config's ``writable`` lists.
    """
    try:
        return _ok(admin.update_home_config(updates))
    except Exception as exc:
        return _err(exc)


# ── Human users & access ───────────────────────────────────────────────────--


def _acting_agent() -> str:
    """The calling agent — QR images land in its workspace uploads/ so chat
    media detection can render them. Falls back to AgentFather's name."""
    try:
        from olano.tools.talk_to import current_agent

        return current_agent.get() or "agentfather"
    except Exception:
        return "agentfather"


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_list_users() -> str:
    """List every dashboard human: email, @username, display name, admin or
    member, active/pending status, and their per-agent access grants."""
    try:
        return _ok(admin.list_users())
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_get_user(user: Annotated[str, "Email, @username, or user id"]) -> str:
    """One human's details: profile fields, active/pending status, and which
    agents they own or may use."""
    try:
        return _ok(admin.get_user(user))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True, owner_only=True)
@tool
def olano_create_user(
    email: Annotated[str, "Login email (unique)"],
    name: Annotated[str, "Display name; empty derives one from the handle"] = "",
    username: Annotated[
        str, "Permanent @handle; empty mints one from the name/email; refused if taken"
    ] = "",
    is_admin: Annotated[bool, "Create as a platform admin (implicitly owns every agent)"] = False,
    timezone: Annotated[str, "IANA timezone, e.g. Europe/Madrid"] = "UTC",
    title: Annotated[
        str, "The person's position, shown under their name (e.g. 'Head of Sales'); optional"
    ] = "",
) -> str:
    """Invite a new human to the dashboard — never takes or sets a password.

    Creates a pending account and returns a single-use, 7-day set-password
    link (absolute URL when the deployment's public address is known) plus a
    QR image of it saved into your workspace. Relay link and QR to the person;
    opening it lets them set their own password and signs them in. Enforces
    the deployment's user quota. The first admin of a fresh deployment is
    created by onboarding, not by this tool.
    """
    try:
        actor = _acting_agent()
        return _ok(
            admin.create_user(
                email,
                name=name,
                username=username,
                is_admin=is_admin,
                timezone=timezone,
                created_by=actor,
                qr_agent=actor,
                title=title,
            )
        )
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True, owner_only=True)
@tool
def olano_reset_user_password(user: Annotated[str, "Email, @username, or user id"]) -> str:
    """Issue a fresh single-use set-password link (+ QR image) for a human —
    the admin-driven password reset; also re-invites a still-pending account.

    The person opens the link, sets a new password, and is signed in; any
    earlier invite/reset link for them is voided. Relay the link/QR and stop —
    never ask for or repeat a password in chat.
    """
    try:
        actor = _acting_agent()
        return _ok(admin.reset_user_password(user, created_by=actor, qr_agent=actor))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True, owner_only=True)
@tool
def olano_set_user_role(
    user: Annotated[str, "Email, @username, or user id"],
    role: Annotated[str, "admin | member"],
) -> str:
    """Make a human a platform ``admin`` (full dashboard, implicitly owns every
    agent) or a ``member`` (sees only agents they were granted). Refuses to
    demote the last active admin."""
    try:
        return _ok(admin.set_user_role(user, role))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=True, owner_only=True)
@tool
def olano_update_user(
    user: Annotated[str, "Email, @username, or user id"],
    updates: Annotated[
        dict[str, Any],
        "Fields to change: name / title (their position) / email / is_active / "
        "can_terminal / ui_mode (advanced|simple) / langfuse_links (show Langfuse links in "
        "the dashboard) / timezone. NOT here: username (permanent), "
        "password (use olano_reset_user_password), is_admin (use olano_set_user_role)",
    ],
) -> str:
    """Update a human's editable profile fields.

    ``is_active: false`` disables the account — login stops and their live
    dashboard sessions stop resolving immediately; ``true`` re-enables it.
    Refuses to disable the last active admin.
    """
    try:
        return _ok(admin.update_user(user, updates))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True, owner_only=True)
@tool
def olano_delete_user(user: Annotated[str, "Email, @username, or user id"]) -> str:
    """Permanently delete a human account, including their sessions, invites,
    per-agent access grants and preferences. Prefer disabling
    (``olano_update_user`` with ``is_active: false``) when the person may
    return. Refuses to delete the last active admin."""
    try:
        return _ok(admin.delete_user(user))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=True, owner_only=True)
@tool
def olano_grant_agent_access(
    user: Annotated[str, "Email, @username, or user id"],
    agent: Annotated[str, "Agent name"],
    role: Annotated[
        str, "owner (manages that agent's access and settings) | member (may chat with it)"
    ] = "member",
) -> str:
    """Assign a human to an agent as ``owner`` or ``member``.

    Role and agent are validated loudly — an unknown role is refused, never
    silently downgraded. Platform admins already reach every agent without a
    grant; granting one anyway just records it explicitly.
    """
    try:
        return _ok(admin.grant_agent_access(user, agent, role, granted_by=_acting_agent()))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=True, owner_only=True)
@tool
def olano_revoke_agent_access(
    user: Annotated[str, "Email, @username, or user id"],
    agent: Annotated[str, "Agent name"],
) -> str:
    """Remove a human's access grant to one agent. (Platform admins keep
    implicit access to every agent regardless of grants.)"""
    try:
        return _ok(admin.revoke_agent_access(user, agent))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_access_overview() -> str:
    """The whole humans × agents access grid in one response: per-agent
    explicit owners/members (by user id), the flat user roster, and
    ``admin_ids`` — the platform admins who implicitly own every agent
    without needing a grant row."""
    try:
        return _ok(admin.access_overview())
    except Exception as exc:
        return _err(exc)


# ── Logs & diagnostics ─────────────────────────────────────────────────────--


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False, owner_only=True)
@tool
def olano_tail_log(
    lines: Annotated[int, "How many trailing lines"] = 100,
    which: Annotated[str, "main | runtime"] = "main",
) -> str:
    """Return the last N lines of a olano log file."""
    try:
        return _ok(admin.tail_log(lines, which=which))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False, owner_only=True)
@tool
def olano_recent_errors(
    lines: Annotated[int, "How many trailing error/warning lines"] = 50,
    which: Annotated[str, "main | runtime"] = "main",
) -> str:
    """Return recent error/warning lines from a olano log file."""
    try:
        return _ok(admin.recent_errors(lines, which=which))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False, owner_only=True)
@tool
def olano_agent_errors(
    agent: Annotated[str, "Agent name"],
    lines: Annotated[int, "How many trailing matching lines"] = 50,
) -> str:
    """Return recent log lines (main + runtime) that mention one agent.

    Errors/warnings are surfaced first — use this to pull one agent's failures
    out of the shared log when diagnosing a boot or runtime fault.
    """
    try:
        return _ok(admin.agent_errors(agent, lines=lines))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False, owner_only=True)
@tool
def olano_list_sessions(agent: Annotated[str, "Agent name"]) -> str:
    """List the JSONL session transcripts recorded for an agent."""
    try:
        return _ok(admin.list_sessions(agent))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False, owner_only=True)
@tool
def olano_read_session(
    agent: Annotated[str, "Agent name"],
    thread_id: Annotated[str, "Thread ID (session file stem)"],
    lines: Annotated[int, "Trailing records to return"] = 100,
) -> str:
    """Return the last N records of a JSONL session transcript."""
    try:
        return _ok(admin.read_session(agent, thread_id, lines=lines))
    except Exception as exc:
        return _err(exc)


# ── Runtime status & live diagnostics ──────────────────────────────────────--


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_runtime_status() -> str:
    """Is the runtime up, and which agents are online vs offline (stopped)?

    Returns the live runtime health plus ``agents_online`` / ``agents_offline``
    name lists. Works in-process (inside ``olano serve``) or by probing the
    local runtime API; falls back to a config-only snapshot when nothing is
    running (every agent reported offline).
    """
    try:
        return _ok(admin.runtime_status())
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_agent_status(
    agent: Annotated[str, "Agent name; empty for all agents"] = "",
) -> str:
    """Online/offline status. One agent's state, or split lists for every agent.

    ``online`` = booted in the live runtime. Offline covers ``configured``,
    ``paused``, ``stopped`` and ``error`` — each row carries the state, paused
    flag, model, channels and any boot error.
    """
    try:
        return _ok(admin.agent_status(agent or None))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_mcp_status(
    agent: Annotated[str, "Agent name; empty for all agents with MCP servers"] = "",
) -> str:
    """Live MCP server health for one agent, or every agent that has MCP servers.

    Per agent: ``overall`` (green/yellow/red/none/unknown) plus per-server
    ``ok``, ``status``, ``transport`` and live ``tool_count``. Reflects the
    agent's actually-bound MCP tools when running; a config snapshot otherwise.
    """
    try:
        return _ok(admin.mcp_status(agent or None))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_agent_activity(
    agent: Annotated[str, "Agent name"],
    thread_id: Annotated[str, "Thread ID; empty = most recently active thread"] = "",
) -> str:
    """Is a tool running, or is the agent hanging? Inspect its latest thread.

    Reports whether work is in flight, which tool (if any) has no result yet,
    how long it has been pending, and a ``likely_stuck`` flag when an online
    agent has had work pending too long. Includes a clipped transcript tail.
    """
    try:
        return _ok(admin.agent_activity(agent, thread_id or None))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False, owner_only=True)
@tool
def olano_diagnose_agent(agent: Annotated[str, "Agent name"]) -> str:
    """One-shot health report for an agent: combine status, activity/hang,
    MCP health, and recent error lines into a ``problems`` list + ``summary``.
    Start here when an agent is misbehaving."""
    try:
        return _ok(admin.diagnose_agent(agent))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_doctor() -> str:
    """Return a lightweight health summary of the olano home."""
    try:
        return _ok(admin.doctor())
    except Exception as exc:
        return _err(exc)


@tool
def olano_memory_report(
    include_gc: Annotated[bool, "Include a gc object-type census (cheap, O(objects))"] = True,
    include_tracemalloc: Annotated[
        bool, "Include tracemalloc top allocations when tracing is on"
    ] = True,
) -> str:
    """Process memory (RSS/VmSize/Pss/threads/uptime) plus a breakdown of
    olano's in-process structures — the tool-arg registry, per-runner hook
    contexts, self-learning offsets, checkpointer caches, loaded RAG collections,
    and a gc top-types census — to tell a real leak from the inherent baseline
    (compiled agent graphs + the embedding model + Chroma indices). Start here
    when the server's RAM climbs."""
    try:
        return _ok(
            admin.memory_report(include_gc=include_gc, include_tracemalloc=include_tracemalloc)
        )
    except Exception as exc:
        return _err(exc)


@tool
def olano_memory_monitor(
    action: Annotated[str, "on | off | status | tracemalloc-on | tracemalloc-off"] = "status",
) -> str:
    """Turn the background memory monitor (periodic sampler + tracemalloc) on or
    off at runtime, or show its state. ``off`` also stops tracemalloc so the
    monitor stops costing memory — use it if the monitor itself is heavy."""
    try:
        return _ok(admin.set_memory_monitor(action))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def olano_disk_report() -> str:
    """Disk usage + monitor state: percent used and level, the whole-disk
    breakdown (Docker, olano home, swap, system residual), the largest
    directories from the last scan, which cleanup actions are selected and
    available on this host, and the last cleanup's outcome. Read-only — the
    place to look before and after ``olano_disk_monitor``."""
    try:
        return _ok(admin.disk_report())
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=True)
@tool
def olano_disk_monitor(
    action: Annotated[str, "on | off | status | run | scan"] = "status",
) -> str:
    """Control the disk monitor: ``on``/``off`` toggle automatic cleanup (the
    monitor's alerting stays governed by config.yaml ``disk_monitor.enabled``),
    ``run`` starts a manual cleanup now in the background (frees Docker images,
    caches, old logs per the selected actions), ``scan`` starts a directory-size
    scan, ``status`` reports state. For a read-only look use
    ``olano_disk_report`` — this tool is approval-gated because ``run``
    deletes data."""
    try:
        return _ok(admin.set_disk_monitor(action))
    except Exception as exc:
        return _err(exc)


# ── Scheduler (fleet-wide) ────────────────────────────────────────────────────
#
# The ordinary ``scheduler_*`` toolkit is scoped to its caller and, more to the
# point, sees only the scheduler database. A deployment schedules work in two
# places — that database and each agent's ``agent.json5 → schedule:`` block
# (addressed ``cfg:<agent>:<index>``) — so a surface that reads one of them
# hides jobs. These six read and write both, for every agent.
#
# Writes are ``cross_agent_exec``-gated: scheduling work for another agent makes
# it take a full model turn on a prompt chosen here, repeatedly and unattended,
# which is strictly more reach than ``olano_run_agent_command``'s single
# supervised command.


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False, owner_only=True)
@tool
def olano_scheduler_list(
    agent: Annotated[str, "Limit to one agent; empty lists the whole fleet"] = "",
    source: Annotated[str, "'db', 'config', or empty for both stores"] = "",
) -> str:
    """Every scheduled job in the deployment, across both stores.

    Prompts come back as a 200-character preview plus the true character count,
    so a large fleet does not flood the turn. Use ``olano_scheduler_get`` for a
    job's full instructions.
    """
    try:
        return _ok(admin.scheduler.list_tasks(agent=agent.strip(), source=source.strip()))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="medium", requires_hitl=False, owner_only=True)
@tool
def olano_scheduler_get(
    task_id: Annotated[
        str, "Database task id (full or 8-char prefix) or a 'cfg:<agent>:<index>' config job id"
    ],
    history: Annotated[int, "Recent run records to include (0-50)"] = 5,
) -> str:
    """One scheduled job in full — its COMPLETE prompt, plus run history.

    Read this before changing a job: an update replaces the prompt outright, and
    no listing shows you the text you would be replacing.
    """
    try:
        return _ok(admin.scheduler.get_task(task_id, history=history))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True, owner_only=True)
@tool
def olano_scheduler_create(
    agent: Annotated[str, "Agent that will run the job"],
    name: Annotated[str, "Short label for the job"],
    prompt: Annotated[str, "The message that agent receives when the job fires"],
    schedule: Annotated[
        str, "Standard 5-field cron, e.g. '0 9 * * 1' (Mondays). Day-of-week 0 or 7 = Sunday"
    ] = "",
    run_at: Annotated[str, "ISO datetime for a one-time run (database store only)"] = "",
    timezone: Annotated[str, "IANA timezone name, e.g. 'America/Costa_Rica'"] = "UTC",
    store: Annotated[
        str,
        "'db' (scheduler database; supports one-time runs and later reassignment) "
        "or 'config' (a durable entry in the agent's agent.json5)",
    ] = "db",
    model: Annotated[
        str,
        "Model the job runs on, as provider:model (e.g. 'olano:turbo'). Empty = that "
        "agent's configured model. A cheaper tier for routine digests, a bigger one "
        "for deep work; refused if the provider is not connected here",
    ] = "",
    reasoning_effort: Annotated[
        str,
        "Reasoning effort for the job's runs, one of the levels the model it runs on "
        "accepts (`model` when set, else that agent's own). Empty = the agent's own "
        "setting; refused with the accepted levels otherwise",
    ] = "",
) -> str:
    """Schedule a recurring or one-time job for any agent in the fleet.

    Give either ``schedule`` (cron) or ``run_at`` (one-time). A config-store job
    is cron-only. ``model`` pins the job to one model without changing the
    agent's own; the run bills whichever model ran. ``reasoning_effort`` sets
    how hard that model thinks on this job alone.
    """
    try:
        return _ok(
            admin.scheduler.create_task(
                agent,
                name,
                prompt,
                schedule=schedule,
                run_at=run_at,
                timezone=timezone,
                store=store,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        )
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True, owner_only=True)
@tool
def olano_scheduler_update(
    task_id: Annotated[str, "Database task id (full or 8-char prefix) or a 'cfg:' config job id"],
    name: Annotated[str | None, "New label; omit to keep the current value"] = None,
    prompt: Annotated[
        str | None,
        "New prompt, REPLACING the existing one in full; omit to keep. "
        "Read it with olano_scheduler_get first",
    ] = None,
    schedule: Annotated[str | None, "New 5-field cron; omit to keep"] = None,
    run_at: Annotated[
        str | None,
        "New one-time datetime; '' clears it. Database jobs only; omit to keep",
    ] = None,
    timezone: Annotated[str | None, "New IANA timezone; omit to keep"] = None,
    enabled: Annotated[bool | None, "False pauses the job, True resumes it; omit to keep"] = None,
    visibility: Annotated[
        str | None, "'private' or 'shared'. Database jobs only; omit to keep"
    ] = None,
    agent: Annotated[
        str | None,
        "Reassign which agent runs it; '' falls back to the default agent. "
        "Database jobs only; omit to keep",
    ] = None,
    model: Annotated[
        str | None,
        "New model as provider:model; '' returns the job to the agent's configured "
        "model. Works for both stores; omit to keep",
    ] = None,
    reasoning_effort: Annotated[
        str | None,
        "New reasoning effort, one of the levels the job's model accepts; '' returns "
        "the job to the agent's own setting. Works for both stores; omit to keep",
    ] = None,
) -> str:
    """Change a scheduled job belonging to any agent.

    Omitting an argument keeps its current value. An empty string clears
    ``run_at``, ``agent``, ``model`` or ``reasoning_effort``; a blank ``name``
    or ``prompt`` is refused rather than written, since no job wants an empty
    one.
    """
    try:
        return _ok(
            admin.scheduler.update_task(
                task_id,
                name=name,
                prompt=prompt,
                schedule=schedule,
                run_at=run_at,
                timezone=timezone,
                enabled=enabled,
                visibility=visibility,
                agent=agent,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        )
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True, owner_only=True)
@tool
def olano_scheduler_delete(
    task_id: Annotated[str, "Database task id (full or 8-char prefix) or a 'cfg:' config job id"],
) -> str:
    """Permanently delete a scheduled job from whichever store holds it.

    Deleting a config job shifts the ``cfg:`` ids of that agent's later entries
    down by one — re-list before reusing an id you were holding.
    """
    try:
        return _ok(admin.scheduler.delete_task(task_id))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True, owner_only=True)
@tool
def olano_scheduler_run_now(
    task_id: Annotated[str, "Database task id (full or 8-char prefix) or a 'cfg:' config job id"],
) -> str:
    """Fire a scheduled job immediately, ignoring its schedule.

    The owning agent takes a full turn now. Needs the runtime scheduler live
    and, for a config job, the owning agent booted.
    """
    try:
        return _ok(admin.scheduler.run_task_now(task_id))
    except Exception as exc:
        return _err(exc)


# ── Cortex review: any agent's ideas / brainstorms / prototypes / projects ────
#
# Reads are owner_only like the scheduler's: an item's detail is another
# agent's working notes. The writes sit in HITL's write_external: discard and
# restore flip a status the dashboard flips with one click (and are undone the
# same way), and a promotion runs the target's own fixed promotion playbook
# under its own config and budget, which is what its Cortex card's
# Promote button does. None of them hands the target a prompt chosen here,
# which is what puts the scheduler writes in cross_agent_exec.


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_cortex_list(
    agent: Annotated[
        str, "Agent whose Cortex items to list; empty = every agent in the fleet"
    ] = "",
    kind: Annotated[
        str, "Filter by kind: idea | brainstorm | prototype | project. Empty = all"
    ] = "",
    status: Annotated[
        str,
        "Which items to show by status. 'active' (the default) = items still in play, "
        "including ones being explored right now. Or 'exploring', 'promoted', "
        "'discarded', several joined by commas (e.g. 'promoted,discarded'), or 'all'",
    ] = "active",
    min_score: Annotated[
        float | None, "Only rated items scoring at least this (0-10); unrated items drop out"
    ] = None,
    max_score: Annotated[
        float | None, "Only rated items scoring at most this (0-10); unrated items drop out"
    ] = None,
    limit: Annotated[int, "Maximum rows to return (1-500)"] = 50,
) -> str:
    """List another agent's Cortex items (or the whole fleet's) for review.

    Each row has the item id the other ``olano_cortex_*`` tools take, its kind,
    status, title, a one-line summary and, when Cortex has rated it, its score
    and recommendation (promote / keep / discard). By default only the items
    still in play are listed; ask for ``status="discarded"`` or ``"all"`` to
    see the rest. ``counts`` are the active items per kind and
    ``counts_by_status`` how many items sit in each status, so you can tell
    the user what the default view leaves out. Use it to answer "what has Scout
    been brainstorming?" or "which ideas across the fleet score below 4?", then
    view, discard or promote what you find.
    """
    try:
        return _ok(
            admin.cortex.list_items(
                agent,
                kind=kind,
                status=status,
                min_score=min_score,
                max_score=max_score,
                limit=limit,
            )
        )
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False, owner_only=True)
@tool
def olano_cortex_view(
    agent: Annotated[str, "Agent that owns the item"],
    item_id: Annotated[str, "Cortex item id (from olano_cortex_list)"],
) -> str:
    """Read one of another agent's Cortex items in full before judging it: its
    metadata, rating rationale and next step, links to the stage it came from or
    went to, its detail markdown, the files in its folder and, for a project,
    PLAN.md progress. Says so when a promote/explore run is working on it now."""
    try:
        return _ok(admin.cortex.get_item(agent, item_id))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=True, owner_only=True)
@tool
def olano_cortex_discard(
    agent: Annotated[str, "Agent that owns the items"],
    item_ids: Annotated[
        list[str] | None, "Ids of the items to discard. Leave empty to select by kind instead"
    ] = None,
    kind: Annotated[
        str,
        "Discard every active item of this kind: idea | brainstorm | prototype | project, "
        "or 'all' for every kind. With item_ids, only those ids of this kind are discarded",
    ] = "",
    max_score: Annotated[
        float | None,
        "Only discard items rated at or below this score (0-10); unrated items are kept",
    ] = None,
    reason: Annotated[str, "Short reason, recorded in the agent's Cortex activity log"] = "",
) -> str:
    """Discard Cortex items of another agent: the ones you name, or every active
    item of a kind ("discard all of Scout's brainstorms"), optionally only those
    Cortex rated at or below ``max_score``.

    Name items or a kind; an empty selection is refused, never read as
    "everything". Already discarded or promoted items, and items a promote or
    explore run is working on right now, are skipped with the reason. Folders
    stay on disk and ``olano_cortex_restore`` undoes it. Confirm a bulk
    selection with the user (list it first) before discarding.
    """
    try:
        return _ok(
            admin.cortex.discard_items(
                agent,
                item_ids=item_ids,
                kind=kind,
                max_score=max_score,
                reason=reason,
                actor=_acting_agent(),
            )
        )
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="low", requires_hitl=True, owner_only=True)
@tool
def olano_cortex_restore(
    agent: Annotated[str, "Agent that owns the items"],
    item_ids: Annotated[list[str], "Ids of the discarded items to bring back"],
) -> str:
    """Bring discarded Cortex items of another agent back to active (the undo
    for ``olano_cortex_discard``). Items that are not discarded are reported
    and left as they are."""
    try:
        return _ok(admin.cortex.restore_items(agent, item_ids, actor=_acting_agent()))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="mutating", risk="medium", requires_hitl=True, owner_only=True)
@tool
async def olano_cortex_promote(
    agent: Annotated[str, "Agent that owns the item"],
    item_id: Annotated[str, "Cortex item id to promote (from olano_cortex_list)"],
    target: Annotated[
        str,
        "Stage to promote into: idea | prototype | project. Empty = the next stage "
        "(brainstorm → idea → prototype → project); 'project' works from any stage",
    ] = "",
    wait: Annotated[
        bool,
        "Wait for the promotion to finish (minutes for a prototype or project). "
        "False = start it and answer at once; the item's Cortex card shows progress",
    ] = True,
) -> str:
    """Promote one of another agent's Cortex items down the funnel, exactly as
    its Cortex card's Promote / Create project button does: brainstorm → idea
    (its strongest angle distilled), idea → prototype (a small runnable build),
    any stage → project (``projects/<slug>/`` with README/PLAN/STATUS/JOURNAL).

    The work is done by that agent's own deep worker, with its models, budget,
    workspace and sandbox, and the source item is marked promoted and linked to
    the new one. A promotion of the same item already running is joined, never
    started twice. For several items, promote them one call each.
    """
    try:
        return _ok(
            await admin.cortex.promote_item(
                agent, item_id, target=target, wait=wait, actor=_acting_agent()
            )
        )
    except Exception as exc:
        return _err(exc)


@tool
def olano_update(
    check: Annotated[bool, "Only check for an update; do not apply"] = True,
) -> str:
    """Update olano via the `olano update` CLI (shell-gated).

    With ``check=True`` this only reports whether an update is available; set
    ``check=False`` to actually upgrade. Runs as a subprocess so it is gated by
    ``hitl_shell`` like any other shell action.
    """
    import subprocess

    # On a managed deployment, updates are driven by the control plane (the
    # cloud "Update" flow), and this runs an installer/network process on the
    # host (which holds /app + secrets) outside the shell chokepoint. Refuse the
    # in-agent self-update there; allow the check-only report.
    if not check:
        try:
            from olano.deployment import enforcement as dep_enforcement

            if dep_enforcement.require_sandboxed_exec():
                return json.dumps(
                    {
                        "error": "In-agent self-update is disabled on this managed "
                        "deployment. Updates are applied from the control plane "
                        "(cloud dashboard)."
                    },
                    ensure_ascii=False,
                )
        except Exception:
            pass

    cmd = ["olano", "update"] + (["--check"] if check else [])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return _ok(
            {
                "command": " ".join(cmd),
                "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-2000:],
            }
        )
    except Exception as exc:
        return _err(exc)


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
