"""Catalog of internal/system tools every agent can get without listing them
in ``agent.json5 → tools``.

Before this module, "what tools does this agent have" was answered by four
independent, scattered sources: ``UNIVERSAL_AGENT_TOOL_NAMES`` (ops_defaults),
the builder's own if/else chain for memory/graph/notes/workspace_context
tools, deepagents' own built-ins, and — until now — an unconditional grant of
``secret_get``/``secret_list``/``secret_set``. No single view (dashboard,
``whoami``, the archetype admin tool) agreed on the full list, and an operator
had no way to see, let alone toggle, most of it.

``INTERNAL_TOOL_CATALOG`` is the one declarative description of that surface,
each entry carrying the condition under which it is actually bound. It does
**not** replace the imperative build logic in ``core/builder.py`` (which still
does the real work of constructing dynamic, per-workspace tool closures) —
it exists so every *reporting* surface (dashboard "Olano Internal Tools"
section, effective-tools listings) can read one source of truth instead of
re-deriving the list. Keep a category's ``condition`` here in sync with the
corresponding gate in ``builder.py`` when either changes.

``secret_get``/``secret_list``/``secret_set`` are deliberately **not** in this
catalog — they used to be unconditionally injected into every agent
regardless of ``tools:``, which is exactly the opposite of least privilege for
a credentials vault. They are now ordinary opt-in tools: list them by name in
``agent.json5 → tools`` (as the ``agentfather`` archetype already does for
``secret_list``/``secret_set``) like any other integration tool.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from olano.config.autonomy_defaults import (
    MAGIC_LINK_TOOL_DESCRIPTIONS,
    MAGIC_LINK_TOOL_NAMES,
    MAGIC_LINK_TOOL_REQUIRES,
    SELF_CONFIG_TOOL_DESCRIPTIONS,
    SELF_CONFIG_TOOL_NAMES,
    SELF_CONFIG_TOOL_REQUIRES,
)
from olano.config.multimodal_defaults import (
    MULTIMODAL_TOOL_DESCRIPTIONS,
    MULTIMODAL_TOOL_NAMES,
    MULTIMODAL_TOOL_REQUIRES,
    capability_available,
)
from olano.config.ops_defaults import (
    BUILTIN_DOCUMENT_TOOL_NAMES,
    CORTEX_TOOL_NAMES,
    OPS_TOOL_NAMES,
)

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

# ── Categories ─────────────────────────────────────────────────────────────
CATEGORY_SELF_TIME = "Self & Time"
CATEGORY_SELF_CONFIG = "Self-configuration"
CATEGORY_MAGIC_LINKS = "Magic links"
CATEGORY_MESH = "Agent Mesh"
CATEGORY_WHATSAPP = "WhatsApp Business"
CATEGORY_INTERACTION = "User Interaction"
CATEGORY_WEB_DOCS = "Web & Documents"
CATEGORY_MULTIMODAL = "Multimodal"
CATEGORY_HOUSEKEEPING = "Housekeeping"
CATEGORY_OPERATIONS = "Operations"
CATEGORY_CORTEX = "Cortex"
CATEGORY_MEMORY = "Memory"
CATEGORY_CUSTOMER_MEMORY = "Customer Memory"
CATEGORY_MULTI_USER = "Multi-user Actions & Scheduling"
CATEGORY_RAG = "Knowledge (RAG)"
CATEGORY_GRAPH = "Knowledge Graph"
CATEGORY_NOTES = "Notes"
CATEGORY_WORKSPACE = "Workspace"
CATEGORY_AGENT_LOOP = "Agent Loop"
CATEGORY_FILESYSTEM = "Filesystem & Shell"


def _always_on(_config: AgentConfig) -> bool:
    return True


def _default_grant(name: str) -> Callable[[AgentConfig], bool]:
    """Condition for a universal default: granted by ``default_internal_tools``,
    or reclaimable one-by-one via an explicit ``tools:`` entry when the knob is
    off (post-validation ``config.tools`` holds exactly the explicit names
    then, so the membership test is the real binding rule)."""

    def _on(config: AgentConfig, _name: str = name) -> bool:
        return bool(config.default_internal_tools) or _name in (config.tools or [])

    return _on


def _ops_default_grant(name: str) -> Callable[[AgentConfig], bool]:
    """Condition for an operations default: a universal default that
    ``AgentConfig._inject_builtin_ops`` withholds in Customer Service mode
    (``memory.per_customer``); an explicit ``tools:`` entry binds either way."""

    def _on(config: AgentConfig, _name: str = name) -> bool:
        if _name in (config.tools or []):
            return True
        return bool(config.default_internal_tools) and not config.memory.per_customer

    return _on


#: `requires` label for the A2A remote-agent tools.
_REMOTE_AGENTS_REQUIRES = (
    "at least one enabled entry under remote_agents (or an explicit tools: entry)"
)

A2A_TOOL_NAMES: tuple[str, ...] = (
    "a2a_list_agents",
    "a2a_discover_agent",
    "a2a_send_message",
    "a2a_get_task",
)


def has_remote_agents(config: AgentConfig) -> bool:
    """True when the agent lists at least one enabled A2A remote agent."""
    return any(getattr(e, "enabled", True) for e in (getattr(config, "remote_agents", None) or []))


def available_a2a_tool_names(config: AgentConfig) -> list[str]:
    """The a2a_* tools the builder binds implicitly for ``config``.

    Same rule as the multimodal defaults: the implicit grant rides
    ``default_internal_tools`` and the presence of a remote agent; an explicit
    ``tools:`` entry binds regardless (resolved by the registry directly), so
    it is not repeated here.
    """
    if not has_remote_agents(config) or not config.default_internal_tools:
        return []
    return list(A2A_TOOL_NAMES)


def _remote_agents_grant(name: str) -> Callable[[AgentConfig], bool]:
    def _on(config: AgentConfig, _name: str = name) -> bool:
        if _name in (config.tools or []):
            return True
        return has_remote_agents(config) and bool(config.default_internal_tools)

    return _on


#: `requires` label for the everyday WhatsApp Business tools.
_WHATSAPP_BUSINESS_REQUIRES = (
    "a WhatsApp (Twilio Business API) or WhatsApp (Meta Cloud API) connector on this agent "
    "(or an explicit tools: entry)"
)

#: The connectors the WhatsApp Business toolkit works with.
WHATSAPP_BUSINESS_CHANNELS: tuple[str, ...] = (
    "platform:whatsapp_twilio",
    "platform:whatsapp_meta_api",
)

#: The everyday side of the WhatsApp Business toolkit
#: (``tools/whatsapp_business_tools.py``). The admin side (create / submit /
#: status / delete, numbers, profile) is deliberately NOT here: it is never
#: implicit, an administrator grants it by name
#: (``admin.integrations.OPERATOR_GRANTED_TOOLKITS``).
WHATSAPP_BUSINESS_TOOL_NAMES: tuple[str, ...] = (
    "whatsapp_template_list",
    "whatsapp_template_send",
    "whatsapp_message_status",
    "whatsapp_window_status",
)


def has_whatsapp_business_channel(config: AgentConfig) -> bool:
    """True when the agent has a WhatsApp business connector (Twilio or Meta Cloud API)."""
    for entry in getattr(config, "channels", None) or []:
        kind: Any
        enabled: Any
        if isinstance(entry, str):
            kind, enabled = entry, True
        elif isinstance(entry, dict):
            kind, enabled = entry.get("type"), entry.get("enabled", True)
        else:
            kind, enabled = getattr(entry, "type", None), getattr(entry, "enabled", True)
        if str(kind or "") in WHATSAPP_BUSINESS_CHANNELS and enabled is not False:
            return True
    return False


def available_whatsapp_business_tool_names(config: AgentConfig) -> list[str]:
    """The everyday WhatsApp Business tools the builder binds implicitly for ``config``.

    Same rule as the A2A tools: the implicit grant rides
    ``default_internal_tools`` and the presence of the connector, and
    ``disabled_tools`` (the Tools tab) switches either tool off; an explicit
    ``tools:`` entry binds regardless and is resolved by the registry.
    """
    if not has_whatsapp_business_channel(config) or not config.default_internal_tools:
        return []
    return list(WHATSAPP_BUSINESS_TOOL_NAMES)


def _whatsapp_business_grant(name: str) -> Callable[[AgentConfig], bool]:
    def _on(config: AgentConfig, _name: str = name) -> bool:
        if _name in (config.tools or []):
            return True
        return has_whatsapp_business_channel(config) and bool(config.default_internal_tools)

    return _on


def _self_config_grant(name: str) -> Callable[[AgentConfig], bool]:
    """Condition for a chat self-configuration default (``self_*`` tools).

    Mirrors ``core/builder._build_extra_tools``: implicit grant needs
    ``default_internal_tools`` **and** the tool's autonomy capability
    resolving ON for this agent; an explicit ``tools:`` entry binds
    unconditionally (the call-time capability check still governs whether the
    tool acts).
    """

    def _on(config: AgentConfig, _name: str = name) -> bool:
        if _name in (config.tools or []):
            return True
        if not config.default_internal_tools:
            return False
        from olano.config.autonomy_defaults import capability_on

        return capability_on(_name, config)

    return _on


def _multimodal_grant(name: str) -> Callable[[AgentConfig], bool]:
    """Condition for a multimodal default (see ``config/multimodal_defaults``).

    Mirrors ``core/builder._build_extra_tools``: the implicit grant needs
    ``default_internal_tools`` **and** a provider credential that actually
    resolves for this agent, while an explicit ``tools:`` entry binds
    unconditionally (the operator named it; a missing key then shows up as the
    tool's own ``[missing-credentials]`` reply rather than as a vanished tool).
    """

    def _on(config: AgentConfig, _name: str = name) -> bool:
        if _name in (config.tools or []):
            return True
        return bool(config.default_internal_tools) and capability_available(_name, config)

    return _on


def _cortex_on(config: AgentConfig) -> bool:
    from olano.config.system_settings import cortex_enabled

    return bool(config.default_internal_tools) and cortex_enabled(config)


def _markdown_memory_on(config: AgentConfig) -> bool:
    # A non-empty top_level_memory_tools pin is an explicit grant, so it
    # survives default_internal_tools: false (the allowlist then narrows the
    # set to exactly the pinned names).
    return (
        bool(config.memory.markdown)
        and not config.memory.per_customer
        and bool(config.default_internal_tools or config.top_level_memory_tools)
    )


def _customer_memory_on(config: AgentConfig) -> bool:
    return bool(config.memory.per_customer)


def _customer_actions_on(config: AgentConfig) -> bool:
    return bool(config.memory.per_customer and (config.allowed_actions or {}))


def _subscription_status_on(config: AgentConfig) -> bool:
    return bool(config.memory.per_customer and getattr(config.monetization, "enabled", False))


def _user_scheduling_on(config: AgentConfig) -> bool:
    return bool(
        config.memory.per_customer and getattr(config.customer_service, "user_scheduling", False)
    )


def _connections_on(config: AgentConfig) -> bool:
    return bool(
        config.memory.per_customer and getattr(config.customer_service, "connections", False)
    )


def _user_credentials_on(config: AgentConfig) -> bool:
    return bool(
        config.memory.per_customer and getattr(config.customer_service, "user_credentials", False)
    )


def _rag_on(config: AgentConfig) -> bool:
    return bool(config.memory.rag)


def _memory_enabled(config: AgentConfig) -> bool:
    return bool(config.memory.markdown or config.memory.rag)


def _memory_defaults_on(config: AgentConfig) -> bool:
    return _memory_enabled(config) and bool(config.default_internal_tools)


def _graph_defaults_on(config: AgentConfig) -> bool:
    # Same explicit-pin rule as _markdown_memory_on: a non-empty
    # top_level_graph_tools list survives default_internal_tools: false.
    return _memory_enabled(config) and bool(
        config.default_internal_tools or config.top_level_graph_tools
    )


def _workspace_context_on(config: AgentConfig) -> bool:
    # top_level_workspace_context defaults to True, so under the knob only a
    # value the config file actually set counts as an explicit grant.
    if not (_memory_enabled(config) and bool(config.top_level_workspace_context)):
        return False
    return (
        bool(config.default_internal_tools)
        or "top_level_workspace_context" in config.model_fields_set
    )


def _shell_on(config: AgentConfig) -> bool:
    from olano.core.prompt_assembler import _SHELL_BACKEND_TYPES, _SHELL_TOOL_NAMES

    tools = {t for t in (config.tools or []) if t}
    if tools & _SHELL_TOOL_NAMES:
        return True
    backend = config.backend
    backend_type = getattr(backend, "type", None) or (backend if isinstance(backend, str) else "")
    return backend_type in _SHELL_BACKEND_TYPES


@dataclass(frozen=True)
class InternalTool:
    name: str
    category: str
    condition: Callable[[AgentConfig], bool] = _always_on
    note: str = ""
    # Name of a `top_level_*_tools` AgentConfig field that additionally gates
    # this tool (core/builder._select_tool_subset): when that field is a
    # non-null list not containing this tool's name, the tool is off
    # regardless of disabled_tools — an allow-list, not an opt-out. Without
    # this, the dashboard could show a tool as "on" that the real builder
    # filters out (exactly the kind of mismatch this catalog exists to fix).
    allowlist_field: str | None = None
    # One-line "what it does" for the dashboard tool browser. Catalog-registry
    # tools get theirs from the tool docstring; these builder/deepagents-wired
    # tools have no registry entry, so the browser card text lives here.
    description: str = ""
    # Human label for `condition` — what has to be on for this tool to exist
    # (e.g. "Customer Service mode"). Every catalog entry is reported for
    # every agent; when the condition doesn't hold, the row shows off+locked
    # with a "requires <this>" note so the operator learns HOW to enable it
    # instead of the tool being invisible. Empty for always-on entries.
    requires: str = ""


# Descriptions for the document readers injected from BUILTIN_DOCUMENT_TOOL_NAMES —
# keep keys in sync with ops_defaults (a new name there shows up here blank, and
# the test suite flags catalog entries with no description).
_DOCUMENT_TOOL_DESCRIPTIONS: dict[str, str] = {
    "document_to_text": "Convert local documents (.pdf/.docx/.pptx/.xlsx/…) to plain text.",
    "pdf_to_text": "Extract the text of a local or downloaded PDF file.",
}

_CORTEX_TOOL_DESCRIPTIONS: dict[str, str] = {
    "cortex_list": "List Cortex items (ideas, brainstorms, prototypes, projects).",
    "cortex_view": "View one Cortex item in full detail.",
    "cortex_add": "Capture a new idea/brainstorm/prototype/project in Cortex.",
    "cortex_edit": "Edit an existing Cortex item.",
    "cortex_discard": "Discard a Cortex item (recoverable).",
    "cortex_restore": "Restore a previously discarded Cortex item.",
    "cortex_promote": (
        "Promote a Cortex item to the next funnel stage "
        "(brainstorm → idea → prototype → project), or straight to a project."
    ),
    "cortex_explore": "Expand a Cortex item with structured exploration.",
    "cortex_schedule": (
        "Schedule a follow-up step for the agent itself on a prototype or project "
        "(bounded: a few pending steps per agent, two per item, at most hourly)."
    ),
}

# Descriptions for the operations tools (OPS_TOOL_NAMES): universal defaults
# since the OpsManager subagent stopped being a default. Keep keys in sync with
# ops_defaults; the test suite flags catalog entries with no description.
_OPS_TOOL_DESCRIPTIONS: dict[str, str] = {
    "scheduler_create": "Schedule a recurring (cron) or one-time task for this agent.",
    "scheduler_list": "List scheduled tasks: this agent's own plus any shared with it.",
    "scheduler_get": "Show one scheduled task in full.",
    "scheduler_update": "Change a scheduled task's name, prompt, schedule, model or enabled state.",
    "scheduler_delete": "Delete a scheduled task this agent created.",
    "scheduler_history": "Show recent runs of a scheduled task and their outcomes.",
    "scheduler_search": "Search scheduled tasks by meaning (semantic search).",
    "scheduler_run_now": "Run a scheduled task immediately, outside its schedule.",
    "heartbeat_list": "List the checks in this agent's HEARTBEAT.md.",
    "heartbeat_add": "Add a periodic check to HEARTBEAT.md.",
    "heartbeat_update": "Edit a heartbeat check.",
    "heartbeat_remove": "Remove a heartbeat check.",
    "heartbeat_install_from_skill": "Install the heartbeat checks a skill ships with.",
    "heartbeat_status": "Show whether heartbeat polling is on and how often it runs.",
    "heartbeat_configure": "Change this agent's heartbeat interval or turn it on or off.",
    "pulse_list": "List curated PULSE insight items.",
    "pulse_generate": "Generate curated PULSE items now from recent work, memory and the workspace.",
    "pulse_accept": "Accept a curated PULSE item.",
    "pulse_defer": "Defer a PULSE item for a number of hours.",
    "pulse_reject": "Reject a PULSE item.",
    "pulse_undo": "Undo an autonomous artifact by restoring its backup.",
    "pulse_clear_expired": "Clear expired curated PULSE items.",
    "approvals_list": "List items waiting for approval.",
    "approvals_approve": "Approve an approval item and run its action.",
    "approvals_reject": "Reject an approval item.",
    "approvals_defer": "Defer an approval item for a number of hours.",
    "approvals_clear_expired": "Clear expired approval items.",
    "ops_list": "List runtime/system notes in the ops queue.",
    "ops_reject": "Dismiss an ops queue item.",
    "ops_defer": "Defer an ops queue item for a number of hours.",
    "ops_clear_expired": "Clear expired ops queue items.",
}

_MEMORY_TOOL_DESCRIPTIONS: dict[str, str] = {
    "memory_save": "Save a durable fact or decision to MEMORY.md.",
    "memory_search": "Search MEMORY.md and the daily logs.",
    "memory_read": "Read the full MEMORY.md long-term memory file.",
    "memory_append_daily": "Append an entry to today's daily log (memories/YYYY-MM-DD.md).",
    "memory_read_today": "Read today's daily log.",
    "memory_search_recent": "Search only the most recent daily logs.",
    "memory_compact": "Compact/distill MEMORY.md when it grows too large.",
    "user_profile_save": "Save a durable fact about the user to USER.md.",
    "identity_preference_save": "Save an identity/behavior preference to IDENTITY.md.",
    "soul_preference_save": "Save a personality/tone preference to SOUL.md.",
    "cortex_guidance_save": "Save standing guidance for the Cortex engine.",
}

_CUSTOMER_MEMORY_TOOL_DESCRIPTIONS: dict[str, str] = {
    "remember": "Store a fact about the current customer (isolated per customer).",
    "recall": "Recall stored facts about the current customer.",
    "recall_all": "List everything stored about the current customer.",
    "forget_me": "Erase everything stored about the current customer.",
    "knowledge_search": "Search the shared knowledge base articles.",
    "knowledge_list": "List the shared knowledge base articles.",
    "knowledge_read": "Read one knowledge base article.",
}

_GRAPH_TOOL_DESCRIPTIONS: dict[str, str] = {
    "graph_search_entities": "Search entities in the knowledge graph.",
    "graph_get_neighbors": "Get an entity's relationships and neighbors.",
    "graph_add_entity": "Add an entity to the knowledge graph.",
    "graph_add_relationship": "Link two entities in the knowledge graph.",
    "graph_summary": "Summarize the knowledge graph's contents.",
}

_NOTES_TOOL_DESCRIPTIONS: dict[str, str] = {
    "notes_add": "Create or update a note in the markdown wiki.",
    "notes_get": "Read a note from the markdown wiki.",
    "notes_search": "Search notes by content.",
    "notes_list": "List all notes in the wiki.",
    "notes_backlinks": "List the notes that link to a given note.",
}

#: `requires` label shared by every universal-default entry: how to get the
#: tool back on an agent that turned the implicit grants off.
_DEFAULTS_REQUIRES = (
    "default internal tools (default_internal_tools: true), or an explicit tools: entry"
)

#: `requires` label for the operations defaults: the universal rule plus the
#: Customer Service exclusion.
_OPS_REQUIRES = "default internal tools with Customer Service mode off, or an explicit tools: entry"

# ── The catalog ────────────────────────────────────────────────────────────
# Order matches display order in the dashboard's "Olano Internal Tools" section.
INTERNAL_TOOL_CATALOG: list[InternalTool] = [
    # Self-awareness & time
    InternalTool(
        "whoami",
        CATEGORY_SELF_TIME,
        _default_grant("whoami"),
        requires=_DEFAULTS_REQUIRES,
        description="Report this agent's own name, model, channels, tools and skills.",
    ),
    InternalTool(
        "get_current_time",
        CATEGORY_SELF_TIME,
        _default_grant("get_current_time"),
        requires=_DEFAULTS_REQUIRES,
        description="Current date/time in UTC and the local timezone.",
    ),
    InternalTool(
        "get_current_date",
        CATEGORY_SELF_TIME,
        _default_grant("get_current_date"),
        requires=_DEFAULTS_REQUIRES,
        description="Alias of get_current_time.",
    ),
    InternalTool(
        "wait_n_seconds",
        CATEGORY_SELF_TIME,
        _default_grant("wait_n_seconds"),
        requires=_DEFAULTS_REQUIRES,
        description="Non-blocking pause (max 300s) for polling patterns.",
    ),
    # Agent mesh
    InternalTool(
        "talk_to_agent",
        CATEGORY_MESH,
        _default_grant("talk_to_agent"),
        requires=_DEFAULTS_REQUIRES,
        description="Send a message to another agent in this runtime.",
    ),
    # External agents over the A2A protocol: bound when the agent lists at
    # least one enabled remote_agents entry (core/builder mirrors this).
    InternalTool(
        "a2a_list_agents",
        CATEGORY_MESH,
        _remote_agents_grant("a2a_list_agents"),
        requires=_REMOTE_AGENTS_REQUIRES,
        description="List the external (A2A) agents this agent may talk to.",
    ),
    InternalTool(
        "a2a_discover_agent",
        CATEGORY_MESH,
        _remote_agents_grant("a2a_discover_agent"),
        requires=_REMOTE_AGENTS_REQUIRES,
        description="Fetch a remote agent's A2A Agent Card (skills, endpoints, auth).",
    ),
    InternalTool(
        "a2a_send_message",
        CATEGORY_MESH,
        _remote_agents_grant("a2a_send_message"),
        requires=_REMOTE_AGENTS_REQUIRES,
        description="Send a message to a configured external agent over A2A.",
    ),
    InternalTool(
        "a2a_get_task",
        CATEGORY_MESH,
        _remote_agents_grant("a2a_get_task"),
        requires=_REMOTE_AGENTS_REQUIRES,
        description="Poll a long-running remote A2A task by id.",
    ),
    # WhatsApp Business, everyday side: bound when the agent has a WhatsApp
    # business connector, Twilio or Meta Cloud API (core/builder mirrors this).
    # The admin side is operator-granted and never listed here.
    InternalTool(
        "whatsapp_template_list",
        CATEGORY_WHATSAPP,
        _whatsapp_business_grant("whatsapp_template_list"),
        requires=_WHATSAPP_BUSINESS_REQUIRES,
        description=(
            "List the WhatsApp templates WhatsApp approved for this agent's WhatsApp Business "
            "account. Owner only."
        ),
    ),
    InternalTool(
        "whatsapp_template_send",
        CATEGORY_WHATSAPP,
        _whatsapp_business_grant("whatsapp_template_send"),
        requires=_WHATSAPP_BUSINESS_REQUIRES,
        description=(
            "Send an approved WhatsApp template, the only message WhatsApp delivers outside "
            "the 24-hour window. Owner only; customers cannot trigger it."
        ),
    ),
    InternalTool(
        "whatsapp_message_status",
        CATEGORY_WHATSAPP,
        _whatsapp_business_grant("whatsapp_message_status"),
        requires=_WHATSAPP_BUSINESS_REQUIRES,
        description=(
            "Check whether a WhatsApp message sent earlier was delivered or read, or why it "
            "failed. Owner only; read-only."
        ),
    ),
    InternalTool(
        "whatsapp_window_status",
        CATEGORY_WHATSAPP,
        _whatsapp_business_grant("whatsapp_window_status"),
        requires=_WHATSAPP_BUSINESS_REQUIRES,
        description=(
            "Check whether WhatsApp's 24-hour window is open for a number, i.e. whether an "
            "ordinary message or only a template would be delivered. Owner only; read-only."
        ),
    ),
    # User interaction
    InternalTool(
        "notification_send_text",
        CATEGORY_INTERACTION,
        _default_grant("notification_send_text"),
        requires=_DEFAULTS_REQUIRES,
        description="Push a text notification to the user's channel.",
    ),
    InternalTool(
        "notification_send_photo",
        CATEGORY_INTERACTION,
        _default_grant("notification_send_photo"),
        requires=_DEFAULTS_REQUIRES,
        description="Push an image attachment to the user's channel.",
    ),
    InternalTool(
        "notification_send_document",
        CATEGORY_INTERACTION,
        _default_grant("notification_send_document"),
        requires=_DEFAULTS_REQUIRES,
        description="Push a file attachment to the user's channel.",
    ),
    # Web & local documents
    InternalTool(
        "web_search",
        CATEGORY_WEB_DOCS,
        _default_grant("web_search"),
        requires=_DEFAULTS_REQUIRES,
        description="Search the web for current information.",
    ),
    InternalTool(
        "web_fetch",
        CATEGORY_WEB_DOCS,
        _default_grant("web_fetch"),
        requires=_DEFAULTS_REQUIRES,
        description="Fetch and read a web page.",
    ),
    *(
        InternalTool(
            name,
            CATEGORY_WEB_DOCS,
            _default_grant(name),
            requires=_DEFAULTS_REQUIRES,
            description=_DOCUMENT_TOOL_DESCRIPTIONS.get(name, ""),
        )
        for name in BUILTIN_DOCUMENT_TOOL_NAMES
    ),
    # Deterministic rendering (HTML/CSS → PNG / PDF / SVG on the bundled
    # Chromium). Universal: local, offline, no credential — and the reason an
    # agent never has to install a plotting library to make a chart.
    InternalTool(
        "render_html_image",
        CATEGORY_WEB_DOCS,
        _default_grant("render_html_image"),
        requires=_DEFAULTS_REQUIRES,
        description=(
            "Render an HTML/CSS document to a PNG at exact pixel size on the bundled "
            "Chromium — charts, slides, social graphics, quote cards, anything with text on it. "
            "`wait_for` a selector for pages that draw with JavaScript."
        ),
    ),
    InternalTool(
        "render_html_pdf",
        CATEGORY_WEB_DOCS,
        _default_grant("render_html_pdf"),
        requires=_DEFAULTS_REQUIRES,
        description=(
            "Render an HTML/CSS document to a PDF at exact page size (one page per "
            "`break-after: page` block) — reports, carousels, printable documents."
        ),
    ),
    InternalTool(
        "render_html_svg",
        CATEGORY_WEB_DOCS,
        _default_grant("render_html_svg"),
        requires=_DEFAULTS_REQUIRES,
        description=(
            "Save one <svg> element of a rendered page as a standalone vector file — the "
            "scalable twin of render_html_image for decks, design tools and print."
        ),
    ),
    # Multimodal (see / hear / speak / draw) — implicit ONLY where the
    # provider key resolves; rows whose key is missing report off + locked with
    # the key name in `requires`, which is how an operator finds out the
    # capability exists at all.
    *(
        InternalTool(
            name,
            CATEGORY_MULTIMODAL,
            _multimodal_grant(name),
            description=MULTIMODAL_TOOL_DESCRIPTIONS.get(name, ""),
            requires=MULTIMODAL_TOOL_REQUIRES.get(name, ""),
        )
        for name in MULTIMODAL_TOOL_NAMES
    ),
    # Chat self-configuration (self_* tools) — granted per resolved autonomy
    # capability; see config/autonomy_defaults.py.
    *(
        InternalTool(
            name,
            CATEGORY_SELF_CONFIG,
            _self_config_grant(name),
            description=SELF_CONFIG_TOOL_DESCRIPTIONS.get(name, ""),
            requires=SELF_CONFIG_TOOL_REQUIRES.get(name, ""),
        )
        for name in SELF_CONFIG_TOOL_NAMES
    ),
    # Magic links (magic_link_* tools) — one capability for the family, same
    # grant shape as the self_* tools; see config/autonomy_defaults.py.
    *(
        InternalTool(
            name,
            CATEGORY_MAGIC_LINKS,
            _self_config_grant(name),
            description=MAGIC_LINK_TOOL_DESCRIPTIONS.get(name, ""),
            requires=MAGIC_LINK_TOOL_REQUIRES.get(name, ""),
        )
        for name in MAGIC_LINK_TOOL_NAMES
    ),
    # Workspace housekeeping (recoverable trash)
    InternalTool(
        "trash_put",
        CATEGORY_HOUSEKEEPING,
        _default_grant("trash_put"),
        requires=_DEFAULTS_REQUIRES,
        description="Move a workspace file/folder to the recoverable trash.",
    ),
    InternalTool(
        "trash_list",
        CATEGORY_HOUSEKEEPING,
        _default_grant("trash_list"),
        requires=_DEFAULTS_REQUIRES,
        description="List what's in the workspace trash can.",
    ),
    InternalTool(
        "trash_restore",
        CATEGORY_HOUSEKEEPING,
        _default_grant("trash_restore"),
        requires=_DEFAULTS_REQUIRES,
        description="Restore a trashed item to its original location.",
    ),
    InternalTool(
        "trash_empty",
        CATEGORY_HOUSEKEEPING,
        _default_grant("trash_empty"),
        requires=_DEFAULTS_REQUIRES,
        description="Permanently empty the trash (requires confirm=true).",
    ),
    # Operations: scheduler, heartbeat, PULSE, approvals, ops queue. Universal
    # defaults (they used to ride the OpsManager subagent), withheld in
    # Customer Service mode.
    *(
        InternalTool(
            name,
            CATEGORY_OPERATIONS,
            _ops_default_grant(name),
            requires=_OPS_REQUIRES,
            description=_OPS_TOOL_DESCRIPTIONS.get(name, ""),
        )
        for name in OPS_TOOL_NAMES
    ),
    # Cortex item tools — only when the Cortex engine is on for this agent
    *(
        InternalTool(
            name,
            CATEGORY_CORTEX,
            _cortex_on,
            description=_CORTEX_TOOL_DESCRIPTIONS.get(name, ""),
            requires="the Cortex engine (cognition) on + default internal tools",
        )
        for name in CORTEX_TOOL_NAMES
    ),
    # Shared MEMORY.md tools — markdown mode, not per-customer. top_level_memory_tools
    # (core/builder._select_tool_subset) is an allow-list on top of disabled_tools:
    # when set, only the names it lists survive.
    *(
        InternalTool(
            name,
            CATEGORY_MEMORY,
            _markdown_memory_on,
            allowlist_field="top_level_memory_tools",
            description=_MEMORY_TOOL_DESCRIPTIONS.get(name, ""),
            requires=(
                "markdown memory in shared mode (Customer Service off) + default "
                "internal tools (or an explicit top_level_memory_tools pin)"
            ),
        )
        for name in _MEMORY_TOOL_DESCRIPTIONS
    ),
    # Per-customer isolated memory — customer-service mode. Filtered by the same
    # top_level_memory_tools allow-list (the reported bug: a stale pin left over
    # from before Customer Service was enabled silently zeroed these out).
    *(
        InternalTool(
            name,
            CATEGORY_CUSTOMER_MEMORY,
            _customer_memory_on,
            allowlist_field="top_level_memory_tools",
            description=_CUSTOMER_MEMORY_TOOL_DESCRIPTIONS.get(name, ""),
            requires="Customer Service mode",
        )
        for name in _CUSTOMER_MEMORY_TOOL_DESCRIPTIONS
    ),
    # Multi-user action/file/subscription/scheduling tools — the identity-
    # injected set built for marketplace agents. All resolve the end-user from
    # the authenticated request context; none takes a user parameter. They are
    # deliberately NOT covered by default_internal_tools: each rides its own
    # explicit opt-in, and they are exactly what a locked-down agent keeps.
    InternalTool(
        "execute_action",
        CATEGORY_MULTI_USER,
        _customer_actions_on,
        description="Run one allow-listed action script as the current user (allowed_actions).",
        requires="Multi-user mode + a non-empty allowed_actions",
    ),
    InternalTool(
        "read_customer_file",
        CATEGORY_MULTI_USER,
        _customer_memory_on,
        description="Read a file from the current user's private folder.",
        requires="Multi-user mode",
    ),
    InternalTool(
        "write_customer_file",
        CATEGORY_MULTI_USER,
        _customer_memory_on,
        description="Write a file in the current user's private folder.",
        requires="Multi-user mode",
    ),
    InternalTool(
        "list_customer_files",
        CATEGORY_MULTI_USER,
        _customer_memory_on,
        description="List the files stored for the current user.",
        requires="Multi-user mode",
    ),
    InternalTool(
        "subscription_status",
        CATEGORY_MULTI_USER,
        _subscription_status_on,
        description="Report the current user's own subscription/trial state (read-only).",
        requires="Multi-user mode + monetization on",
    ),
    InternalTool(
        "mute_my_notifications",
        CATEGORY_MULTI_USER,
        _customer_memory_on,
        description="Pause all scheduled messages to the current user (their own opt-out switch).",
        requires="Multi-user mode",
    ),
    *(
        InternalTool(
            name,
            CATEGORY_MULTI_USER,
            _connections_on,
            description=desc,
            requires="Multi-user mode + customer_service.connections",
        )
        for name, desc in (
            (
                "connection_invite",
                "Mint a forwardable invite so another person can consent to connect.",
            ),
            (
                "connection_accept",
                "Accept an invite the current user received ('connect <CODE>').",
            ),
            ("my_connections", "List the current user's own connections and open invites."),
            (
                "send_to_connection",
                "Relay a short message to one consented connection (capped daily).",
            ),
            ("disconnect_connection", "Sever one of the current user's connections."),
            ("revoke_connection_invite", "Withdraw one of the current user's open invites."),
        )
    ),
    *(
        InternalTool(
            name,
            CATEGORY_MULTI_USER,
            _user_credentials_on,
            description=desc,
            requires="Multi-user mode + customer_service.user_credentials",
        )
        for name, desc in (
            (
                "set_my_credential",
                "Store the current user's own token (encrypted, declared names only).",
            ),
            ("list_my_credentials", "List which credential names the current user stored."),
            ("delete_my_credential", "Delete one of the current user's stored credentials."),
        )
    ),
    InternalTool(
        "resume_my_notifications",
        CATEGORY_MULTI_USER,
        _customer_memory_on,
        description="Turn scheduled messages back on for the current user after a mute.",
        requires="Multi-user mode",
    ),
    InternalTool(
        "schedule_my_task",
        CATEGORY_MULTI_USER,
        _user_scheduling_on,
        description="Schedule a recurring or one-time task for the current user (capped).",
        requires="Multi-user mode + customer_service.user_scheduling",
    ),
    InternalTool(
        "list_my_scheduled_tasks",
        CATEGORY_MULTI_USER,
        _user_scheduling_on,
        description="List the current user's own scheduled tasks.",
        requires="Multi-user mode + customer_service.user_scheduling",
    ),
    InternalTool(
        "cancel_my_scheduled_task",
        CATEGORY_MULTI_USER,
        _user_scheduling_on,
        description="Cancel one of the current user's own scheduled tasks.",
        requires="Multi-user mode + customer_service.user_scheduling",
    ),
    # RAG
    InternalTool(
        "rag_save",
        CATEGORY_RAG,
        _rag_on,
        allowlist_field="top_level_rag_tools",
        description="Store a document/passage in the vector memory.",
        requires="RAG memory on",
    ),
    InternalTool(
        "rag_search",
        CATEGORY_RAG,
        _rag_on,
        allowlist_field="top_level_rag_tools",
        description="Semantic search over the vector memory.",
        requires="RAG memory on",
    ),
    # Knowledge graph — on whenever any memory mode is on
    *(
        InternalTool(
            name,
            CATEGORY_GRAPH,
            _graph_defaults_on,
            allowlist_field="top_level_graph_tools",
            description=_GRAPH_TOOL_DESCRIPTIONS.get(name, ""),
            requires=(
                "a memory mode (markdown or RAG) on + default internal tools "
                "(or an explicit top_level_graph_tools pin)"
            ),
        )
        for name in _GRAPH_TOOL_DESCRIPTIONS
    ),
    # Notes (markdown wiki)
    *(
        InternalTool(
            name,
            CATEGORY_NOTES,
            _memory_defaults_on,
            description=_NOTES_TOOL_DESCRIPTIONS.get(name, ""),
            requires="a memory mode (markdown or RAG) on + default internal tools",
        )
        for name in _NOTES_TOOL_DESCRIPTIONS
    ),
    # L2 progressive-disclosure workspace loader
    InternalTool(
        "workspace_context",
        CATEGORY_WORKSPACE,
        _workspace_context_on,
        description="Load deeper workspace context files on demand (L2 progressive disclosure).",
        requires=(
            "memory on + top_level_workspace_context + default internal tools "
            "(or top_level_workspace_context set explicitly in the config)"
        ),
    ),
    # Agent loop. write_todos/compact_conversation are olano-owned middleware
    # (disabling one also skips wiring it, dropping its prompt section); task is
    # attached by deepagents' subagent wiring and is removed from the model's
    # bound toolset by core/disabled_tools_middleware when disabled.
    InternalTool(
        "write_todos",
        CATEGORY_AGENT_LOOP,
        note="restored by TodoListMiddleware",
        description="Maintain a visible todo/plan list for multi-step work.",
    ),
    InternalTool(
        "compact_conversation",
        CATEGORY_AGENT_LOOP,
        note="on-demand /compact tool",
        description="Summarize the conversation to free context window space.",
    ),
    InternalTool(
        "task",
        CATEGORY_AGENT_LOOP,
        note="disabling this also cuts the agent off from its subagents",
        description="Delegate a piece of work to a subagent.",
    ),
    # Filesystem & shell (from the deepagents backend, not a `tools:` entry).
    # Enforced by core/disabled_tools_middleware: disabling one removes it from
    # every model bind even though deepagents attaches it after the builder.
    InternalTool(
        "read_file",
        CATEGORY_FILESYSTEM,
        description="Read a file from the agent's workspace.",
    ),
    InternalTool(
        "write_file",
        CATEGORY_FILESYSTEM,
        description="Write a file in the agent's workspace.",
    ),
    InternalTool(
        "edit_file",
        CATEGORY_FILESYSTEM,
        description="Edit a file in place (find & replace).",
    ),
    InternalTool(
        "ls",
        CATEGORY_FILESYSTEM,
        description="List files and directories in the workspace.",
    ),
    InternalTool(
        "glob",
        CATEGORY_FILESYSTEM,
        description="Find files by glob pattern.",
    ),
    InternalTool(
        "grep",
        CATEGORY_FILESYSTEM,
        description="Search file contents by regex.",
    ),
    InternalTool(
        "execute",
        CATEGORY_FILESYSTEM,
        _shell_on,
        description="Run a shell command in the agent's backend.",
        requires="a shell-capable backend",
    ),
]


class InternalToolRow(TypedDict):
    name: str
    category: str
    on: bool
    note: str
    toggleable: bool
    description: str


def effective_internal_tools(config: AgentConfig) -> list[InternalToolRow]:
    """EVERY catalog entry, with its live on/off state for ``config``.

    Entries whose condition doesn't hold (e.g. the customer-memory tools on an
    agent without Customer Service mode) are still reported — off, locked, with
    a "requires <mode>" note — so an operator can see the platform's whole
    internal surface on any agent and learn how to enable a tool, instead of
    the row being invisible until the right mode is already on.

    For applicable entries, "on" factors in ``disabled_tools`` (the opt-out
    every entry respects — enforced by the builder's tool merge plus
    ``core/disabled_tools_middleware`` for the deepagents-attached tools) and,
    for entries with an ``allowlist_field``, whether that field — when set —
    actually names this tool; a tool excluded by the allow-list is off (and
    not toggleable here) regardless of disabled_tools, matching
    core/builder._select_tool_subset. ``toggleable`` is therefore False only
    for rows whose state isn't governed by ``disabled_tools``: condition-off
    and allow-list-excluded rows.
    """
    disabled = set(config.disabled_tools or [])
    rows: list[InternalToolRow] = []
    for t in INTERNAL_TOOL_CATALOG:
        if not t.condition(config):
            rows.append(
                InternalToolRow(
                    name=t.name,
                    category=t.category,
                    on=False,
                    note=f"requires {t.requires}" if t.requires else "not available on this agent",
                    toggleable=False,
                    description=t.description,
                )
            )
            continue
        allowlist = getattr(config, t.allowlist_field, None) if t.allowlist_field else None
        if allowlist is not None and t.name not in allowlist:
            rows.append(
                InternalToolRow(
                    name=t.name,
                    category=t.category,
                    on=False,
                    note=f"excluded by {t.allowlist_field}",
                    toggleable=False,
                    description=t.description,
                )
            )
            continue
        rows.append(
            InternalToolRow(
                name=t.name,
                category=t.category,
                on=t.name not in disabled,
                note=t.note,
                toggleable=True,
                description=t.description,
            )
        )
    return rows


def enabled_internal_tool_names(config: AgentConfig) -> list[str]:
    """Names of every catalog entry currently bound (applicable and not disabled)."""
    return [row["name"] for row in effective_internal_tools(config) if row["on"]]
