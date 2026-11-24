"""One function decides which magic-link flow a "connect X" request mints.

Three surfaces used to carry their own copy of the same five-branch table —
the ``magic_link_connect_service`` tool, the ``/olano connect`` chat command
and the ``olano connect`` CLI — and all three hardcoded ``github|gh`` to a
flow that signed in the runtime's own ``gh`` binary. Routing now asks the
connector registry: a provider's declared methods decide, and the caller may
name one.

Order with no method named: an OAuth app (Olano's official one, else the
operator's own) → a sign-in code → OAuth anyway. The last step is deliberate:
a provider with nothing configured still mints ``connector-oauth`` so the
broker's own error names what is missing, instead of this module guessing.
A pasted key (``secret-entry``) is minted only when asked for by name — it is
the last resort, and the integration finder says when to use it.
"""

from __future__ import annotations

from typing import Any

#: The methods a caller may name. ``official_oauth``/``byo_oauth`` both mint
#: ``connector-oauth`` — the broker picks the client, BYO always winning.
METHODS: tuple[str, ...] = ("official_oauth", "byo_oauth", "device", "key")

# Ids the connector registry does not own but the router must still resolve:
# Google keeps its bespoke token files, Codex its device flow into auth.json.
_BESPOKE: dict[str, tuple[str, bool]] = {
    # id → (flow id, takes an agent)
    "google": ("google", True),
    "openai-codex": ("codex-device", False),
}
_ALIASES: dict[str, str] = {
    "gh": "github",
    "github-cli": "github",
    "gmail": "google",
    "google-workspace": "google",
    "codex": "openai-codex",
    "chatgpt": "openai-codex",
}


def canonical_service(target: str) -> str:
    return _ALIASES.get(target.strip().lower(), target.strip().lower())


def connectable_ids() -> list[str]:
    """Every id ``resolve_connect_flow`` accepts besides an MCP URL."""
    from olano.core.connectors import provider_ids

    return sorted({*provider_ids(), *_BESPOKE})


def resolve_connect_flow(
    target: str, *, agent: str = "", method: str | None = None
) -> tuple[str, dict[str, Any]]:
    """``(flow_id, params)`` for *target*; raises ``ValueError`` with a fixable message."""
    raw = (target or "").strip()
    if not raw:
        raise ValueError("Say what to connect (e.g. 'github', 'google', or an MCP server URL).")
    agent = (agent or "").strip()
    wanted = (method or "").strip().lower() or None
    if wanted is not None and wanted not in METHODS:
        raise ValueError(f"Unknown method '{method}'. One of: {', '.join(METHODS)}.")

    if raw.lower().startswith(("http://", "https://")):
        return "mcp-oauth", {"url": raw, "agent": agent}

    service = canonical_service(raw)
    if service in _BESPOKE:
        flow, takes_agent = _BESPOKE[service]
        if wanted not in (None, "byo_oauth" if flow == "google" else "device"):
            raise ValueError(f"'{raw}' connects only through its own sign-in; drop the method.")
        return flow, ({"agent": agent} if takes_agent else {})

    from olano.core.connectors import connect_methods, get_provider

    provider = get_provider(service)
    if provider is None:
        raise ValueError(
            f"Unknown service '{raw}'. Available: {', '.join(connectable_ids())}, "
            "or an MCP server URL."
        )
    methods = connect_methods(provider)
    oauth = "connector-oauth", {"service": provider.id, "agent": agent}
    device = "connector-device", {"service": provider.id, "agent": agent}

    if wanted == "key":
        names = list(methods["key"])
        if not names:
            raise ValueError(f"{provider.name} takes no pasted key; use its OAuth app.")
        return "secret-entry", {"name": names[0], "agent": agent}
    if wanted == "device":
        if not methods["device"]:
            raise ValueError(f"{provider.name} does not offer a sign-in code.")
        return device
    if wanted == "official_oauth" and not methods["official_oauth"]:
        raise ValueError(
            f"No official Olano app is available for {provider.name} on this deployment; "
            "use your own OAuth app (byo_oauth), a sign-in code, or a key."
        )
    if wanted == "byo_oauth" and not methods["byo_oauth"]:
        raise ValueError(
            f"No OAuth client is configured for {provider.name}. Store one under "
            f"{provider.creds_vault_key} or connect it from the dashboard's Connections tab."
        )
    if wanted in ("official_oauth", "byo_oauth"):
        return oauth

    if methods["official_oauth"] or methods["byo_oauth"]:
        return oauth
    if methods["device"]:
        return device
    return oauth
