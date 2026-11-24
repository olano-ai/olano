"""The inbound MCP endpoint: agents as tools, behind bearer-token auth.

Composition (all official ``mcp`` SDK pieces, wired as one ASGI chain rather
than a mounted sub-app so the runtime's FastAPI app keeps one route table and
no nested lifespan)::

    /api/mcp  →  enabled? https? ready?
              →  AuthenticationMiddleware(BearerAuthBackend(OlanoTokenVerifier))
              →  AuthContextMiddleware            (get_access_token() in tools)
              →  RequireAuthMiddleware            (401 + WWW-Authenticate, 403 scope)
              →  StreamableHTTPSessionManager     (stateless, DNS-rebinding guard)
              →  FastMCP tools: olano_list_agents, ask_<agent> …

    /api/mcp/.well-known/oauth-protected-resource   RFC 9728 metadata (proxy-reachable)
    /.well-known/oauth-protected-resource/api/mcp    the RFC-derived location

Everything origin-dependent (the resource identifier tokens are bound to, the
metadata URL, the allowed ``Host`` values) is built lazily from the deployment's
public origin — ``LIONCLAW_PUBLIC_URL``, else the origin a browser last reached
the dashboard on — and rebuilt if that origin changes, so a box that learns its
public URL after boot does not serve stale metadata.

Tool calls run the agent exactly as a peer or scheduled turn would:
``AgentRunner.ainvoke`` on an ``mcp::<client>::<thread>`` checkpoint thread,
under a ``source="mcp"`` request context that is *not* owner and *not* admin,
with a non-interactive origin — so an action the agent's HITL rules gate waits
in the Approvals inbox instead of running because a stranger asked.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from olano.config.system_settings import ExternalAccessSettings, load_external_access_settings

logger = logging.getLogger(__name__)

MCP_PATH = "/api/mcp"
METADATA_ALIAS_PATH = f"{MCP_PATH}/.well-known/oauth-protected-resource"
# Olano's own OAuth 2.1 authorization server (olano/mcp_server/oauth.py) —
# issuer = <origin> + this path.
OAUTH_ISSUER_PATH = "/api/oauth"
_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def mcp_endpoint_path() -> str:
    return MCP_PATH


def public_description(config: Any) -> str:
    """What an outside caller may learn about an agent: the operator-written
    ``external_access.description``, else the roster description. Never the
    system prompt."""
    ext = getattr(config, "external_access", None)
    text = str(getattr(ext, "description", "") or "").strip()
    if text:
        return text
    return str(getattr(config, "description", "") or "").strip()


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("_", (value or "").strip()).strip("_")
    return slug[:48] or "agent"


def _is_private_origin(origin: str) -> bool:
    host = (urlparse(origin).hostname or "").lower()
    if host in _LOOPBACK:
        return True
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith((".local", ".internal", ".lan"))
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _json(status: int, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


def authorization_servers(origin: str, settings: ExternalAccessSettings) -> list[str]:
    """RFC 9728 ``authorization_servers`` for any protected resource on this
    deployment: Olano's own authorization server (olano/mcp_server/oauth.py)
    when it is on, plus the external issuer when one is configured. With both
    off the box's origin stands in: tokens are then minted by the operator only.

    One definition for the MCP endpoint and every A2A agent, so the two
    surfaces can never send a client to different issuers."""
    origin = origin.rstrip("/")
    issuers: list[str] = []
    if settings.oauth.enabled:
        issuers.append(f"{origin}{OAUTH_ISSUER_PATH}")
    if settings.jwt.configured:
        issuers.append(settings.jwt.issuer.strip())
    if not issuers:
        issuers.append(origin)
    return issuers


class _Build:
    """Everything derived from one public origin."""

    def __init__(
        self, origin: str, settings: ExternalAccessSettings, server: OlanoMcpServer
    ) -> None:
        from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
        from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
        from mcp.server.auth.routes import create_protected_resource_routes
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings
        from pydantic import AnyHttpUrl
        from starlette.middleware.authentication import AuthenticationMiddleware

        from olano.mcp_server.auth import OlanoTokenVerifier

        self.origin = origin.rstrip("/")
        # Settings are baked into the verifier, the metadata and the AS routes,
        # so a Config change must produce a new build (see _ensure_build).
        self.settings_fingerprint = settings.model_dump_json()
        self.resource_url = f"{self.origin}{MCP_PATH}"
        self.metadata_url = f"{self.origin}{METADATA_ALIAS_PATH}"
        self.verifier = OlanoTokenVerifier(
            resource_url=self.resource_url, jwt_settings=settings.jwt, store=server.token_store
        )
        self.prm_routes = create_protected_resource_routes(
            resource_url=AnyHttpUrl(self.resource_url),
            authorization_servers=[
                AnyHttpUrl(i) for i in authorization_servers(self.origin, settings)
            ],
            scopes_supported=["agents:*"] + [f"agent:{n}" for n in server.all_exposed_names()],
            resource_name="Olano agents",
        )
        # Route.app is the ASGI-wrapped handler (the endpoint itself takes a Request).
        self.prm_handler = self.prm_routes[0].app if self.prm_routes else None

        host = urlparse(self.origin).netloc
        bare = urlparse(self.origin).hostname or host
        allowed_hosts = list(
            dict.fromkeys(
                [
                    host,
                    bare,
                    f"{bare}:*",
                    "localhost",
                    "localhost:*",
                    "127.0.0.1",
                    "127.0.0.1:*",
                    *settings.allowed_hosts,
                ]
            )
        )
        allowed_origins = list(
            dict.fromkeys(
                [
                    self.origin,
                    "http://localhost:*",
                    "http://127.0.0.1:*",
                    *settings.allowed_origins,
                ]
            )
        )
        # Authorization server routes (issuer = this origin + /api/oauth).
        self.oauth_router: Any = None
        if settings.oauth.enabled:
            from starlette.routing import Router

            try:
                server.oauth.set_origin(self.origin)
                self.oauth_router = Router(routes=server.oauth.build_routes())
            except ValueError as exc:
                # The SDK refuses a non-https issuer that is not loopback; the
                # gate in `handle` already refuses such origins for the endpoint.
                logger.warning("OAuth authorization server not built for %s: %s", self.origin, exc)
                self.oauth_router = None
        self.session_manager = StreamableHTTPSessionManager(
            app=server.fastmcp._mcp_server,
            stateless=True,
            json_response=False,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
            ),
        )

        async def _transport(scope: Scope, receive: Receive, send: Send) -> None:
            await self.session_manager.handle_request(scope, receive, send)

        chain: Any = RequireAuthMiddleware(
            _transport, required_scopes=[], resource_metadata_url=AnyHttpUrl(self.metadata_url)
        )
        chain = AuthContextMiddleware(chain)
        self.app = AuthenticationMiddleware(chain, backend=BearerAuthBackend(self.verifier))
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()

    async def serve(self) -> None:
        async with self.session_manager.run():
            self.ready.set()
            await self.stop.wait()


class OlanoMcpServer:
    """Owns the FastMCP tool surface, the token store and the ASGI entry points."""

    def __init__(
        self,
        runtime: Any,
        *,
        settings_loader: Callable[[], ExternalAccessSettings] = load_external_access_settings,
        origin_resolver: Callable[[], str | None] | None = None,
        token_store: Any | None = None,
    ) -> None:
        from mcp.server.fastmcp import FastMCP

        from olano.mcp_server.tokens import get_token_store

        self.runtime = runtime
        self._settings_loader = settings_loader
        self._origin_resolver = origin_resolver or _default_origin
        self.token_store = token_store or get_token_store()
        self.fastmcp = FastMCP(
            name="Olano agents",
            instructions=(
                "Each ask_<agent> tool sends one message to that Olano agent and returns its "
                "reply. Pass the same thread_id across calls to keep one conversation. "
                "olano_list_agents shows which agents your token may address."
            ),
        )
        self._agents: dict[str, Any] = {}  # agent name → runner
        self._tool_names: dict[str, str] = {}  # agent name → tool name
        # Olano's own authorization server: one instance so pending consents and
        # unredeemed codes survive a per-origin/tool-set rebuild of the routes.
        from olano.mcp_server.oauth import (
            OAuthClientStore,
            OlanoAuthorizationServer,
            client_store_path,
        )

        self.oauth = OlanoAuthorizationServer(
            origin="https://unknown.invalid",
            token_store=self.token_store,
            client_store=OAuthClientStore(client_store_path()),
            settings_loader=self.settings,
            exposed_names=self.all_exposed_names,
        )
        self._build: _Build | None = None
        self._build_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stopped = False
        self._register_list_tool()

    # ── settings / origin ──────────────────────────────────────────────────
    def settings(self) -> ExternalAccessSettings:
        try:
            return self._settings_loader()
        except Exception:  # pragma: no cover - loader is itself defensive
            return ExternalAccessSettings()

    def resolve_origin(self, scope: Scope | None = None) -> str | None:
        origin = self._origin_resolver()
        if origin:
            return origin.rstrip("/")
        if scope is None:
            return None
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        host = headers.get("x-forwarded-host") or headers.get("host")
        if not host:
            return None
        scheme = headers.get("x-forwarded-proto") or scope.get("scheme") or "http"
        return f"{scheme}://{host}"

    # ── agent registry ─────────────────────────────────────────────────────
    def exposed_names(self) -> list[str]:
        return sorted(self._agents)

    def all_exposed_names(self) -> list[str]:
        """Agents reachable from outside over *either* surface — what the
        authorization server may grant and the metadata advertises."""
        names = set(self._agents)
        a2a = getattr(self.runtime, "a2a_server", None)
        if a2a is not None:
            try:
                names.update(a2a.exposed_names())
            except Exception:  # pragma: no cover
                pass
        return sorted(names)

    def register_agent(self, name: str, runner: Any) -> None:
        """Publish ``name`` as an ``ask_<name>`` tool (idempotent)."""
        self.unregister_agent(name)
        tool_name = f"ask_{_slug(name)}"
        config = getattr(runner, "config", None)
        # The public description is the operator-written `external_access.description`
        # (what an outside caller may know) — never the system prompt, and only as a
        # fallback the roster description.
        blurb = public_description(config)
        doc = (
            f"Send a message to the Olano agent '{name}'"
            + (f". {blurb}" if blurb else "")
            + " `message` is the full request in plain language; the agent has none of your "
            "context, so include every fact it needs. `thread_id` (optional) names a "
            "conversation: reuse it to continue where you left off, omit it for a fresh one "
            "per token. The reply is the agent's answer; it may take a while when the agent "
            "uses tools, and if the agent needs a human approval on its side the call waits "
            "for that decision."
        )

        async def _ask(message: str, thread_id: str = "") -> str:
            return await self._ask(name, message, thread_id)

        _ask.__name__ = tool_name
        _ask.__doc__ = doc
        self.fastmcp.add_tool(_ask, name=tool_name, description=doc)
        self._agents[name] = runner
        self._tool_names[name] = tool_name
        self._invalidate_build()
        logger.info("MCP server: agent '%s' exposed as tool %s", name, tool_name)

    def unregister_agent(self, name: str) -> None:
        tool_name = self._tool_names.pop(name, None)
        self._agents.pop(name, None)
        if tool_name:
            with contextlib.suppress(Exception):
                self.fastmcp.remove_tool(tool_name)
            self._invalidate_build()

    def _register_list_tool(self) -> None:
        async def olano_list_agents() -> str:
            """List the Olano agents this token may talk to, with a one-line description each
            and the ask_<agent> tool that reaches them."""
            from mcp.server.auth.middleware.auth_context import get_access_token

            from olano.mcp_server.auth import token_allows_agent

            access = get_access_token()
            rows = []
            for name in self.exposed_names():
                if access is not None and not token_allows_agent(access, name):
                    continue
                cfg = getattr(self._agents[name], "config", None)
                desc = public_description(cfg) or "(no description)"
                rows.append(f"- {name} → {self._tool_names[name]}: {desc}")
            if not rows:
                return "No agents are available to this token."
            return "Agents you may talk to:\n" + "\n".join(rows)

        self.fastmcp.add_tool(olano_list_agents, name="olano_list_agents")

    # ── tool body ──────────────────────────────────────────────────────────
    async def _ask(self, name: str, message: str, thread_id: str) -> str:
        from mcp.server.auth.middleware.auth_context import get_access_token
        from mcp.server.fastmcp.exceptions import ToolError

        from olano.mcp_server.auth import token_allows_agent
        from olano.security.request_context import ORIGIN_KEY, make_origin, push_request_context

        access = get_access_token()
        if access is None:
            raise ToolError("Authentication required.")
        if not token_allows_agent(access, name):
            raise ToolError(
                f"This token is not authorized for agent '{name}'. Ask the operator for a token "
                "that includes it."
            )
        runner = self._agents.get(name) or (
            self.runtime.get_runner(name) if hasattr(self.runtime, "get_runner") else None
        )
        if runner is None:
            raise ToolError(f"Agent '{name}' is not running right now.")
        message = (message or "").strip()
        if not message:
            raise ToolError("message is empty.")
        client = access.client_id or "mcp-client"
        thread = _slug(thread_id) if (thread_id or "").strip() else "default"
        scoped_thread = f"mcp::{_slug(client)}::{thread}"
        sender = f"mcp:{client}"
        started = datetime.now(timezone.utc).isoformat()
        self._publish(
            "agent_message",
            {
                "kind": "inbound",
                "protocol": "mcp",
                "phase": "send",
                "from": sender,
                "to": name,
                "message": message,
                "thread_id": scoped_thread,
                "ts": started,
            },
        )
        self._publish("agent_activity", {"agent": name, "status": "talking", "peer": sender})
        reply = ""
        error = ""
        try:
            with push_request_context(
                source="mcp",
                channel="mcp",
                user_id=sender,
                session_id=scoped_thread,
                authenticated=True,
                is_owner=False,
                is_admin=False,
                is_group=False,
                metadata={
                    ORIGIN_KEY: make_origin(
                        "external",
                        False,
                        root_user_id=sender,
                        root_session_id=scoped_thread,
                        call_path=[sender],
                    ),
                    "mcp_client": client,
                    "mcp_subject": access.subject or "",
                },
            ):
                result = await runner.ainvoke(message, thread_id=scoped_thread)
            reply = result if isinstance(result, str) else str(result)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("MCP server: agent '%s' turn failed for %s: %s", name, sender, error)
            raise ToolError(f"Agent '{name}' could not complete the request: {exc}") from exc
        finally:
            finished = datetime.now(timezone.utc).isoformat()
            payload: dict[str, Any] = {
                "kind": "inbound",
                "protocol": "mcp",
                "phase": "reply",
                "from": sender,
                "to": name,
                "message": message,
                "thread_id": scoped_thread,
                "ts_sent": started,
                "ts_reply": finished,
                "reply": reply if not error else f"[error] {error}",
            }
            if error:
                payload["error"] = error
            self._publish("agent_message", payload)
            self._publish("agent_activity", {"agent": name, "status": "idle"})
        return reply

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            from olano.tools.talk_to import publish_activity_event

            publish_activity_event(event_type, payload)
        except Exception:  # pragma: no cover - telemetry never breaks a call
            logger.debug("MCP server: activity publish failed", exc_info=True)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def _invalidate_build(self) -> None:
        """Tool set changed → metadata scopes changed; rebuild on next request.
        The session manager is stateless, so dropping it loses nothing."""
        build, self._build = self._build, None
        if build is not None:
            build.stop.set()

    async def _ensure_build(self, origin: str) -> _Build | None:
        async with self._lock:
            settings = self.settings()
            if (
                self._build is not None
                and self._build.origin == origin.rstrip("/")
                and self._build.settings_fingerprint == settings.model_dump_json()
            ):
                return self._build
            if self._build is not None:
                self._build.stop.set()
            if self._stopped:
                return None
            build = _Build(origin, settings, self)
            self._build = build
            self._build_task = asyncio.create_task(build.serve(), name="olano_mcp_server")
            try:
                await asyncio.wait_for(build.ready.wait(), timeout=10)
            except TimeoutError:  # pragma: no cover - task group start is immediate
                logger.error("MCP server: session manager did not start")
                return None
            return build

    async def stop(self) -> None:
        self._stopped = True
        build, self._build = self._build, None
        if build is not None:
            build.stop.set()
        task, self._build_task = self._build_task, None
        if task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=5)

    # ── status ─────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        settings = self.settings()
        origin = self.resolve_origin()
        exposed = self.exposed_names()
        tokens = self.token_store.list()
        return {
            "enabled": settings.mcp.enabled,
            "endpoint": f"{origin}{MCP_PATH}" if origin else None,
            "metadata_url": f"{origin}{METADATA_ALIAS_PATH}" if origin else None,
            "public_origin_known": bool(origin),
            "https": bool(origin and origin.startswith("https://")),
            "require_https": settings.require_https,
            "serving": bool(
                settings.mcp.enabled and (origin is not None) and self._https_ok(origin, settings)
            ),
            "oauth_enabled": settings.oauth.enabled,
            "oauth_issuer": f"{origin}{OAUTH_ISSUER_PATH}"
            if origin and settings.oauth.enabled
            else None,
            "oauth_dynamic_registration": settings.oauth.dynamic_registration,
            "oauth_clients": len(self.oauth.client_store.list()),
            "exposed_agents": exposed,
            "tools": ["olano_list_agents", *(self._tool_names[n] for n in exposed)],
            "tokens": {
                "total": len(tokens),
                "active": sum(1 for t in tokens if t.active),
            },
            "jwt_issuer": settings.jwt.issuer if settings.jwt.configured else None,
        }

    @staticmethod
    def _https_ok(origin: str, settings: ExternalAccessSettings) -> bool:
        return (
            origin.startswith("https://")
            or not settings.require_https
            or _is_private_origin(origin)
        )

    # ── ASGI entry points ──────────────────────────────────────────────────
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The ``/api/mcp`` endpoint."""
        if scope["type"] != "http":  # pragma: no cover - HTTP routes only
            await _json(404, "not_found")(scope, receive, send)
            return
        settings = self.settings()
        if not settings.mcp.enabled:
            await _json(
                404,
                "mcp_server_disabled",
                hint="Enable it home-wide: Config → External access → MCP "
                "(config.yaml → external_access.mcp.enabled: true)",
            )(scope, receive, send)
            return
        origin = self.resolve_origin(scope)
        if not origin:
            await _json(503, "public_origin_unknown", hint="Set LIONCLAW_PUBLIC_URL")(
                scope, receive, send
            )
            return
        if not self._https_ok(origin, settings):
            await _json(
                403,
                "mcp_server_requires_https",
                hint="Serve the dashboard over https:// or set mcp_server.require_https: false",
            )(scope, receive, send)
            return
        build = await self._ensure_build(origin)
        if build is None:
            await _json(503, "mcp_server_starting")(scope, receive, send)
            return
        await build.app(scope, receive, send)

    async def handle_oauth(self, scope: Scope, receive: Receive, send: Send) -> None:
        """``/api/oauth/*`` and the AS metadata well-known paths."""
        settings = self.settings()
        if not settings.oauth.enabled or not (settings.mcp.enabled or settings.a2a.enabled):
            await _json(
                404,
                "oauth_server_disabled",
                hint="Turn on an inbound surface and OAuth under Config → External access",
            )(scope, receive, send)
            return
        origin = self.resolve_origin(scope)
        if not origin:
            await _json(503, "public_origin_unknown", hint="Set LIONCLAW_PUBLIC_URL")(
                scope, receive, send
            )
            return
        if not self._https_ok(origin, settings):
            await _json(403, "oauth_server_requires_https")(scope, receive, send)
            return
        build = await self._ensure_build(origin)
        if build is None or build.oauth_router is None:
            await _json(503, "oauth_server_unavailable")(scope, receive, send)
            return
        await build.oauth_router(scope, receive, send)

    def invalidate_for_a2a(self) -> None:
        """The A2A surface changed its agent set: rebuild so the advertised
        scopes (metadata, registration) follow."""
        self._invalidate_build()

    async def handle_metadata(self, scope: Scope, receive: Receive, send: Send) -> None:
        """RFC 9728 protected-resource metadata, at both well-known locations."""
        settings = self.settings()
        if not settings.mcp.enabled:
            await _json(404, "mcp_server_disabled")(scope, receive, send)
            return
        origin = self.resolve_origin(scope)
        if not origin:
            await _json(503, "public_origin_unknown")(scope, receive, send)
            return
        build = await self._ensure_build(origin)
        if build is None or build.prm_handler is None:
            await _json(503, "mcp_server_starting")(scope, receive, send)
            return
        await build.prm_handler(scope, receive, send)


