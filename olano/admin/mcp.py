"""MCP server management for an agent.

Add/remove/list MCP servers in an agent's ``agent.json5`` (each entry validated
through :class:`~olano.config.agent_config.MCPServerConfig`) and probe a
server live via :func:`olano.core.mcp_setup.probe_server`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from olano.config.agent_config import AgentConfig, MCPServerConfig
from olano.config.loader import load_agent_config, mutate_agent_config


def list_mcp_servers(agent: str) -> list[dict[str, Any]]:
    """Return the MCP servers configured on ``agent``."""
    cfg = load_agent_config(agent, source="home")
    return [s.model_dump() for s in cfg.mcp_servers]


def add_mcp_server(
    agent: str,
    url: str,
    *,
    transport: str = "sse",
    auth: str = "none",
    oauth_scope: str | None = None,
    oauth_client_id: str | None = None,
    oauth_client_secret: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    disabled_tools: list[str] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Add (or replace, by URL) an MCP server on ``agent`` and save.

    ``oauth_client_id`` / ``oauth_client_secret`` are the pre-registered OAuth
    client credentials used for servers that do **not** support Dynamic Client
    Registration (e.g. HubSpot). They are only meaningful when ``auth`` is
    ``"oauth"`` and are ignored otherwise.
    """
    entry = MCPServerConfig(
        url=url,
        transport=transport,
        enabled=enabled,
        auth=auth,
        oauth_scope=oauth_scope,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
        command=command,
        args=args or [],
        env=env or {},
        headers=headers or {},
        disabled_tools=disabled_tools or [],
    )
    replaced = False

    def _apply(cfg: AgentConfig) -> None:
        nonlocal replaced
        replaced = any(s.url == url for s in cfg.mcp_servers)
        cfg.mcp_servers = [s for s in cfg.mcp_servers if s.url != url] + [entry]

    mutate_agent_config(agent, _apply)
    return {
        "agent": agent,
        "url": url,
        "transport": transport,
        "auth": auth,
        "added": True,
        "replaced": replaced,
    }


# Fields an agent needs to decide "is this the right server, and what does it
# need from me". Deliberately narrower than the full directory entry: logos,
# badges and example JSON blobs are dashboard chrome and cost prompt tokens.
_DIRECTORY_FIELDS = (
    "slug",
    "name",
    "description",
    "kind",
    "category",
    "transport",
    "auth",
    "oauthClient",
    "url",
    "command",
    "args",
    "envKeys",
    "homepageUrl",
    "appConsoleUrl",
    "appConsoleLabel",
    "notes",
)


def _directory_view(entry: dict[str, Any]) -> dict[str, Any]:
    """Trim a directory entry to the fields that inform an agent's decision."""
    return {k: entry[k] for k in _DIRECTORY_FIELDS if entry.get(k) not in (None, "", [], {})}


def directory_entries() -> list[dict[str, Any]]:
    """Every directory entry (presets + examples), trimmed like a search hit.

    Local only — in-process cache → disk → bundled, per
    :func:`olano.core.mcp_registry.get_directory`. Never a network wait, which
    is what lets the integration finder enumerate and match the whole catalog
    on every query instead of delegating matching to a remote substring filter.
    """
    from olano.core import mcp_registry

    directory = mcp_registry.get_directory()
    pool = list(directory["presets"]) + list(directory["examples"])
    return [_directory_view(e) for e in pool]


