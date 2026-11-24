"""MCP servers on an agent: status, health, OAuth, CRUD.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_mcp_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the mcp routes to ``app``."""
    # ── MCP CRUD ──────────────────────────────────────────────────────────────

    def _mcp_entry_from_body(body: _api.MCPCreateRequest) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "url": body.url.strip(),
            "transport": (body.transport or "sse").strip() or "sse",
            "enabled": bool(body.enabled),
        }
        if body.command:
            entry["command"] = body.command.strip()
        if body.args:
            entry["args"] = [str(arg) for arg in body.args]
        if body.env:
            entry["env"] = {str(k): str(v) for k, v in body.env.items()}
        if body.disabled_tools:
            entry["disabled_tools"] = [str(tool) for tool in body.disabled_tools]
        if body.headers:
            entry["headers"] = {str(k): str(v) for k, v in body.headers.items()}
        auth_mode = (body.auth or "none").strip().lower()
        if auth_mode and auth_mode != "none":
            entry["auth"] = auth_mode
        if body.oauth_scope and body.oauth_scope.strip():
            entry["oauth_scope"] = body.oauth_scope.strip()
        if body.oauth_client_id and body.oauth_client_id.strip():
            entry["oauth_client_id"] = body.oauth_client_id.strip()
        if body.oauth_client_secret and body.oauth_client_secret.strip():
            entry["oauth_client_secret"] = body.oauth_client_secret.strip()
        return entry

    async def _probe_mcp_server(agent_name: str, entry: dict[str, Any]) -> dict[str, Any]:
        """Connect to one MCP server and return rich diagnostics for the dashboard.

        Returns the full :func:`mcp_setup.probe_server` payload — ``ok``,
        ``status`` (connected | auth_required | error), ``message``, ``logs``,
        ``tools``, ``needs_oauth`` and ``authorized``.
        """
        from olano.core.mcp_setup import probe_server

        # Discover all tools even when some are disabled in the agent config; the
        # UI needs the full list so it can toggle individual tools back on.
        probe_entry = dict(entry)
        probe_entry["disabled_tools"] = []
        return await probe_server(agent_name, probe_entry)

    def _live_mcp_summary(agent_name: str) -> dict[str, dict[str, Any]]:
        """Last-known live MCP summary rows keyed by URL — only when the agent is
        actually running. Connection-free: reads the toolset cached at the most
        recent build/reload (the same source the chat MCP indicator uses), so the
        setup card and the indicator never disagree on which tools are live."""
        if agent_name not in getattr(runtime, "_runners", {}):
            return {}
        from olano.core.builder import get_mcp_summary

        return {row.get("url", ""): row for row in get_mcp_summary(agent_name)}

    async def _mcp_status_row(
        agent_name: str, entry: dict[str, Any], *, probe: bool = False
    ) -> dict[str, Any]:
        """One server's status row for the dashboard MCP tab.

        ``probe=False`` (the default, used on tab mount) returns a **cheap**
        snapshot derived from config + stored tokens *without* opening a network
        connection — this is what makes a revisit instant and stops the
        "shows not-authenticated, then re-connects every time" flicker. The
        authorized state is the source of truth (``oauth_token_state``, which is
        expiry-aware), so a server we hold *usable* tokens for renders
        authorized immediately — while an expired access token with no refresh
        token reads as needing re-authorization instead of a dead green pill.

        ``probe=True`` (the "Refresh status" button / "Test") does the full live
        connect + tool-listing via :func:`mcp_setup.probe_server`.
        """
        from olano.core.mcp_setup import normalize_transport, oauth_token_state

        disabled = set(entry.get("disabled_tools") or [])
        transport = entry.get("transport", "sse")
        url = entry.get("url", "")
        auth_mode = (entry.get("auth") or "none").strip().lower()
        is_stdio = normalize_transport(transport, command=entry.get("command")) == "stdio"
        # Stored tokens count as authorized even when the entry still says
        # auth:none — they only exist because an Authorize flow completed, and
        # the connect paths (probe/boot/reload) use them regardless.
        token = (
            oauth_token_state(agent_name, url)
            if url and not is_stdio
            else {"state": "none", "authorized": False, "expires_at": None}
        )
        authorized = token["authorized"]
        base = {
            "url": url,
            "transport": transport,
            "auth": auth_mode,
            # A dead-but-present token file still means this server speaks OAuth,
            # so keep the OAuth pill (and Authorize button) visible for it.
            "needs_oauth": auth_mode == "oauth" or token["state"] != "none",
            "authorized": authorized,
            "token_state": token["state"],
            "token_expires_at": token["expires_at"],
        }
        disabled_tool_rows = [
            {"name": name, "description": "", "enabled": False} for name in sorted(disabled)
        ]
        if entry.get("enabled", True) is False:
            return {
                **base,
                "enabled": False,
                "ok": None,
                "status": "disabled",
                "message": "Disabled in config",
                "tools": disabled_tool_rows,
                "logs": [],
            }

        if not probe:
            # When the agent is running we can reflect the live bound toolset from
            # the last build/reload (cached, no network) so the card shows the same
            # tools as the chat MCP indicator — with per-tool toggles — instead of
            # an empty "0 tools" list that flickers away after every refresh.
            live = _live_mcp_summary(agent_name).get(url)
            if live and live.get("ok") is True:
                live_names = [n for n in (live.get("tools") or []) if n]
                tool_rows = [
                    {"name": name, "description": "", "enabled": name not in disabled}
                    for name in live_names
                ]
                # Configured-but-disabled tools are filtered out of the live (kept)
                # set, so add them back as disabled rows the user can re-enable.
                tool_rows += [
                    {"name": name, "description": "", "enabled": False}
                    for name in sorted(disabled)
                    if name not in live_names
                ]
                tool_rows.sort(key=lambda row: row["name"])
                enabled_count = sum(1 for row in tool_rows if row["enabled"])
                return {
                    **base,
                    "enabled": True,
                    "ok": True,
                    "status": "connected",
                    "message": live.get("message")
                    or f"{enabled_count} tool{'s' if enabled_count != 1 else ''}",
                    "tools": tool_rows,
                    "logs": [],
                }

            # Cheap, connection-free snapshot.
            ok: bool | None
            if (auth_mode == "oauth" or token["state"] != "none") and not authorized:
                ok, status, message = (
                    False,
                    "auth_required",
                    "OAuth access token expired and no refresh token is stored. "
                    "Click 'Authorize' to sign in again."
                    if token["state"] == "expired"
                    else "OAuth authorization required. Click 'Authorize' to sign in.",
                )
            elif auth_mode == "oauth" or authorized:
                ok, status, message = (
                    None,
                    "authorized",
                    "Authorized · stored tokens. Click 'Refresh status' to verify live.",
                )
            else:
                ok, status, message = (
                    None,
                    "unknown",
                    "Not yet checked. Click 'Test' or 'Refresh status' to connect.",
                )
            return {
                **base,
                "enabled": True,
                "ok": ok,
                "status": status,
                "message": message,
                "tools": disabled_tool_rows,
                "logs": [],
            }

        try:
            probed = await _probe_mcp_server(agent_name, entry)
            # Restore the configured disabled flags onto the discovered tools.
            for tool in probed.get("tools") or []:
                tool["enabled"] = tool.get("name", "") not in disabled
            merged = {**base, "enabled": True, **probed}
            # A transient probe failure must not flip a server we genuinely hold
            # tokens for back to "unauthorized" — only a real auth_required does.
            if authorized and probed.get("status") != "auth_required":
                merged["authorized"] = True
            # Connecting may have refreshed the tokens on disk (the provider
            # renews an expired access token when it holds a refresh token), so
            # re-derive the expiry-aware fields for the pill.
            if url and not is_stdio:
                fresh = oauth_token_state(agent_name, url)
                merged["token_state"] = fresh["state"]
                merged["token_expires_at"] = fresh["expires_at"]
            return merged
        except Exception as exc:
            return {
                **base,
                "enabled": True,
                "ok": False,
                "status": "error",
                "message": str(exc),
                "tools": disabled_tool_rows,
                "logs": [],
            }

    @app.get("/api/agents/{name}/mcp/status")
    async def mcp_status(name: str, probe: bool = False) -> dict[str, Any]:
        """Per-server MCP status. ``?probe=true`` does a live connect for each
        server (slower); the default is a cheap, connection-free snapshot."""
        auth_layer.assert_agent_access(name)
        try:
            data = await asyncio.to_thread(_api._read_agent_config, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        rows = [
            await _mcp_status_row(name, entry, probe=probe)
            for entry in data.get("mcp_servers", []) or []
        ]
        return {"ok": True, "servers": rows}

    async def _mcp_health(name: str) -> dict[str, Any]:
        """Compact, connection-free MCP health for the chat indicator.

        For a running agent it reflects the *live* bound toolset (what the agent
        can actually call right now), from the last build/reload. Otherwise it
        falls back to a config + stored-token snapshot.
        """
        from olano.core.builder import get_mcp_summary

        running = name in getattr(runtime, "_runners", {})
        live = get_mcp_summary(name) if running else []
        servers: list[dict[str, Any]] = []
        if live:
            for s in live:
                servers.append(
                    {
                        "url": s.get("url", ""),
                        "transport": s.get("transport", ""),
                        "status": s.get("status", "unknown"),
                        "ok": s.get("ok"),
                        "tool_count": len(s.get("tools") or []),
                        "enabled": s.get("status") != "disabled",
                        "message": s.get("message", ""),
                    }
                )
        else:
            try:
                data = await asyncio.to_thread(_api._read_agent_config, name)
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
            for entry in data.get("mcp_servers", []) or []:
                row = await _mcp_status_row(name, entry, probe=False)
                servers.append(
                    {
                        "url": row.get("url", ""),
                        "transport": row.get("transport", ""),
                        "status": row.get("status", "unknown"),
                        "ok": row.get("ok"),
                        "tool_count": len([t for t in row.get("tools") or [] if t.get("enabled")]),
                        "enabled": row.get("enabled", True),
                        "message": row.get("message", ""),
                    }
                )

        enabled = [s for s in servers if s.get("enabled")]
        if not enabled:
            overall = "none"
        elif all(s.get("ok") is True for s in enabled):
            overall = "green"
        elif any(s.get("ok") is not False for s in enabled):
            # at least one connected or not-yet-confirmed → degraded, not down
            overall = "yellow"
        else:
            overall = "red"
        return {"ok": True, "running": running, "overall": overall, "servers": servers}

    async def _emit_mcp_status(name: str) -> None:
        """Push the current MCP health to dashboards over /api/stream."""
        try:
            health = await _mcp_health(name)
        except Exception:  # never let SSE emission break the triggering action
            return
        _api._push_event("mcp_status", {"name": name, **health})

    @app.get("/api/agents/{name}/mcp/health")
    async def mcp_health(name: str) -> dict[str, Any]:
        """Lightweight per-server health for the chat MCP indicator."""
        auth_layer.assert_agent_access(name)
        return await _mcp_health(name)

    @app.post("/api/agents/{name}/mcp/reload")
    async def reload_mcp(name: str) -> dict[str, Any]:
        """Rebuild + hot-swap a running agent's MCP tools/prompt without a full
        restart. Backs the chat ``/reload-mcps`` slash command and auto-runs
        after a server is authorized. No-op (running=False) if the agent is not
        currently running in this runtime."""
        try:
            await asyncio.to_thread(_api._read_agent_config, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        if not hasattr(runtime, "reload_agent_mcp"):
            raise HTTPException(status_code=501, detail="MCP reload not supported by this runtime")
        result: dict[str, Any] = await runtime.reload_agent_mcp(name)
        await _emit_mcp_status(name)
        return result

    @app.post("/api/agents/{name}/mcp/test")
    async def test_mcp(name: str, body: _api.MCPTestRequest) -> dict[str, Any]:
        try:
            await asyncio.to_thread(_api._read_agent_config, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        if not body.url.strip() and (body.transport or "").strip() != "stdio":
            raise HTTPException(status_code=400, detail="MCP server URL/name is required")
        entry = _mcp_entry_from_body(body)
        # probe_server never raises — it folds connection failures (timeouts,
        # auth-required, tracebacks) into a structured payload with full logs.
        return await _probe_mcp_server(name, entry)

    # ── MCP OAuth (interactive authorization-code + PKCE) ───────────────────

    def _mcp_oauth_redirect_uri(request: Request, redirect_base: str | None) -> str:
        base = (redirect_base or "").strip().rstrip("/")
        if not base:
            base = str(request.base_url).rstrip("/")
        # Remember this external origin so chat-driven OAuth (which has no browser
        # request to derive an origin from) can reuse it as a fallback redirect
        # target when no central relay is configured.
        from olano.core.mcp_setup import remember_public_origin

        remember_public_origin(base)
        return f"{base}/api/mcp/oauth/callback"

    @app.get("/api/mcp/oauth/redirect-uri")
    async def mcp_oauth_redirect_uri(
        request: Request, redirect_base: str | None = None
    ) -> dict[str, Any]:
        """The OAuth redirect URI the dashboard should tell users to register with
        a provider (e.g. HubSpot's *Redirect URL*), resolved with the SAME
        precedence the flow uses: the central relay when one is configured, else
        the browser origin's same-origin callback. Read-only — the setup modal
        calls this as it opens so the "register this URI" hint matches what the
        provider will actually receive."""
        from olano.core import mcp_relay

        try:
            cfg = mcp_relay.resolve_relay_config()
        except RuntimeError:
            cfg = None  # relay URL set but secret missing — fall back to origin
        if cfg is not None:
            return {"redirect_uri": cfg.callback_uri, "via": "relay"}
        base = (redirect_base or "").strip().rstrip("/") or str(request.base_url).rstrip("/")
        return {"redirect_uri": f"{base}/api/mcp/oauth/callback", "via": "origin"}

    @app.post("/api/agents/{name}/mcp/oauth/start")
    async def start_mcp_oauth(
        name: str, body: _api.MCPOAuthStartRequest, request: Request
    ) -> dict[str, Any]:
        from olano.core import mcp_relay
        from olano.core.mcp_setup import start_mcp_authorization, start_oauth_flow

        try:
            await asyncio.to_thread(_api._read_agent_config, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        if not body.url.strip():
            raise HTTPException(status_code=400, detail="MCP server URL is required for OAuth")
        entry = _mcp_entry_from_body(body)
        entry.setdefault("auth", "oauth")
        # Prefer the central relay when one is configured. The dashboard origin
        # (e.g. http://localhost:3201) is a valid redirect target only when it is
        # publicly reachable over HTTPS; for a localhost/domain-less dashboard, or
        # a DCR-less provider like HubSpot that pins one pre-registered redirect
        # URI, the provider must redirect to the relay's single callback instead.
        # start_mcp_authorization resolves that callback and starts the background
        # pump that shuttles the code back into this flow. Without a relay we keep
        # the unchanged same-origin behaviour (works for a public/single-box host).
        try:
            cfg = mcp_relay.resolve_relay_config()
        except RuntimeError as exc:  # relay URL set but its secret is missing
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            if cfg is not None:
                redirect_uri = cfg.callback_uri
                session = start_mcp_authorization(name, entry)
            else:
                redirect_uri = _mcp_oauth_redirect_uri(request, body.redirect_base)
                session = start_oauth_flow(name, entry, redirect_uri=redirect_uri)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**session.public(), "redirect_uri": redirect_uri}

    # Session ids whose successful authorization has already triggered a live
    # MCP reload — so polling the terminal "authorized" status repeatedly only
    # hot-reloads the agent once.
    _mcp_oauth_reloaded: set[str] = set()

    @app.get("/api/agents/{name}/mcp/oauth/poll/{session_id}")
    async def poll_mcp_oauth(name: str, session_id: str) -> dict[str, Any]:
        from olano.core.mcp_setup import poll_oauth_flow

        auth_layer.assert_agent_access(name)
        session = poll_oauth_flow(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Unknown MCP OAuth session")
        # First time we observe a completed authorization → hot-reload the live
        # agent so the just-unlocked tools appear without a restart.
        if (
            session.status == "authorized"
            and session_id not in _mcp_oauth_reloaded
            and hasattr(runtime, "reload_agent_mcp")
        ):
            _mcp_oauth_reloaded.add(session_id)
            try:
                await runtime.reload_agent_mcp(name)
                await _emit_mcp_status(name)
            except Exception as exc:  # never break polling on a reload hiccup
                logging.getLogger(__name__).warning(
                    "Post-authorize MCP reload failed for %s: %s", name, exc
                )
        return session.public()

    @app.delete("/api/agents/{name}/mcp/oauth")
    async def clear_mcp_oauth(name: str, url: str) -> dict[str, Any]:
        from olano.core.mcp_setup import clear_oauth_tokens

        cleared = clear_oauth_tokens(name, url)
        return {"ok": True, "cleared": cleared}

    @app.get("/api/mcp/oauth/callback")
    async def mcp_oauth_callback(request: Request) -> Any:
        """OAuth redirect target. Matches ?state= to the pending flow and feeds
        back the authorization ?code= (or ?error=). Renders a small page telling
        the user to return to the setup panel."""
        from starlette.responses import HTMLResponse

        from olano.core.mcp_setup import resolve_oauth_callback

        params = request.query_params
        state = params.get("state")
        code = params.get("code")
        error = params.get("error") or params.get("error_description")
        session = resolve_oauth_callback(state, code, error)

        if session is None:
            title, detail, accent = (
                "Authorization session not found",
                "This authorization link has expired or was already used. "
                "Return to the MCP setup panel and start again.",
                "#f46363",
            )
        elif error:
            title, detail, accent = (
                "Authorization failed",
                f"The provider reported: {error}. You can close this tab and retry.",
                "#f46363",
            )
        else:
            title, detail, accent = (
                "Authorization received",
                "Olano is finishing the connection. You can close this tab and "
                "return to the MCP setup panel.",
                "#48d389",
            )
        html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Olano · MCP OAuth</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#0b0d12; color:#e8eaed; }}
  .card {{ max-width:420px; padding:32px; border-radius:14px; background:#13161d;
          border:1px solid rgba(255,255,255,.08); text-align:center; }}
  .dot {{ width:10px; height:10px; border-radius:50%; background:{accent};
         display:inline-block; margin-right:8px; }}
  h1 {{ font-size:18px; margin:0 0 12px; }}
  p {{ font-size:14px; line-height:1.55; color:#a9b0bd; margin:0; }}
</style></head><body><div class="card">
  <h1><span class="dot"></span>{title}</h1><p>{detail}</p>
</div></body></html>"""
        return HTMLResponse(html)

    @app.post("/api/agents/{name}/mcp")
    async def add_mcp(name: str, body: _api.MCPCreateRequest) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        servers = data.setdefault("mcp_servers", []) or []
        entry = _mcp_entry_from_body(body)
        if not entry["url"]:
            raise HTTPException(status_code=400, detail="MCP server URL/name is required")
        if any(s.get("url") == entry["url"] for s in servers):
            raise HTTPException(
                status_code=409, detail=f"MCP server '{entry['url']}' already added"
            )

        servers.append(entry)
        data["mcp_servers"] = servers
        await _api._write_agent_config_async(name, data)
        return {"ok": True}

    @app.put("/api/agents/{name}/mcp")
    async def update_mcp(name: str, body: _api.MCPCreateRequest, url: str) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        servers = data.get("mcp_servers", []) or []
        idx = next((i for i, server in enumerate(servers) if server.get("url") == url), None)
        if idx is None:
            raise HTTPException(status_code=404, detail=f"MCP server '{url}' not found")
        entry = _mcp_entry_from_body(body)
        if not entry["url"]:
            raise HTTPException(status_code=400, detail="MCP server URL/name is required")
        if entry["url"] != url and any(s.get("url") == entry["url"] for s in servers):
            raise HTTPException(
                status_code=409, detail=f"MCP server '{entry['url']}' already added"
            )

        servers[idx] = entry
        data["mcp_servers"] = servers
        await _api._write_agent_config_async(name, data)
        return {"ok": True}

    @app.patch("/api/agents/{name}/mcp/tools")
    async def update_mcp_tools(name: str, body: _api.MCPToolsPatchRequest, url: str) -> dict:
        """Rewrite one server's ``disabled_tools`` and nothing else.

        This is what the dashboard's per-tool switches and its Enable all /
        Disable all buttons write through. Touching the single key means a tool
        toggle can never disturb the rest of the entry — including the OAuth
        credentials the full-entry PUT would otherwise have to carry both ways.
        """
        auth_layer.assert_agent_access(name)
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        servers = data.get("mcp_servers", []) or []
        entry = next((s for s in servers if s.get("url") == url), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"MCP server '{url}' not found")

        names = sorted({str(tool).strip() for tool in body.disabled_tools if str(tool).strip()})
        if names:
            entry["disabled_tools"] = names
        else:
            # Empty means "nothing disabled" — drop the key rather than storing
            # `[]`, matching `_mcp_entry_from_body` and the add/edit modal.
            entry.pop("disabled_tools", None)
        data["mcp_servers"] = servers
        await _api._write_agent_config_async(name, data)
        return {"ok": True, "disabled_tools": names}

    @app.delete("/api/agents/{name}/mcp")
    async def delete_mcp(name: str, url: str) -> dict:
        try:
            data = await _api._read_agent_config_for_update(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        servers = data.get("mcp_servers", []) or []
        new_list = [s for s in servers if s.get("url") != url]
        if len(new_list) == len(servers):
            raise HTTPException(status_code=404, detail=f"MCP server '{url}' not found")

        data["mcp_servers"] = new_list
        await _api._write_agent_config_async(name, data)
        return {"ok": True}
