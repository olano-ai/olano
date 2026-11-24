"""Defaults for auto-generated ToolkitAgent definitions.

When a toolkit in ``olano.tools.registry:_TOOLKITS`` has no hand-authored
JSON5 file under ``definitions/``, the registry synthesizes a
``ToolkitAgentDef`` using these defaults so every toolkit is usable as a
ToolkitAgent out of the box.
"""

from __future__ import annotations

from olano.config.agent_config import SecurityConfig

# ── Pretty names / emojis ──────────────────────────────────────────────────

_AGENT_NAME_OVERRIDES: dict[str, str] = {
    "google": "GoogleAgent",
    "microsoft365": "Microsoft365Agent",
    "github": "GitHubAgent",
    "x_twitter": "XTwitterAgent",
    "youtube": "YouTubeAgent",
    "ms365": "Microsoft365Agent",
    "okx": "OKXAgent",
    "ibkr": "IBKRAgent",
    "dydx": "dYdXAgent",
    "fileops": "FileOpsAgent",
    "gcloud": "GCloudAgent",
    "fred": "FREDAgent",
    "wecom": "WeComAgent",
    "dingtalk": "DingTalkAgent",
    "quickbooks": "QuickBooksAgent",
    "paypal": "PayPalAgent",
    "hubspot": "HubSpotAgent",
    "docusign": "DocuSignAgent",
}

_EMOJI: dict[str, str] = {
    "alpaca": "📈",
    "ibkr": "📈",
    "yahoo": "📈",
    "coingecko": "🪙",
    "fred": "📊",
    "binance": "🪙",
    "coinbase": "🪙",
    "kraken": "🪙",
    "bybit": "🪙",
    "okx": "🪙",
    "dydx": "🪙",
    "polymarket": "🎯",
    "wallet": "💼",
    "stripe": "💳",
    "paypal": "💳",
    "quickbooks": "📒",
    "shopify": "🛍️",
    "hubspot": "🎯",
    "salesforce": "☁️",
    "apollo": "🚀",
    "lusha": "🔎",
    "google": "📧",
    "microsoft365": "📧",
    "slack": "💬",
    "github": "🐙",
    "git": "🐙",
    "linear": "📋",
    "jira": "🪲",
    "trello": "📋",
    "asana": "📋",
    "airtable": "🗂️",
    "notion": "📝",
    "zapier": "⚡",
    "feishu": "💬",
    "dingtalk": "💬",
    "wecom": "💬",
    "notification": "🔔",
    "canva": "🎨",
    "docusign": "✍️",
    "x_twitter": "𝕏",
    "linkedin": "💼",
    "youtube": "▶️",
    "buffer": "📅",
    "mailchimp": "📨",
    "fitbit": "⌚",
    "garmin": "⌚",
    "health": "🩺",
    "image": "🖼️",
    "media": "🎬",
    "fileops": "📁",
    "gcloud": "☁️",
    "web_search": "🔎",
    "web_fetch": "🌐",
    "http": "🌐",
    "firecrawl": "🕷️",
    "browser": "🧭",
    "vision": "👁️",
    "scheduler": "⏰",
    "heartbeat": "💓",
    "pulse": "✨",
    "knowledge": "📚",
    "graph": "🕸️",
    "notes": "🗒️",
    "secrets": "🔐",
    "time": "🕐",
    "talk_to": "🗣️",
}

_CATEGORY: dict[str, str] = {
    # External services that ship in the picker
    "google": "Productivity",
    "microsoft365": "Productivity",
    "github": "Productivity",
    "git": "Productivity",
    "notion": "Productivity",
    "slack": "Productivity",
    "linear": "Productivity",
    "jira": "Productivity",
    "trello": "Productivity",
    "asana": "Productivity",
    "airtable": "Productivity",
    "zapier": "Productivity",
    "notification": "Productivity",
    "feishu": "Productivity",
    "dingtalk": "Productivity",
    "wecom": "Productivity",
    "hubspot": "CRM",
    "salesforce": "CRM",
    "apollo": "CRM",
    "lusha": "CRM",
    "quickbooks": "Finance",
    "paypal": "Finance",
    "stripe": "Finance",
    "yahoo": "Markets",
    "coingecko": "Markets",
    "alpaca": "Markets",
    "fred": "Markets",
    "ibkr": "Markets",
    "coinbase": "Markets",
    "binance": "Markets",
    "bybit": "Markets",
    "okx": "Markets",
    "kraken": "Markets",
    "dydx": "Markets",
    "polymarket": "Markets",
    "wallet": "Markets",
    "shopify": "E-commerce",
    "canva": "Design",
    "docusign": "Design",
    "x_twitter": "Social",
    "linkedin": "Social",
    "youtube": "Social",
    "buffer": "Marketing",
    "mailchimp": "Marketing",
    "fitbit": "Health",
    "garmin": "Health",
    "health": "Health",
    "image": "Media",
    "media": "Media",
    "fileops": "Cloud",
    "gcloud": "Cloud",
    "web_search": "Web",
    "web_fetch": "Web",
    "http": "Web",
    "firecrawl": "Web",
    "browser": "Web",
}

