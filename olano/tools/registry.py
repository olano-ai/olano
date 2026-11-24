"""Tool registry — resolves agent-config tool names into callable tools.

Each per-service toolkit module in ``olano/tools/`` (and a few in
``olano/memory/``) follows the same shape:

    >>> from olano.tools import yahoo_tools
    >>> yahoo_tools.get_tools()  # all tools in this toolkit
    >>> yahoo_tools.get_tools(["yahoo_get_quote"])
    >>> yahoo_tools.get_tool("yahoo_get_quote")

This registry just glues them together:

* ``_TOOLKITS``        — toolkit name → module path (one entry per per-service
                         file). Every toolkit name is ALSO a bundle name and
                         resolves to all the tools that toolkit exposes.
* ``_COMPOSITE_BUNDLES`` — bundles that mix multiple toolkits or expose a
                           subset of one toolkit's tools.
* ``_ALIASES``        — alternate names that should resolve to the same tool.
* ``_STATIC_TOOLS``    — eagerly-built tools (e.g. ``safe_shell``, ``node_run``)
                         that don't fit the toolkit pattern. Also where
                         ``register_tool()`` writes user-supplied tools.

``resolve_tools(names)`` is the only function builder.py calls — everything
else (catalog endpoint, capability banner, dashboard) goes through the
``list_*`` accessors below.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from olano.tools.node import node_run_tool
from olano.tools.shell import safe_shell_tool

# ── Toolkits ───────────────────────────────────────────────────────────────
# Mapping of toolkit/bundle name → module path. Every entry is automatically
# a bundle that resolves to all the tools the toolkit exposes via
# ``module.get_tools()``.
_TOOLKITS: dict[str, str] = {
    # Web / browser / HTTP
    "talk_to": "olano.tools.talk_to",
    "a2a": "olano.tools.a2a_tools",
    "time": "olano.tools.time_tools",
    "vision": "olano.tools.vision",
    "web_search": "olano.tools.web_search",
    "web_fetch": "olano.tools.web_fetch",
    "http": "olano.tools.http_tools",
    "firecrawl": "olano.tools.firecrawl",
    "browser": "olano.tools.browser",
    # Scheduler / pulse / heartbeat
    "scheduler": "olano.tools.scheduler_tools",
    "heartbeat": "olano.tools.heartbeat_tools",
    "pulse": "olano.tools.pulse_tools",
    # Generic watchdog/monitoring state: cross-run dedup, run log, feedback
    "monitor": "olano.tools.monitor_tools",
    # Agent-to-agent issue tracking (durable, assignable work items)
    "issues": "olano.tools.issue_tools",
    "groups": "olano.tools.group_tools",
    # Publish password-protected static pages from the workspace
    "pages": "olano.tools.pages_tools",
    "ideas": "olano.tools.ideas_tools",
    "cortex": "olano.tools.cortex_tools",
    # Knowledge / notes / graph / secrets
    "knowledge": "olano.tools.knowledge_tools",
    "graph": "olano.memory.graph_tools",
    "notes": "olano.memory.notes_tools",
    "secrets": "olano.tools.secret_tools",
    # Browser-reachable magic links for chat-only users (device flows, OAuth,
    # secure secret entry, file edits). Bound to every agent by default under
    # the `magic_links` autonomy capability (config/autonomy_defaults.py);
    # every mint stays HITL-gated and owner-only at call time.
    "magic_link": "olano.tools.magic_link_tools",
    # Productivity / collaboration
    "google": "olano.tools.google_tools",
    "microsoft365": "olano.tools.microsoft365_tools",
    "github": "olano.tools.github_tools",
    # Network git verbs (clone/fetch/pull/push) run as a credentialed job
    # container the agent has no shell in; local git stays in its sandbox.
    "git": "olano.tools.git_tools",
    "notion": "olano.tools.notion_tools",
    "slack": "olano.tools.slack_tools",
    "linear": "olano.tools.linear_tools",
    "jira": "olano.tools.jira_tools",
    "trello": "olano.tools.trello_tools",
    "asana": "olano.tools.asana_tools",
    "airtable": "olano.tools.airtable_tools",
    "zapier": "olano.tools.zapier_tools",
    "notification": "olano.tools.notification_tools",
    # Cross-platform chat/user id lookup (chat directory + native enumeration)
    "chat_directory": "olano.tools.chat_directory_tools",
    # Telegram Business (secretary mode) + managed-bot administration —
    # every mutating tool is additionally gated on connector toggles.
    "telegram_business": "olano.tools.telegram_business_tools",
    # WhatsApp Business, on either business connector (Twilio or Meta's Cloud
    # API), split by who may hold each tool: the everyday side is bound to every
    # agent with a business connector; the admin side (create / submit / status
    # / delete, numbers, profile, Twilio balance and usage) only by an operator.
    "whatsapp_business": "olano.tools.whatsapp_business_tools",
    "feishu": "olano.tools.feishu_tools",
    "dingtalk": "olano.tools.dingtalk_tools",
    "wecom": "olano.tools.wecom_tools",
    # CRM / finance / billing
    "hubspot": "olano.tools.hubspot_tools",
    "salesforce": "olano.tools.salesforce_tools",
    # Sales intelligence: prospect database, enrichment, sequences.
    "apollo": "olano.tools.apollo_tools",
    # B2B contact and company data: enrichment, prospecting, signals.
    "lusha": "olano.tools.lusha_tools",
    "quickbooks": "olano.tools.quickbooks_tools",
    "paypal": "olano.tools.paypal_tools",
    # Finance / markets (one toolkit per service)
    "yahoo": "olano.tools.yahoo_tools",
    "coingecko": "olano.tools.coingecko_tools",
    "alpaca": "olano.tools.alpaca_tools",
    "fred": "olano.tools.fred_tools",
    "ibkr": "olano.tools.ibkr_tools",
    "coinbase": "olano.tools.coinbase_tools",
    "binance": "olano.tools.binance_tools",
    "bybit": "olano.tools.bybit_tools",
    "okx": "olano.tools.okx_tools",
    "kraken": "olano.tools.kraken_tools",
    "dydx": "olano.tools.dydx_tools",
    "polymarket": "olano.tools.polymarket_tools",
    "wallet": "olano.tools.wallet_tools",
    # E-commerce
    "shopify": "olano.tools.shopify_tools",
    "stripe": "olano.tools.stripe_tools",
    # Design / docs
    "canva": "olano.tools.canva_tools",
    "docusign": "olano.tools.docusign_tools",
    # Social / marketing
    "x_twitter": "olano.tools.x_twitter_tools",
    "linkedin": "olano.tools.linkedin_tools",
    # Managed (credits): visit / follow / connection requests on the member's own
    # account through Olano Cloud; the official API above cannot do these.
    "linkedin_outreach": "olano.tools.linkedin_outreach_tools",
    "facebook": "olano.tools.facebook_tools",
    "instagram": "olano.tools.instagram_tools",
    "tiktok": "olano.tools.tiktok_tools",
    "douyin": "olano.tools.douyin_tools",
    "rednote": "olano.tools.rednote_tools",
    "youtube": "olano.tools.youtube_tools",
    "buffer": "olano.tools.buffer_tools",
    "mailchimp": "olano.tools.mailchimp_tools",
    # Health
    "fitbit": "olano.tools.fitbit_tools",
    "garmin": "olano.tools.garmin_tools",
    "health": "olano.tools.health_tools",
    # Media / files / cloud
    "image": "olano.tools.image_tools",
    # Deterministic HTML/CSS -> PNG/PDF on the bundled Chromium. The companion
    # to "image": a model paints the scene, this paints the text.
    "render": "olano.tools.render_tools",
    "media": "olano.tools.media_tools",
    "fileops": "olano.tools.fileops_tools",
    "workspace_files": "olano.tools.workspace_files_tools",
    "gcloud": "olano.tools.gcloud_tools",
    # Research / knowledge (one toolkit per service)
    "wikipedia": "olano.tools.wikipedia_tools",
    "arxiv": "olano.tools.arxiv_tools",
    "pubmed": "olano.tools.pubmed_tools",
    "wolfram": "olano.tools.wolfram_tools",
    # Generic SQL access via SQLAlchemy
    "sql": "olano.tools.sql_tools",
    # Community / sentiment research
    "reddit": "olano.tools.reddit_tools",
    # Market data alternatives
    "polygon": "olano.tools.polygon_tools",
    "alpha_vantage": "olano.tools.alpha_vantage_tools",
    # Weather
    "weather": "olano.tools.weather_tools",
    # Free fallback search
    "ddg": "olano.tools.ddg_tools",
    # Integration platforms / sandboxed code execution
    "composio": "olano.tools.composio_tools",
    "e2b": "olano.tools.e2b_tools",
    # Olano self-management (AgentFather) — manage agents, tools, MCP,
    # skills, config, and logs from inside an agent.
    "olano_admin": "olano.tools.olano_admin",
    "self_config": "olano.tools.self_config_tools",
}


# ── Static / custom tools ──────────────────────────────────────────────────
# Eagerly-built tools that don't fit the toolkit pattern, plus a slot for
# user-registered tools via ``register_tool()``.
_STATIC_TOOLS: dict[str, Any] = {
    "safe_shell": safe_shell_tool,
    "node_run": node_run_tool,
}


# ── Aliases ────────────────────────────────────────────────────────────────
# Map an alternate name to the canonical tool name an agent should resolve.
# Resolution looks up the canonical name in the toolkit index / static table.
_ALIASES: dict[str, str] = {
    "get_current_date": "get_current_time",  # time_tools
    "talk_to": "talk_to_agent",  # talk_to_tools (kept for back-compat configs)
    # vision shipped the same tool under two names; `image_describe` is the one
    # that survives (it matches image_generate/image_edit and is what the
    # archetypes list). Kept resolvable for configs written against the old name.
    "describe_image": "image_describe",
    # The feature is "magic links" now; the status tool shipped as
    # `handshake_status` and is named in already-deployed agent.json5 files.
    "handshake_status": "magic_link_status",
    # Every link-minting tool now carries the `magic_link_` prefix so the family
    # reads as one thing (they all hand off to a human in a browser). The bare
    # names are in every deployed AgentFather config; migration 0016 rewrites
    # them, and these keep the un-migrated ones working.
    "connect_service": "magic_link_connect_service",
    "request_secret": "magic_link_request_secret",
    "edit_instructions": "magic_link_edit_instructions",
    "request_file_edit": "magic_link_request_file_edit",
    "connect_channels": "magic_link_connect_channels",
    "open_dashboard": "magic_link_open_dashboard",
}


# ── Tool tags ──────────────────────────────────────────────────────────────
# Free-form discovery labels: "which tools serve customer support?". Tags are
# pure metadata — they never affect resolution, bundling, or permissions. They
# exist so an operator (or AgentFather, via ``olano_find_tools``) can locate
# a capability by intent instead of by remembering a tool name.
_TOOL_TAGS: dict[str, tuple[str, ...]] = {
    # Per-customer isolated memory — the privacy surface of the
    # CustomerServiceAgent archetype (memory.per_customer).
    "remember": ("customer_service", "privacy", "memory"),
    "recall": ("customer_service", "privacy", "memory"),
    "recall_all": ("customer_service", "privacy", "memory"),
    "forget_me": ("customer_service", "privacy", "memory", "gdpr"),
}

# Tools the **builder** wires per-agent rather than the registry resolving them
# by name — they are created against a live agent/workspace (see
# ``core/builder.py:_build_extra_tools``) and therefore never appear in
# ``list_tool_names()``. Recording where each one comes from keeps it findable:
# ``find_tools_by_tag("customer_service")`` answers "what stores customer data
# privately?" even when no such agent is currently booted.
_DYNAMIC_TOOL_SOURCES: dict[str, str] = {
    "remember": "memory.per_customer — olano/memory/customer_memory.py",
    "recall": "memory.per_customer — olano/memory/customer_memory.py",
    "recall_all": "memory.per_customer — olano/memory/customer_memory.py",
    "forget_me": "memory.per_customer — olano/memory/customer_memory.py",
}


# ── Composite bundles ──────────────────────────────────────────────────────
# Bundles that mix tools from multiple toolkits OR expose a subset of one
# toolkit. Pure single-toolkit bundles (e.g. ``github``, ``slack``) are
# derived automatically from ``_TOOLKITS`` and don't need an entry here.
_COMPOSITE_BUNDLES: dict[str, list[str]] = {
    "finance": [
        # Yahoo Finance
        "yahoo_get_quote",
        "yahoo_get_history",
        "yahoo_get_financials",
        "yahoo_get_dividends",
        "yahoo_get_news",
        "yahoo_compare_symbols",
        "yahoo_get_options_summary",
        # CoinGecko (public API — no key required)
        "coingecko_get_price",
        "coingecko_trending",
        "coingecko_top_coins",
        "coingecko_get_coin_info",
        "coingecko_get_ohlc",
        "coingecko_search",
        # Alpaca (requires ALPACA_API_KEY + ALPACA_SECRET_KEY)
        "alpaca_get_account",
        "alpaca_list_positions",
        "alpaca_list_orders",
        "alpaca_get_bars",
        "alpaca_get_portfolio_history",
        "alpaca_place_order",
        "alpaca_cancel_order",
    ],
    "wallet_cold": [
        "wallet_get_balance",
        "wallet_get_transactions",
        "wallet_unlock_hot_mode",
    ],
    "wallet_hot": [
        "wallet_get_balance",
        "wallet_get_transactions",
        "wallet_unlock_hot_mode",
        "wallet_send",
        "wallet_lock",
    ],
    "web": [
        "page_read",
        "page_extract",
        "http_request",
        "url_download",
        "page_screenshot",
        "image_describe",
        "web_search",
    ],
    "ops": [
        "scheduler_create",
        "scheduler_list",
        "scheduler_get",
        "scheduler_update",
        "scheduler_delete",
        "scheduler_history",
        "scheduler_search",
        "scheduler_run_now",
        "heartbeat_list",
        "heartbeat_add",
        "heartbeat_update",
        "heartbeat_remove",
        "heartbeat_install_from_skill",
        "heartbeat_status",
        "heartbeat_configure",
        "pulse_list",
        "pulse_generate",
        "pulse_accept",
        "pulse_reject",
        "pulse_defer",
        "pulse_clear_expired",
        "approvals_list",
        "approvals_approve",
        "approvals_reject",
        "approvals_defer",
        "approvals_clear_expired",
        "ops_list",
        "ops_reject",
        "ops_defer",
        "ops_clear_expired",
        "gmail_send",
        "notification_send_text",
        "chats_find",
        "chats_list",
        "chats_find_user",
        "chat_info",
        "chat_messages_recent",
        "chat_messages_search",
    ],
    "ecommerce": [
        "shopify_list_products",
        "shopify_list_orders",
        "stripe_get_balance",
        "stripe_list_payments",
        "stripe_get_revenue",
    ],
    "social": [
        "x_post_tweet",
        "x_search",
        "linkedin_post",
        "youtube_get_analytics",
        "buffer_create_post",
        "buffer_list_scheduled",
        "facebook_publish_post",
        "instagram_publish_image",
    ],
    "marketing": [
        "mailchimp_list_audiences",
        "mailchimp_create_campaign",
        "buffer_create_post",
        "linkedin_post",
        "x_post_tweet",
        "youtube_get_analytics",
        "facebook_publish_post",
        "facebook_get_page_insights",
        "instagram_publish_image",
        "instagram_get_media_insights",
    ],
    # All Meta surfaces (Facebook + Instagram) in one bundle.
    "meta": [
        "facebook_list_pages",
        "facebook_publish_post",
        "facebook_get_page_insights",
        "facebook_get_post_comments",
        "facebook_reply_to_comment",
        "facebook_hide_comment",
        "instagram_get_account",
        "instagram_publish_image",
        "instagram_publish_reel",
        "instagram_get_media_insights",
        "instagram_get_comments",
        "instagram_reply_to_comment",
        "instagram_search_hashtag",
    ],
}


# Tool sets exposed per backend tier (default tools added by the builder).
_BACKEND_TOOLS: dict[str, list[str]] = {
    "filesystem": [],
    "filesystem+shell": ["safe_shell"],
}


# ── Lazy toolkit index ─────────────────────────────────────────────────────
# Built on first lookup: tool_name → toolkit module path.
_NAME_INDEX: dict[str, str] = {}
_INDEX_BUILT = False


def _static_index() -> dict[str, str]:
    """The generated name → module index (empty when the file is missing).

    Kept in a separate generated module (``_name_index.py``, regenerated by
    ``scripts/gen_tool_name_index.py``) so resolving one tool name doesn't
    require importing every toolkit to learn who owns it. Consulted only until
    the full live scan has run — after that the live index is authoritative.
    """
    try:
        from olano.tools._name_index import NAME_INDEX

        return NAME_INDEX
    except Exception:  # pragma: no cover - generated file absent/broken
        return {}


def _ensure_index() -> None:
    """Import every toolkit once and snapshot tool-name → module path."""
    global _INDEX_BUILT
    if _INDEX_BUILT:
        return
    for toolkit, mod_path in _TOOLKITS.items():
        try:
            mod = importlib.import_module(mod_path)
        except Exception as e:
            print(f"Warning: could not import toolkit '{toolkit}' ({mod_path}): {e}")
            continue
        get_tools = getattr(mod, "get_tools", None)
        if get_tools is None:
            print(f"Warning: toolkit '{toolkit}' has no get_tools()")
            continue
        try:
            tools = get_tools()
        except Exception as e:
            print(f"Warning: toolkit '{toolkit}'.get_tools() failed: {e}")
            continue
        for t in tools:
            name = getattr(t, "name", None)
            if name:
                _NAME_INDEX[name] = mod_path
    _INDEX_BUILT = True


def _resolve_name(name: str) -> Any | None:
    """Resolve a single tool name to a callable tool object (or None)."""
    canonical = _ALIASES.get(name, name)

    if canonical in _STATIC_TOOLS:
        return _STATIC_TOOLS[canonical]

    # Fast path: the generated static index names the owning toolkit, so only
    # that one module gets imported. Anything unexpected — name not indexed,
    # module won't import, tool no longer defined there — falls through to the
    # authoritative full scan, so a stale index can't change behavior.
    if not _INDEX_BUILT:
        static_mod = _static_index().get(canonical)
        if static_mod is not None:
            try:
                tool = importlib.import_module(static_mod).get_tool(canonical)
            except Exception:
                tool = None
            if tool is not None:
                return tool

    _ensure_index()
    mod_path = _NAME_INDEX.get(canonical)
    if mod_path is not None:
        mod = importlib.import_module(mod_path)
        return mod.get_tool(canonical)

    # Final fallback: dotted import path (e.g. "myproject.my_tools.foo").
    if "." in name:
        try:
            parts = name.rsplit(".", 1)
            mod = importlib.import_module(parts[0])
            return getattr(mod, parts[1])
        except (ImportError, AttributeError) as e:
            print(f"Warning: could not load tool '{name}': {e}")
            return None

    return None


# ── Public API ─────────────────────────────────────────────────────────────


def register_tool(name: str, tool: Any) -> None:
    """Register a custom tool under a given name (overrides toolkit entries)."""
    _STATIC_TOOLS[name] = tool
    apply_tool_tags([tool])


# ── Tags ───────────────────────────────────────────────────────────────────


def get_tool_tags(name: str) -> list[str]:
    """Return the discovery tags for a tool name (empty when untagged)."""
    return list(_TOOL_TAGS.get(_ALIASES.get(name, name), ()))


def list_tool_tags() -> dict[str, list[str]]:
    """Return every tag mapped to the tool names carrying it."""
    out: dict[str, list[str]] = {}
    for name, tags in _TOOL_TAGS.items():
        for tag in tags:
            out.setdefault(tag, []).append(name)
    return {tag: sorted(names) for tag, names in sorted(out.items())}


def find_tools_by_tag(tag: str) -> list[str]:
    """Return the tool names carrying ``tag`` (case-insensitive, sorted)."""
    wanted = tag.strip().lower()
    return sorted(name for name, tags in _TOOL_TAGS.items() if wanted in tags)


def get_dynamic_tool_source(name: str) -> str:
    """Explain what wires a builder-injected tool, or '' for registry tools.

    Builder-injected tools (per-customer memory, notes, graph, …) are bound to a
    live agent and never resolve through ``resolve_tools``, so ``describe_tool``
    would otherwise report nothing at all for them.
    """
    return _DYNAMIC_TOOL_SOURCES.get(_ALIASES.get(name, name), "")


def apply_tool_tags(tools: Any) -> None:
    """Stamp registry tags onto live tool objects' ``metadata`` (idempotent).

    Called for every dynamically-registered tool so an agent's bound tools carry
    the same labels the catalog reports.
    """
    for tool in tools:
        tags = get_tool_tags(str(getattr(tool, "name", "")))
        if not tags:
            continue
        try:
            metadata = dict(getattr(tool, "metadata", None) or {})
            metadata["tags"] = tags
            object.__setattr__(tool, "metadata", metadata)
        except Exception:  # pragma: no cover - exotic tool objects
            continue


# Filesystem/shell tools provided by the deepagents backend itself (added by
# create_deep_agent(backend=...)), NOT the olano registry. Listing them in an
# allowlist (e.g. a cognition worker's tools_allow) is valid — the backend wires
# them in — so resolve_tools skips them silently instead of crying "unknown tool".
_BACKEND_BUILTIN_TOOLS: frozenset[str] = frozenset({"ls", "read_file", "write_file", "edit_file"})

# Tools that no longer exist. Migration 0023 strips them from agent.json5 on
# upgrade, but an archetype installed afterwards from an older pack can still
# list one, and "unknown tool" on every boot would be noise about a name the
# operator cannot act on. ``ask_user`` drew a picker on the terminal chat,
# which was removed.
_RETIRED_TOOLS: frozenset[str] = frozenset({"ask_user"})


def resolve_tools(tool_names: list[str]) -> list[Any]:
    """Resolve a list of tool name strings into callable tool objects.

    Tools globally disabled via the dashboard are silently skipped, and so are
    the browser tools while ``config.yaml → browser`` leaves the browser off
    (:func:`olano.browser.policy.browser_tools_allowed`). This is the one
    resolution every binding path shares (top level, subagents, compiled
    subagents, cognition allowlists), so the browser gate cannot be missed by
    a path that never sees an ``AgentConfig``.
    """
    disabled = get_disabled_tools()
    from olano.browser.policy import BROWSER_TOOL_NAMES, browser_tools_allowed

    browser_off = any(n in BROWSER_TOOL_NAMES for n in tool_names) and not browser_tools_allowed()
    resolved: list[Any] = []
    for name in tool_names:
        if name in disabled:
            print(f"Note: tool '{name}' is globally disabled — skipping.")
            continue
        if browser_off and name in BROWSER_TOOL_NAMES:
            continue
        tool = _resolve_name(name)
        if tool is None:
            # Backend-provided builtins are added by the deepagents backend, not
            # here — skip quietly rather than warning about a non-issue.
            if name not in _BACKEND_BUILTIN_TOOLS and name not in _RETIRED_TOOLS:
                print(f"Warning: unknown tool '{name}', skipping.")
            continue
        resolved.append(tool)
    return resolved


def resolve_bundle(bundle_name: str) -> list[Any]:
    """Resolve a named tool bundle into tool objects.

    Order of precedence:
      1. Composite bundle (explicit name list).
      2. Toolkit (returns all tools that toolkit exposes).
      3. Empty list if unknown.
    """
    if bundle_name in _COMPOSITE_BUNDLES:
        return resolve_tools(_COMPOSITE_BUNDLES[bundle_name])
    mod_path = _TOOLKITS.get(bundle_name)
    if mod_path is None:
        return []
    try:
        mod = importlib.import_module(mod_path)
    except Exception as e:
        print(f"Warning: could not import toolkit '{bundle_name}': {e}")
        return []
    return list(mod.get_tools())


def get_backend_tools(backend: str) -> list[Any]:
    """Return the default tool set for the given backend tier."""
    names = _BACKEND_TOOLS.get(backend, _BACKEND_TOOLS["filesystem+shell"])
    return resolve_tools(names)


def list_tool_names() -> list[str]:
    """Return every known tool name (toolkit-resolved + static + aliases)."""
    _ensure_index()
    names: set[str] = set(_NAME_INDEX.keys())
    names.update(_STATIC_TOOLS.keys())
    names.update(_ALIASES.keys())
    return sorted(names)


def list_bundles() -> dict[str, list[str]]:
    """Return every bundle name and the tool names it expands to.

    Composite bundles are returned verbatim; pure-toolkit bundles are
    expanded from each toolkit's ``get_tools()``.
    """
    out: dict[str, list[str]] = {}
    for name, names in _COMPOSITE_BUNDLES.items():
        out[name] = list(names)
    for toolkit, mod_path in _TOOLKITS.items():
        if toolkit in out:
            continue
        try:
            mod = importlib.import_module(mod_path)
            out[toolkit] = [t.name for t in mod.get_tools()]
        except Exception:
            out[toolkit] = []
    return out


def list_integrations() -> dict[str, list[dict[str, Any]]]:
    """Concise catalog of integration tool-packs for an agent's ``tools`` list.

    Splits the registry into per-service ``toolkits`` (one integration each) and
    curated ``composite_bundles`` (cross-service mixes). Each row carries a
    ``tool_count`` and ``enabled_count`` (after global disables) — a high-level
    "what can I wire?" view. Drill into the member tool names with
    :func:`list_bundles` and their descriptions with :func:`get_tool_description`.
    """
    bundles = list_bundles()
    disabled = get_disabled_tools()

    def _row(name: str, names: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "tool_count": len(names),
            "enabled_count": sum(1 for n in names if n not in disabled),
        }

    toolkits = [_row(name, bundles.get(name, [])) for name in sorted(_TOOLKITS)]
    composites = [_row(name, names) for name, names in sorted(_COMPOSITE_BUNDLES.items())]
    return {"toolkits": toolkits, "composite_bundles": composites}


def get_tool_description(name: str) -> str:
    """Return the human-readable description for a tool, or ''."""
    tool = _resolve_name(name)
    if tool is None:
        return _dynamic_tool_descriptions().get(_ALIASES.get(name, name), "")
    raw = getattr(tool, "description", "") or ""
    return raw.strip().split("\n\n")[0].strip()


_DYNAMIC_DESCRIPTIONS: dict[str, str] | None = None


def _dynamic_tool_descriptions() -> dict[str, str]:
    """Descriptions for builder-injected tools, read from their real factories.

    Keeps ``describe_tool("remember")`` useful when no per-customer agent has
    booted yet (nothing has called ``register_tool`` for it). Building the
    per-customer tools is pure closure construction — no IO, no agent, no
    workspace — so a throwaway instance is a safe way to read the live
    docstrings instead of duplicating them in this file. Never route
    ``resolve_tools`` through here: these instances are unbound.
    """
    global _DYNAMIC_DESCRIPTIONS
    if _DYNAMIC_DESCRIPTIONS is None:
        out: dict[str, str] = {}
        try:
            from olano.memory.customer_memory import make_customer_memory_tools

            for tool in make_customer_memory_tools("catalog"):
                raw = getattr(tool, "description", "") or ""
                out[tool.name] = raw.strip().split("\n\n")[0].strip()
        except Exception as exc:  # pragma: no cover - import-time breakage only
            print(f"Warning: could not read per-customer tool descriptions: {exc}")
        _DYNAMIC_DESCRIPTIONS = out
    return _DYNAMIC_DESCRIPTIONS


# ── Globally disabled tools ────────────────────────────────────────────────
# Names listed here are skipped by ``resolve_tools`` even when an agent asks
# for them. Persisted at ``~/.olano/tools_disabled.json`` (or the
# equivalent under ``$LIONCLAW_HOME``) so the dashboard can edit it.


def _disabled_path() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().root / "tools_disabled.json"


def get_disabled_tools() -> set[str]:
    p = _disabled_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return set(data.get("disabled") or [])


def set_tool_disabled(name: str, disabled: bool) -> set[str]:
    p = _disabled_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    current = get_disabled_tools()
    if disabled:
        current.add(name)
    else:
        current.discard(name)
    p.write_text(json.dumps({"disabled": sorted(current)}, indent=2), encoding="utf-8")
    return current