class _AsgiEndpoint:
    """Adapter so Starlette mounts an ``async (scope, receive, send)`` as-is."""

    def __init__(self, fn: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self._fn = fn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._fn(scope, receive, send)


def _default_origin() -> str | None:
    try:
        from olano.core.mcp_setup import public_origin

        return public_origin()
    except Exception:  # pragma: no cover
        return None


def register_mcp_server_routes(app: Any, server: OlanoMcpServer) -> None:
    """Attach the endpoint + metadata routes to the runtime's FastAPI app.

    Call **after** every other ``/api/mcp/...`` route is registered: these are
    exact paths, so order only matters for readability, but the dashboard's
    ``/api/mcp/directory`` and OAuth callback routes must never be shadowed.
    """
    from starlette.routing import Route

    # Route() treats a bound method as a request handler and wraps it; these
    # are raw ASGI apps (they stream), so hand it callable objects instead.
    endpoint = _AsgiEndpoint(server.handle)
    metadata = _AsgiEndpoint(server.handle_metadata)
    app.router.routes.append(Route(MCP_PATH, endpoint=endpoint))
    app.router.routes.append(Route(METADATA_ALIAS_PATH, endpoint=metadata))
    app.router.routes.append(
        Route(f"/.well-known/oauth-protected-resource{MCP_PATH}", endpoint=metadata)
    )
    # Olano's own authorization server + its discovery documents. Its exact
    # endpoints only, never a prefix catch-all: the consent API under the same
    # prefix is registered after this (see oauth.ENDPOINT_PATHS).
    from olano.mcp_server.oauth import ENDPOINT_PATHS

    oauth = _AsgiEndpoint(server.handle_oauth)
    for path in (
        *ENDPOINT_PATHS,
        f"/.well-known/oauth-authorization-server{OAUTH_ISSUER_PATH}",
        f"/.well-known/openid-configuration{OAUTH_ISSUER_PATH}",
    ):
        app.router.routes.append(Route(path, endpoint=oauth))


__all__ = [
    "MCP_PATH",
    "METADATA_ALIAS_PATH",
    "OlanoMcpServer",
    "authorization_servers",
    "mcp_endpoint_path",
    "register_mcp_server_routes",
]