# Toolkits whose ToolkitAgent should NOT appear in the dashboard picker.
# They still resolve as subagents when explicitly referenced — they're just
# plumbing, not "services you integrate with".
_CATALOG_HIDDEN: set[str] = {
    "time",
    "talk_to",
    "vision",
    "heartbeat",
    "pulse",
    "scheduler",
    "knowledge",
    "graph",
    "notes",
    "secrets",
    # Class-4 by policy: gcloud authenticates with a service-account file and
    # is not in the sandbox image; the toolkit answers `[unsupported]`.
    "gcloud",
}

# ── Default factories ──────────────────────────────────────────────────────


def default_agent_name(toolkit: str) -> str:
    """Map a toolkit slug to its ``ToolkitAgent`` class name."""
    if toolkit in _AGENT_NAME_OVERRIDES:
        return _AGENT_NAME_OVERRIDES[toolkit]
    parts = toolkit.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts if p) + "Agent"


def default_emoji(toolkit: str) -> str:
    return _EMOJI.get(toolkit, "🦁")


def default_category(toolkit: str) -> str:
    return _CATEGORY.get(toolkit, "Other")


def default_catalog_visible(toolkit: str) -> bool:
    return toolkit not in _CATALOG_HIDDEN


def default_credential_env_vars(toolkit: str) -> list[str]:
    """The credential names a toolkit cannot run without, for the catalog card.

    Derived from ``tools/requirements.BUNDLE_REQUIREMENTS`` — the one map of
    what each toolkit reads (``tests/test_toolkit_requirements.py`` pins it
    to the toolkit code). A second hand-maintained list here drifted for
    years (``NOTION_TOKEN`` vs the ``NOTION_API_KEY`` the toolkit reads,
    Google's client id/secret pair for a toolkit that takes a token file);
    the requirements entry is the source of truth. Only non-optional names:
    an alias, a testnet pair or a per-mode key is guidance, not a badge.
    """
    from olano.tools.requirements import BUNDLE_REQUIREMENTS

    specs = (BUNDLE_REQUIREMENTS.get(toolkit) or {}).get("env_vars") or []
    return [str(spec["name"]) for spec in specs if spec.get("name") and not spec.get("optional")]


def default_system_prompt(toolkit: str, agent_name: str) -> str:
    """Generic prompt for any toolkit that doesn't ship a hand-authored one."""
    return (
        f"You are {agent_name}, the Olano specialist for the '{toolkit}' "
        "toolkit. Use only the tools in this toolkit — route requests outside "
        "your domain back to the supervising agent.\n"
        "Prefer reading, listing, and searching before mutating state unless "
        "the user already provided exact IDs and explicitly requested a write.\n"
        "Summarize every external mutation with the object type, identifier, "
        "and the next recommended verification step.\n"
        "Never fabricate IDs, balances, statuses, or document contents. If "
        "credentials or permissions are missing, explain the required setup "
        "rather than guessing."
    )


def default_security() -> SecurityConfig:
    """Default security profile for an auto-generated ToolkitAgent.

    Conservative: developer profile, HITL on writes, PII scan on input. Hand-
    authored definitions can override with a literal `security` block.
    """
    return SecurityConfig(
        profile="developer",
        hitl_enabled=True,
        hitl_write_tools=True,
        hitl_shell=False,
        pii_scan_input=True,
        pii_scan_output=False,
    )