def search_mcp_directory(
    query: str = "",
    *,
    kind: str | None = None,
    category: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search the MCP server directory (cloud-synced, bundled fallback).

    This is how an agent finds a **verified** server URL instead of recalling
    one. The directory carries the endpoint, transport, auth mode and the env
    var names each server expects; a remembered URL that is subtly wrong writes
    a config that fails only when someone tries to use it.
    """
    from olano.core import mcp_registry

    # The registry clamps only the cloud query param; its offline fallback
    # slices with the raw value, where limit<=0 returns nonsense.
    limit = max(1, min(int(limit), 200))
    hits = mcp_registry.search(query or "", kind=kind, category=category, limit=limit)
    return [_directory_view(e) for e in hits]


def get_mcp_directory_entry(slug: str) -> dict[str, Any]:
    """Return one MCP directory entry by slug. Raises when the slug is unknown."""
    from olano.core import mcp_registry

    entry = mcp_registry.get_server(slug)
    if entry is None:
        raise ValueError(
            f"No MCP directory entry with slug {slug!r}. "
            "Use search_mcp_directory() to find the right slug."
        )
    return _directory_view(entry)


def remove_mcp_server(agent: str, url: str) -> dict[str, Any]:
    """Remove the MCP server with ``url`` from ``agent`` and save."""
    removed = 0

    def _apply(cfg: AgentConfig) -> None:
        nonlocal removed
        before = len(cfg.mcp_servers)
        cfg.mcp_servers = [s for s in cfg.mcp_servers if s.url != url]
        removed = before - len(cfg.mcp_servers)

    mutate_agent_config(agent, _apply)
    return {"agent": agent, "url": url, "removed": removed > 0}


async def test_mcp_server(agent: str, url: str) -> dict[str, Any]:
    """Probe a configured MCP server and return rich diagnostics.

    Delegates to :func:`olano.core.mcp_setup.probe_server` (connects, lists
    tools, detects whether OAuth is required).
    """
    from olano.core.mcp_setup import probe_server

    cfg = load_agent_config(agent, source="home")
    entry = next((s for s in cfg.mcp_servers if s.url == url), None)
    if entry is None:
        raise ValueError(f"Agent '{agent}' has no MCP server with url {url!r}.")
    return await probe_server(agent, entry.model_dump())


async def authorize_mcp_server(
    agent: str,
    url: str,
    *,
    on_authorization_url: Callable[[str], None] | None = None,
    open_browser: bool = True,
    host: str = "127.0.0.1",
    port: int = 0,
) -> dict[str, Any]:
    """Run the interactive OAuth (authorization-code + PKCE) flow for a server.

    Resolves the configured server on ``agent``, hosts a localhost redirect to
    capture the browser callback, and persists the resulting tokens under
    ``<LIONCLAW_HOME>/mcp_oauth/``. Returns the terminal flow session payload
    (``status``, ``message``, ``tool_count`` …). ``on_authorization_url`` is
    invoked once with the URL the user must open to grant access.
    """
    from olano.core.mcp_setup import authorize_mcp_and_wait

    cfg = load_agent_config(agent, source="home")
    entry = next((s for s in cfg.mcp_servers if s.url == url), None)
    if entry is None:
        raise ValueError(f"Agent '{agent}' has no MCP server with url {url!r}.")
    session = await authorize_mcp_and_wait(
        agent,
        entry.model_dump(),
        open_browser=open_browser,
        on_authorization_url=on_authorization_url,
    )
    return session.public()


async def start_mcp_authorization(agent: str, url: str) -> dict[str, Any]:
    """Begin an interactive OAuth flow **non-blocking** and return the auth link.

    For chat/agent callers: resolves the configured server on ``agent``, starts
    the flow (relay / public-origin redirect), waits briefly for the
    ``authorization_url`` to materialize, and returns the flow session
    (``session_id`` + ``authorization_url``) so the agent can send the link to
    the user over any channel. Poll :func:`mcp_authorization_status` for
    completion. Runs inside the runtime event loop.
    """
    import asyncio

    from olano.core.mcp_setup import poll_oauth_flow
    from olano.core.mcp_setup import start_mcp_authorization as _start

    cfg = load_agent_config(agent, source="home")
    entry = next((s for s in cfg.mcp_servers if s.url == url), None)
    if entry is None:
        raise ValueError(f"Agent '{agent}' has no MCP server with url {url!r}.")
    session = _start(agent, entry.model_dump())
    # Wait up to ~8s for the authorization URL so the first result already carries
    # the link the agent must relay (metadata discovery + URL build is usually fast).
    for _ in range(80):
        if session.authorization_url or session.status in {"error", "expired"}:
            break
        await asyncio.sleep(0.1)
    live = poll_oauth_flow(session.session_id) or session
    return live.public()


def mcp_authorization_status(session_id: str) -> dict[str, Any]:
    """Return the current status of an in-flight MCP OAuth session (or ``None``)."""
    from olano.core.mcp_setup import poll_oauth_flow

    session = poll_oauth_flow(session_id)
    if session is None:
        raise ValueError(f"Unknown MCP OAuth session {session_id!r}.")
    return session.public()
