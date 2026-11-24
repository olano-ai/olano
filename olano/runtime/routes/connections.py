"""Connected services: health, OAuth and device flows.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request

from olano.models.codex_auth import _DEVICE_SESSIONS as _codex_auth_sessions
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_connections_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the connections routes to ``app``."""
    # Closures defined by an earlier route module (see routes/__init__.py).
    _vault_or_unlock = closures["_vault_or_unlock"]

    @app.get("/api/agents/{name}/gmail-watch")
    async def gmail_watch_status(name: str) -> dict[str, Any]:
        """The agent's Gmail watch as its Overview card shows it.

        Settings come from ``agent.json5 → gmail_watch``; the rest is the
        watch's own state file (status, last check, last delivery) plus the
        agent's Google connection, because the watch reads Gmail only through
        that connection and the card has to say so when there is none.
        """
        from olano.config.workspace import get_workspace
        from olano.scheduler.gmail_watch import status_report

        auth_layer.assert_agent_access(name)
        try:
            cfg = await asyncio.to_thread(_api._load_agent_config_cached, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        root = get_workspace(cfg.workspace).root
        return await asyncio.to_thread(status_report, cfg, root)

    @app.get("/api/agents/{name}/connectors/health")
    async def connectors_health(name: str) -> dict[str, Any]:
        """Mission Control: per-connector live status and actionable checks.

        Two kinds of connectors live in two different runtime registries:
          • classic channels  → ``_agent_resources[name]["channels"]``
            (Telegram, Discord, Slack, …); resolved by ``adapter.name``.
          • platform adapters → ``_agent_resources[name]["platforms"]``
            (``platform:whatsapp``, ``platform:matrix``, …); resolved by
            ``adapter.platform.value``.

        Earlier versions of this endpoint only checked the channels list,
        so any ``platform:<x>`` connector always looked "Not loaded" in the
        dashboard even when its adapter was happily running.
        """
        auth_layer.assert_agent_access(name)
        try:
            data = await asyncio.to_thread(_api._read_agent_config_cached, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        resources = runtime._agent_resources.get(name, {})
        resources_chans = resources.get("channels", []) or []
        resources_plats = resources.get("platforms", []) or []
        booted = name in runtime._runners

        def _find_attached(ctype: str) -> Any | None:
            if ctype.startswith("platform:"):
                plat_name = ctype.split(":", 1)[1]
                return next(
                    (
                        p
                        for p in resources_plats
                        if getattr(getattr(p, "platform", None), "value", None) == plat_name
                    ),
                    None,
                )
            return next(
                (c for c in resources_chans if getattr(c, "name", None) == ctype),
                None,
            )

        def _platform_default_snapshot(adapter: Any, ctype: str) -> dict[str, Any]:
            """Generic snapshot for platform adapters that don't override
            ``diagnostic_snapshot()``.  Reports the connection state and
            surfaces fatal_error_message if the adapter recorded one.
            """
            connected = bool(getattr(adapter, "is_connected", False))
            fatal = getattr(adapter, "fatal_error_message", None)
            checks: list[dict[str, Any]] = [
                {
                    "id": "runtime",
                    "ok": True,
                    "message": "Adapter is running.",
                },
                {
                    "id": "connection",
                    "ok": connected,
                    "message": (
                        "Connected to platform."
                        if connected
                        else (
                            "Adapter loaded but not yet connected — pairing, "
                            "logging in, or waiting on the upstream service."
                        )
                    ),
                },
            ]
            if fatal:
                checks.append(
                    {
                        "id": "fatal_error",
                        "ok": False,
                        "message": str(fatal),
                    }
                )
            bot_user: str | None = None
            for attr in ("_paired_jid", "_bot_user_id", "_bot_id", "_bot_username"):
                value = getattr(adapter, attr, None)
                if value:
                    bot_user = str(value)
                    break
            return {
                "adapter": ctype,
                "live": connected,
                "bot_user": bot_user,
                "checks": checks,
            }

        # Why a connector was skipped at boot (a missing credential), when it
        # was: the one case where "not running" has a known, specific fix.
        skipped_map = getattr(runtime, "_skipped_channels", None)
        skipped_here = skipped_map.get(name) if isinstance(skipped_map, dict) else None

        def _snapshot(ctype: str) -> dict[str, Any] | None:
            live = _find_attached(ctype)
            reason = skipped_here.get(ctype) if isinstance(skipped_here, dict) else None
            if live is None and isinstance(reason, str) and reason:
                return {
                    "adapter": ctype,
                    "live": False,
                    "bot_user": None,
                    "checks": [{"id": "credentials", "ok": False, "message": reason}],
                }
            if live is None:
                return {
                    "adapter": ctype,
                    "live": False,
                    "bot_user": None,
                    "checks": [
                        {
                            "id": "runtime",
                            "ok": False,
                            "message": (
                                "Adapter not running — start the agent, fix boot errors in terminal logs, "
                                "or click Restart after editing connectors."
                            ),
                        }
                    ],
                }
            fn = getattr(live, "diagnostic_snapshot", None)
            if callable(fn):
                try:
                    return cast("dict[str, Any] | None", fn())
                except Exception as exc:
                    return {
                        "adapter": ctype,
                        "live": False,
                        "bot_user": None,
                        "checks": [
                            {
                                "id": "diagnostic_error",
                                "ok": False,
                                "message": str(exc),
                            }
                        ],
                    }
            # Platform adapters get a sensible default; classic channels with
            # no diagnostic_snapshot fall through to the legacy "live=booted"
            # placeholder so we don't change their behaviour.
            if ctype.startswith("platform:"):
                return _platform_default_snapshot(live, ctype)
            return {
                "adapter": ctype,
                "live": booted,
                "bot_user": None,
                "checks": [
                    {
                        "id": "diagnostics",
                        "ok": None,
                        "message": "No automated diagnostics for this connector type yet.",
                    }
                ],
            }

        def _collect() -> list[dict[str, Any]]:
            # Off the loop: an adapter's diagnostic_snapshot() may do live
            # network probes (WhatsApp's bridge check is a sync urlopen).
            connectors: list[dict[str, Any]] = []
            for entry in data.get("channels") or []:
                ctype = entry if isinstance(entry, str) else entry.get("type")
                if not ctype:
                    continue
                live_inst = _find_attached(str(ctype))
                connectors.append(
                    {
                        "type": ctype,
                        "runtime_attached": live_inst is not None,
                        "snapshot": _snapshot(str(ctype)),
                    }
                )
            return connectors

        return {
            "agent": name,
            "agent_booted": booted,
            "connectors": await asyncio.to_thread(_collect),
        }

    @app.get("/api/auth/openai-codex/status")
    async def openai_codex_auth_status() -> dict:
        from olano.models.codex_auth import auth_status, get_codex_model_ids

        status = auth_status()
        models = get_codex_model_ids() if status.get("logged_in") else []
        return {**status, "models": models}

    @app.post("/api/auth/openai-codex/start")
    async def start_openai_codex_auth() -> dict:
        # Codex connect is BYOK in OAuth clothing — the operator's own OpenAI
        # subscription — so it honors the same plan gate as provider API keys.
        _api._reject_unless_byok_allowed()
        try:
            session = await _api._start_codex_device_flow()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return session

    @app.get("/api/auth/openai-codex/poll/{session_id}")
    async def poll_openai_codex_auth(session_id: str) -> dict:
        session = _codex_auth_sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Unknown OpenAI Codex auth session")
        payload = dict(session)
        if payload.get("status") == "approved":
            from olano.models.codex_auth import get_codex_model_ids

            payload["models"] = get_codex_model_ids()
        return payload

    # ── Connector device-code sign-in ─────────────────────────────────────────
    # The broker's second method (RFC 8628): any provider declaring a
    # ``DeviceAuth`` recipe — GitHub with the CLI's public client id today —
    # signs in with a short code typed on the provider's site. The result is
    # an ordinary connector token record at the requested scope, so the same
    # DELETE / share routes as OAuth apply afterwards. Session shape mirrors
    # the OpenAI Codex device flow.

    def _device_provider_or_404(service: str) -> Any:
        provider = _connector_provider_or_404(service)
        if provider.device is None:
            raise HTTPException(
                status_code=400,
                detail=f"{provider.name} does not offer a sign-in code; use its OAuth app.",
            )
        return provider

    @app.post("/api/auth/{service}/device/start")
    async def start_device_auth_route(service: str, request: Request) -> dict:
        """Request a one-time code for *service*. Body: ``{agent?, shared?}``."""
        from olano.core.connectors import start_device_auth

        provider = _device_provider_or_404(service)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        agent = str(body.get("agent") or "").strip() or None
        if agent:
            auth_layer.assert_agent_admin(agent)
        else:
            auth_layer.assert_admin()
        try:
            return await asyncio.to_thread(
                start_device_auth, provider, agent=agent, shared=bool(body.get("shared"))
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/auth/{service}/device/poll/{session_id}")
    async def poll_device_auth_route(service: str, session_id: str) -> dict:
        from olano.core.connectors import poll_device_auth

        _device_provider_or_404(service)
        session = poll_device_auth(session_id)
        if session is None or session.get("service") != service:
            raise HTTPException(status_code=404, detail="Unknown device sign-in session")
        return session

    @app.post("/api/auth/{service}/device/cancel/{session_id}")
    async def cancel_device_auth_route(service: str, session_id: str) -> dict:
        """Stop an in-flight sign-in so its device code dies with the panel."""
        from olano.core.connectors import cancel_device_auth, poll_device_auth

        _device_provider_or_404(service)
        session = poll_device_auth(session_id)
        if session is not None and session.get("agent"):
            auth_layer.assert_agent_admin(str(session["agent"]))
        else:
            auth_layer.assert_admin()
        return {"ok": True, "cancelled": cancel_device_auth(session_id)}

    # ── Service auth (Connections dashboard) ──────────────────────────────────

    # ── Service connections (Google + the generic OAuth connector broker) ────
    # Google predates the broker and keeps its bespoke flow (google_tools.py)
    # and legacy token paths; every other service resolves through
    # olano.core.connectors. URLs are parameterized but /api/auth/google/*
    # stays byte-identical for existing OAuth-client redirect registrations.

    def _connector_provider_or_404(service: str) -> Any:
        from olano.core.connectors import get_provider

        provider = get_provider(service)
        if provider is None:
            raise HTTPException(status_code=404, detail=f"Unknown connection service '{service}'")
        return provider

    def _connector_display_name(service: str) -> str:
        if service == "google":
            return "Google"
        from olano.core.connectors import get_provider

        provider = get_provider(service)
        return provider.name if provider else service

    @app.get("/api/auth/status")
    async def auth_status(agent: str | None = None) -> dict:
        """Return connection status for all supported services.

        ``?agent=<name>`` checks per-agent tokens (own → shared → system fallback).
        Omit for system-level status.
        """
        from olano.core.connectors import all_statuses
        from olano.tools.google_tools import google_auth_status

        if agent:
            auth_layer.assert_agent_access(agent)
        google_status = google_auth_status(agent=agent)
        if isinstance(google_status, dict):
            # Google keeps its bespoke token files, but it is one card like
            # the rest: OAuth only, and never through Olano's official app.
            google_status.setdefault("methods", ["oauth"])
        statuses = {
            "google": google_status,
            **all_statuses(agent),
        }
        # Official-connector availability, so the dashboard can offer the
        # one-click "via Olano's app" path per service. Relay credentials are
        # resolved once per request (env/vault reads); the catalog read is
        # cache-only (connectors_registry never touches the network here).
        # The kick is the one exception, and it is fire-and-forget: a stale
        # disk copy starts a throttled background fetch so an official client
        # enabled in the admin console shows up seconds after the operator
        # opens Connections, instead of after the hourly manifest poll.
        from olano.catalog.connectors_registry import kick_refresh_if_stale, official_ids
        from olano.core.connectors import get_provider, relay_capable

        kick_refresh_if_stale()

        try:
            from olano.core.connect_relay import resolve_connect_relay_config

            relay_ok = resolve_connect_relay_config() is not None
        except Exception:  # URL set but no credential — official is unusable
            relay_ok = False
        official = official_ids() if relay_ok else frozenset()
        for sid, status in statuses.items():
            if not isinstance(status, dict):
                continue
            svc_provider = get_provider(sid)
            if svc_provider is None:
                continue  # google: no broker provider, BYO-only in v1
            status["official_available"] = bool(
                svc_provider is not None and relay_capable(svc_provider) and sid in official
            )
        # token_path exposes the home-dir filesystem layout — operator detail
        # only admins need (the UI keys off connected/scope/shared).
        user = auth_layer.current_user()
        if not getattr(user, "is_admin", False):
            for status in statuses.values():
                if isinstance(status, dict):
                    status.pop("token_path", None)
        return statuses

    def _service_oauth_redirect_uri(
        request: Request, redirect_base: str | None, service: str
    ) -> str:
        base = (redirect_base or "").strip().rstrip("/")
        if not base:
            base = str(request.base_url).rstrip("/")
        return f"{base}/api/auth/{service}/callback"

    @app.post("/api/auth/{service}/start")
    async def auth_service_start(service: str, body: dict, request: Request) -> dict:
        """Begin an OAuth2 flow without opening a browser server-side.

        Returns ``authorization_url`` + ``session_id``: the dashboard shows the
        link for the user to open on any device, the provider redirects back to
        ``/api/auth/{service}/callback``, and the dashboard polls
        ``/api/auth/{service}/poll/{session_id}`` until the token is stored.
        This is headless-safe (no ``could not locate runnable browser`` error).

        Body fields:
          - creds         {"client_id", "client_secret", plus connect params
                          like "shop"/"tenant"}; falls back to the provider's
                          vault JSON blob / credential pair (for google:
                          GOOGLE_OAUTH_CLIENT blob → GOOGLE_CLIENT_ID/-SECRET
                          pair → legacy GOOGLE_OAUTH_CREDENTIALS path)
          - creds_path    (google only, legacy) explicit path to a
                          client_secret.json; like the creds fields, an
                          explicit value wins over the stored fallbacks
          - agent         (str | null) agent name for per-agent token; null → system
          - shared        (bool) whether to mark the agent token as shared
          - redirect_base (str, optional) origin for the callback URL; defaults to
                          the request origin (pass window.location.origin so the
                          redirect works behind a reverse proxy — a branded
                          <name>.olano.run deployment or an sslip.io dev host).
                          Ignored when the flow resolves to the official Olano
                          app: relay-mode connections always redirect to the
                          central relay's callback, never to this box.
        """
        agent_name: str | None = (body or {}).get("agent") or None
        shared: bool = bool((body or {}).get("shared", False))
        redirect_base: str | None = (body or {}).get("redirect_base") or None
        # Connecting a service stores credentials: agent scope needs management
        # rights on that agent (admin or its owner); system scope is admin-only.
        if agent_name:
            auth_layer.assert_agent_admin(agent_name)
        else:
            auth_layer.assert_admin()
        redirect_uri = _service_oauth_redirect_uri(request, redirect_base, service)

        if service == "google":
            from olano.tools.google_tools import start_google_auth_flow

            creds_path: str | None = (body or {}).get("creds_path") or None
            # Google now also takes the generic {"creds": {client_id, client_secret}}
            # body the other connectors use; stored fallbacks (GOOGLE_OAUTH_CLIENT
            # blob / GOOGLE_CLIENT_ID pair / legacy GOOGLE_OAUTH_CREDENTIALS path)
            # resolve inside the flow via get_credential — unlock the vault first
            # so that lookup can read it.
            google_creds = (body or {}).get("creds")
            google_creds = google_creds if isinstance(google_creds, dict) else {}
            await asyncio.to_thread(_vault_or_unlock)
            try:
                session = start_google_auth_flow(
                    creds_path=creds_path,
                    client_id=str(google_creds.get("client_id") or "") or None,
                    client_secret=str(google_creds.get("client_secret") or "") or None,
                    agent=agent_name,
                    shared=shared,
                    redirect_uri=redirect_uri,
                )
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return session.public()

        from olano.core.connectors import start_connector_auth

        provider = _connector_provider_or_404(service)
        # Unlock the vault (best-effort) so the client-credential fallback in
        # the broker can read the provider's stored OAuth app via get_credential.
        await asyncio.to_thread(_vault_or_unlock)
        creds = (body or {}).get("creds")
        try:
            generic_session = start_connector_auth(
                provider,
                creds=creds if isinstance(creds, dict) else None,
                agent=agent_name,
                shared=shared,
                redirect_uri=redirect_uri,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return generic_session.public()

    @app.get("/api/auth/{service}/poll/{session_id}")
    async def auth_service_poll(service: str, session_id: str) -> dict:
        """Poll a pending OAuth flow for its authorization URL / status."""
        if service == "google":
            from olano.tools.google_tools import poll_google_auth_flow

            session = poll_google_auth_flow(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Unknown Google OAuth session")
            return session.public()

        from olano.core.connectors import poll_connector_auth

        _connector_provider_or_404(service)
        generic = poll_connector_auth(session_id)
        if generic is None or generic.service != service:
            raise HTTPException(status_code=404, detail="Unknown OAuth session")
        return generic.public()

    @app.get("/api/auth/{service}/callback")
    async def auth_service_callback(service: str, request: Request) -> Any:
        """OAuth redirect target. Matches ?state= to the pending flow, exchanges
        the ?code= for tokens (or surfaces ?error=), and renders a small page
        telling the user to return to the Connections panel."""
        from starlette.responses import HTMLResponse

        params = request.query_params
        state = params.get("state")
        code = params.get("code")
        error = params.get("error") or params.get("error_description")
        # GoogleAuthSession or connectors.OAuthSession — both expose the same
        # status/error surface consumed below. The code→token exchange is sync
        # HTTP to the provider (up to its 30s timeout), so it runs off the
        # loop; the resolvers mutate only their own module-global session dict
        # entries (matched by state), which is safe from a worker thread —
        # dict ops are GIL-atomic and each flow touches only its own session.
        session: Any
        if service == "google":
            from olano.tools.google_tools import resolve_google_auth_callback

            session = await asyncio.to_thread(resolve_google_auth_callback, state, code, error)
        else:
            from olano.core.connectors import get_provider, resolve_connector_callback

            provider = get_provider(service)
            session = (
                await asyncio.to_thread(
                    resolve_connector_callback, provider, state, code, error, dict(params)
                )
                if provider is not None
                else None
            )

        if session is None:
            title, detail, accent = (
                "Authorization session not found",
                "This authorization link has expired or was already used. "
                "Return to the Connections panel and start again.",
                "#f46363",
            )
        elif session.status == "error":
            title, detail, accent = (
                "Authorization failed",
                f"{session.error or 'The provider reported an error.'} "
                "You can close this tab and retry.",
                "#f46363",
            )
        else:
            title, detail, accent = (
                "Authorization received",
                "Olano is finishing the connection. You can close this tab and "
                "return to the Connections panel.",
                "#48d389",
            )
        html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Olano · {_connector_display_name(service)}</title>
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

    @app.delete("/api/auth/{service}")
    async def auth_service_disconnect(service: str, agent: str | None = None) -> dict:
        """Delete the service token for the given scope (agent or system).
        Destroying a stored credential is a management action: agent scope
        needs admin-or-owner on that agent; system scope is admin-only."""
        if agent:
            auth_layer.assert_agent_admin(agent)
        else:
            auth_layer.assert_admin()
        if service == "google":
            from olano.tools.google_tools import disconnect_google

            removed = disconnect_google(agent=agent)
        else:
            from olano.core.connectors import disconnect_connector

            provider = _connector_provider_or_404(service)
            removed = disconnect_connector(provider, agent=agent)
        return {"ok": True, "removed": removed, "agent": agent}

    @app.patch("/api/auth/{service}/share")
    async def auth_service_set_shared(service: str, body: dict) -> dict:
        """Toggle the shared flag on a per-agent service token.

        Body: { "agent": "<name>", "shared": true|false }
        """
        agent_name = (body or {}).get("agent", "").strip()
        shared = bool((body or {}).get("shared", False))
        if not agent_name:
            raise HTTPException(status_code=400, detail="agent is required")
        # Sharing exposes the agent's live token to every other agent (and
        # unsharing breaks consumers) — admin-or-owner of the agent only.
        auth_layer.assert_agent_admin(agent_name)
        if service == "google":
            from olano.tools.google_tools import set_google_shared

            set_google_shared(agent_name, shared)
        else:
            from olano.core.connectors import set_connector_shared

            _connector_provider_or_404(service)
            set_connector_shared(service, agent_name, shared)
        return {"ok": True, "agent": agent_name, "shared": shared}
