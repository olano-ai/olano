"""Inbound A2A server — each exposed agent as an Agent2Agent endpoint.

The mirror image of :mod:`olano.a2a.client`: where that module lets an Olano
agent call an agent somewhere else, this one lets an agent somewhere else call
an Olano agent, over the same open protocol. Built on the ``a2a-sdk`` server
pieces — ``create_jsonrpc_routes`` + ``DefaultRequestHandler`` implement the
JSON-RPC methods, task lifecycle and Agent Card serialization — with Olano
supplying an :class:`AgentExecutor` that runs the agent turn, the task store
(:mod:`olano.a2a.task_store`: SQLite, owner-scoped, swept to
``external_access.a2a_task_retention_days`` — the SDK's in-memory store never
evicts and forgets every task id on restart), and the same authentication,
request-context posture and audit trail the MCP endpoint uses:

* per agent: ``GET  /api/a2a/agents/<name>/.well-known/agent-card.json`` (public
  card: the operator-written ``external_access.description``, one ``chat``
  skill, declared auth schemes) and ``POST /api/a2a/agents/<name>`` (JSON-RPC:
  ``SendMessage``, ``GetTask``, ``CancelTask`` …);
* auth: the shared bearer-token verifier (operator-minted tokens, tokens issued
  by Olano's own OAuth server, optional external JWTs) through the ``mcp`` SDK's
  bearer middleware, then a per-agent scope check (``agent:<name>`` /
  ``agents:*``) — 401 with ``WWW-Authenticate`` when missing, 403
  ``insufficient_scope`` when the token names other agents;
* the turn runs ``AgentRunner.ainvoke`` on an ``a2a::<client>::<context>``
  thread as ``source="a2a"``, non-owner, non-interactive origin, so HITL-gated
  actions defer to the Approvals inbox; frames ``kind: "inbound"``,
  ``from: "a2a:<client>"``.

Same gates as the MCP endpoint before anything else: the home-wide
``external_access.a2a`` switch, a known public origin, HTTPS, and a ``Host``
header that matches the public URL (DNS-rebinding).
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

A2A_PREFIX = "/api/a2a/agents"
CARD_SUFFIX = "/.well-known/agent-card.json"
METADATA_SUFFIX = "/.well-known/oauth-protected-resource"
_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def a2a_agent_url(origin: str, name: str) -> str:
    return f"{origin.rstrip('/')}{A2A_PREFIX}/{name}"


def a2a_metadata_url(origin: str, name: str) -> str:
    """An agent's RFC 9728 protected-resource metadata, under its own path so
    every deployment's ``/api/*`` proxy rule reaches it (the RFC-derived
    ``/.well-known/oauth-protected-resource/api/a2a/agents/<name>`` is served
    too). What the endpoint's 401 and 403 ``WWW-Authenticate`` point at."""
    return f"{a2a_agent_url(origin, name)}{METADATA_SUFFIX}"


def _display_name(config: Any, name: str) -> str:
    return str(getattr(config, "display_name", "") or "").strip() or name


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("_", (value or "").strip()).strip("_")
    return slug[:64] or "x"


def _json(status: int, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


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


def _public_description(config: Any) -> str:
    ext = getattr(config, "external_access", None)
    text = str(getattr(ext, "description", "") or "").strip()
    return text or str(getattr(config, "description", "") or "").strip()


# ── Executor: one A2A task = one agent turn ────────────────────────────────────


class OlanoAgentExecutor:
    """``a2a.server.agent_execution.AgentExecutor`` over an ``AgentRunner``."""

    def __init__(self, name: str, runner: Any, server: OlanoA2AServer) -> None:
        self.name = name
        self.runner = runner
        self.server = server

    async def execute(self, context: Any, event_queue: Any) -> None:
        from a2a.server.tasks import TaskUpdater
        from a2a.types import Part, Task, TaskState, TaskStatus

        from olano.security.request_context import ORIGIN_KEY, make_origin, push_request_context

        access = (
            (context.call_context.state or {}).get("access_token") if context.call_context else None
        )
        client = str(getattr(access, "client_id", "") or "a2a-client")
        sender = f"a2a:{client}"
        text = (context.get_user_input() or "").strip()
        if not context.current_task:
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[context.message] if context.message else [],
                )
            )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not text:
            await updater.reject(updater.new_agent_message([Part(text="The message has no text.")]))
            return
        await updater.start_work()
        thread = f"a2a::{_slug(client)}::{_slug(context.context_id or context.task_id or 'x')}"
        started = datetime.now(timezone.utc).isoformat()
        self.server.publish(
            "agent_message",
            {
                "kind": "inbound",
                "protocol": "a2a",
                "phase": "send",
                "from": sender,
                "to": self.name,
                "message": text,
                "thread_id": thread,
                "task_id": context.task_id,
                "ts": started,
            },
        )
        self.server.publish(
            "agent_activity", {"agent": self.name, "status": "talking", "peer": sender}
        )
        reply = ""
        error = ""
        try:
            with push_request_context(
                source="a2a",
                channel="a2a",
                user_id=sender,
                session_id=thread,
                authenticated=True,
                is_owner=False,
                is_admin=False,
                is_group=False,
                metadata={
                    ORIGIN_KEY: make_origin(
                        "external",
                        False,
                        root_user_id=sender,
                        root_session_id=thread,
                        call_path=[sender],
                    ),
                    "a2a_client": client,
                    "a2a_subject": str(getattr(access, "subject", "") or ""),
                },
            ):
                result = await self.runner.ainvoke(text, thread_id=thread)
            reply = result if isinstance(result, str) else str(result)
            await updater.add_artifact([Part(text=reply)], name="reply")
            await updater.complete()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "A2A server: agent '%s' turn failed for %s: %s", self.name, sender, error
            )
            with contextlib.suppress(Exception):
                await updater.failed(
                    updater.new_agent_message(
                        [Part(text=f"The agent could not complete the request: {exc}")]
                    )
                )
        finally:
            payload: dict[str, Any] = {
                "kind": "inbound",
                "protocol": "a2a",
                "phase": "reply",
                "from": sender,
                "to": self.name,
                "message": text,
                "thread_id": thread,
                "task_id": context.task_id,
                "ts_sent": started,
                "ts_reply": datetime.now(timezone.utc).isoformat(),
                "reply": reply if not error else f"[error] {error}",
            }
            if error:
                payload["error"] = error
            self.server.publish("agent_message", payload)
            self.server.publish("agent_activity", {"agent": self.name, "status": "idle"})

    async def cancel(self, context: Any, event_queue: Any) -> None:
        from a2a.server.tasks import TaskUpdater

        # A running turn cannot be interrupted safely mid-tool; the task is
        # marked cancelled for the caller and the turn's result is discarded.
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


# ── Per-origin build ───────────────────────────────────────────────────────────


class _Build:
    def __init__(
        self, origin: str, settings: ExternalAccessSettings, server: OlanoA2AServer
    ) -> None:
        self.origin = origin.rstrip("/")
        self.settings = settings
        self.settings_fingerprint = settings.model_dump_json()
        self.server = server
        host = urlparse(self.origin).netloc
        bare = urlparse(self.origin).hostname or host
        self.allowed_hosts = {host, bare, "localhost", "127.0.0.1", *settings.allowed_hosts}
        self.allowed_host_prefixes = {f"{bare}:", "localhost:", "127.0.0.1:"} | {
            a[:-1] for a in settings.allowed_hosts if a.endswith(":*")
        }
        self.apps: dict[str, Any] = {}  # agent → Starlette app with the JSON-RPC route
        self.auth_apps: dict[str, Any] = {}  # agent → bearer chain in front of it
        self.metadata_apps: dict[str, Any] = {}  # agent → its RFC 9728 document

    def forget(self, name: str) -> None:
        for cache in (self.apps, self.auth_apps, self.metadata_apps):
            cache.pop(name, None)

    def auth_app(self, name: str) -> Any:
        """The bearer chain in front of one agent's JSON-RPC app.

        Per agent because the protected resource is: RFC 9728 §3.3 has a client
        discard metadata, reached through a ``WWW-Authenticate`` header, whose
        ``resource`` is not the URL it called. The 401 used to point every agent
        at the MCP endpoint's document, which names ``/api/mcp``, so a client
        that followed it found metadata for the wrong resource. The verifier is
        per agent for the same reason: an external JWT is bound to this agent's
        URL (the shared ``/api/a2a/agents`` identifier stays accepted)."""
        app = self.auth_apps.get(name)
        if app is None:
            from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
            from mcp.server.auth.middleware.bearer_auth import (
                BearerAuthBackend,
                RequireAuthMiddleware,
            )
            from pydantic import AnyHttpUrl
            from starlette.middleware.authentication import AuthenticationMiddleware

            from olano.mcp_server.auth import OlanoTokenVerifier

            verifier = OlanoTokenVerifier(
                resource_url=a2a_agent_url(self.origin, name),
                jwt_settings=self.settings.jwt,
                store=self.server.token_store,
                extra_audiences=(f"{self.origin}{A2A_PREFIX}",),
            )

            async def _dispatch(scope: Scope, receive: Receive, send: Send) -> None:
                await self.server._dispatch_rpc(self, scope, receive, send)

            chain: Any = RequireAuthMiddleware(
                _dispatch,
                required_scopes=[],
                resource_metadata_url=AnyHttpUrl(a2a_metadata_url(self.origin, name)),
            )
            chain = AuthContextMiddleware(chain)
            app = AuthenticationMiddleware(chain, backend=BearerAuthBackend(verifier))
            self.auth_apps[name] = app
        return app

    def host_ok(self, scope: Scope) -> bool:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        if not host:
            return False
        if host in self.allowed_hosts:
            return True
        return any(host.startswith(p) for p in self.allowed_host_prefixes)


class _AccessTokenContextBuilder:
    """a2a ``ServerCallContextBuilder`` that hands the executor the verified token."""

    def build(self, request: Any) -> Any:
        from a2a.server.routes import DefaultServerCallContextBuilder

        ctx = DefaultServerCallContextBuilder().build(request)
        user = request.scope.get("user")
        token = getattr(user, "access_token", None)
        if token is not None:
            ctx.state["access_token"] = token
        return ctx


# ── The server ─────────────────────────────────────────────────────────────────


class OlanoA2AServer:
    def __init__(
        self,
        runtime: Any,
        *,
        settings_loader: Callable[[], ExternalAccessSettings] = load_external_access_settings,
        origin_resolver: Callable[[], str | None] | None = None,
        token_store: Any | None = None,
        task_store: Any | None = None,
    ) -> None:
        from olano.mcp_server.tokens import get_token_store

        self.runtime = runtime
        self._settings_loader = settings_loader
        self._origin_resolver = origin_resolver or _default_origin
        self.token_store = token_store or get_token_store()
        self._agents: dict[str, Any] = {}
        self._build: _Build | None = None
        self._lock = asyncio.Lock()
        self._task_store: Any | None = task_store

    @property
    def task_store(self) -> Any:
        """The one ``TaskStore`` every exposed agent's request handler shares:
        SQLite under ``<home>/a2a/tasks.db`` (owner-scoped, so a token only ever
        sees its own tasks), swept to ``a2a_task_retention_days`` read live from
        the settings. Lazy, because the home path is not known at import."""
        if self._task_store is None:
            from olano.a2a.task_store import SqliteTaskStore

            self._task_store = SqliteTaskStore(
                retention_days=lambda: self.settings().a2a_task_retention_days
            )
        return self._task_store

    # ── config / origin ────────────────────────────────────────────────────
    def settings(self) -> ExternalAccessSettings:
        try:
            return self._settings_loader()
        except Exception:  # pragma: no cover
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

    @staticmethod
    def _https_ok(origin: str, settings: ExternalAccessSettings) -> bool:
        return (
            origin.startswith("https://")
            or not settings.require_https
            or _is_private_origin(origin)
        )

    # ── registry ───────────────────────────────────────────────────────────
    def exposed_names(self) -> list[str]:
        return sorted(self._agents)

    def register_agent(self, name: str, runner: Any) -> None:
        self._agents[name] = runner
        if self._build is not None:
            self._build.forget(name)
        logger.info("A2A server: agent '%s' exposed at %s/%s", name, A2A_PREFIX, name)

    def unregister_agent(self, name: str) -> None:
        self._agents.pop(name, None)
        if self._build is not None:
            self._build.forget(name)

    async def stop(self) -> None:
        self._build = None
        store, self._task_store = self._task_store, None
        if store is not None and hasattr(store, "close"):
            store.close()

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            from olano.tools.talk_to import publish_activity_event

            publish_activity_event(event_type, payload)
        except Exception:  # pragma: no cover
            logger.debug("A2A server: activity publish failed", exc_info=True)

    # ── card ───────────────────────────────────────────────────────────────
    def agent_card(self, name: str, origin: str) -> Any:
        from a2a.types import (
            AgentCapabilities,
            AgentCard,
            AgentInterface,
            AgentProvider,
            AgentSkill,
            AuthorizationCodeOAuthFlow,
            HTTPAuthSecurityScheme,
            OAuth2SecurityScheme,
            OAuthFlows,
            SecurityRequirement,
            SecurityScheme,
            StringList,
        )

        runner = self._agents[name]
        config = getattr(runner, "config", None)
        description = _public_description(config) or f"The Olano agent {name}."
        display = _display_name(config, name)
        settings = self.settings()
        scope = f"agent:{name}"
        schemes = {
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    description=(
                        "A bearer token issued by this deployment's operator (dashboard → "
                        "agent → External access), or an access token from the oauth2 flow."
                    ),
                )
            )
        }
        requirements = [SecurityRequirement(schemes={"bearer": StringList()})]
        if settings.oauth.enabled:
            issuer = f"{origin}/api/oauth"
            schemes["oauth2"] = SecurityScheme(
                oauth2_security_scheme=OAuth2SecurityScheme(
                    description=(
                        "OAuth 2.1 authorization code + PKCE against this deployment; an "
                        "administrator approves which agents the client may reach."
                    ),
                    flows=OAuthFlows(
                        authorization_code=AuthorizationCodeOAuthFlow(
                            authorization_url=f"{issuer}/authorize",
                            token_url=f"{issuer}/token",
                            refresh_url=f"{issuer}/token",
                            scopes={scope: f"Send messages to the agent {name}"},
                            pkce_required=True,
                        )
                    ),
                    oauth2_metadata_url=f"{issuer}/.well-known/oauth-authorization-server",
                )
            )
            requirements.append(SecurityRequirement(schemes={"oauth2": StringList(list=[scope])}))
        return AgentCard(
            name=display,
            description=description,
            version="1.0",
            provider=AgentProvider(organization="Olano", url=origin),
            capabilities=AgentCapabilities(streaming=False),
            supported_interfaces=[
                AgentInterface(
                    url=a2a_agent_url(origin, name),
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                )
            ],
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            skills=[
                AgentSkill(
                    id="chat",
                    name=f"Ask {display}",
                    description=description,
                    tags=["chat", "olano"],
                    examples=[
                        "Describe what you need in plain language, with every fact the agent needs."
                    ],
                )
            ],
            security_schemes=schemes,
            security_requirements=requirements,
        )

    def status(self) -> dict[str, Any]:
        settings = self.settings()
        origin = self.resolve_origin()
        return {
            "enabled": settings.a2a.enabled,
            "endpoint_template": f"{origin}{A2A_PREFIX}/<agent>" if origin else None,
            "public_origin_known": bool(origin),
            "https": bool(origin and origin.startswith("https://")),
            "require_https": settings.require_https,
            "serving": bool(
                settings.a2a.enabled and origin is not None and self._https_ok(origin, settings)
            ),
            "exposed_agents": self.exposed_names(),
            "cards": {n: f"{origin}{A2A_PREFIX}/{n}{CARD_SUFFIX}" for n in self.exposed_names()}
            if origin
            else {},
        }

    # ── ASGI ───────────────────────────────────────────────────────────────
    async def _ensure_build(self, origin: str) -> _Build:
        async with self._lock:
            settings = self.settings()
            stale = (
                self._build is None
                or self._build.origin != origin.rstrip("/")
                or self._build.settings_fingerprint != settings.model_dump_json()
            )
            build = self._build
            if stale or build is None:
                build = self._build = _Build(origin, settings, self)
            return build

    def _agent_app(self, build: _Build, name: str) -> Any:
        app = build.apps.get(name)
        if app is None:
            from a2a.server.request_handlers import DefaultRequestHandler
            from a2a.server.routes import create_jsonrpc_routes
            from starlette.applications import Starlette

            card = self.agent_card(name, build.origin)
            handler = DefaultRequestHandler(
                agent_executor=OlanoAgentExecutor(name, self._agents[name], self),  # type: ignore[arg-type]
                task_store=self.task_store,
                agent_card=card,
            )
            app = Starlette(
                routes=create_jsonrpc_routes(
                    handler,
                    "/",
                    context_builder=_AccessTokenContextBuilder(),  # type: ignore[arg-type]
                )
            )
            build.apps[name] = app
        return app

    def _metadata_app(self, build: _Build, name: str) -> Any:
        """The agent's RFC 9728 document: ``resource`` is the agent's own URL,
        the issuers are the ones the MCP endpoint advertises, and the one scope
        is the one the card's ``oauth2`` flow asks for."""
        app = build.metadata_apps.get(name)
        if app is None:
            from mcp.server.auth.routes import create_protected_resource_routes
            from pydantic import AnyHttpUrl

            from olano.mcp_server.server import authorization_servers

            config = getattr(self._agents[name], "config", None)
            (route,) = create_protected_resource_routes(
                resource_url=AnyHttpUrl(a2a_agent_url(build.origin, name)),
                authorization_servers=[
                    AnyHttpUrl(i) for i in authorization_servers(build.origin, build.settings)
                ],
                scopes_supported=[f"agent:{name}"],
                resource_name=f"Olano agent {_display_name(config, name)}",
            )
            # Route.app is the ASGI-wrapped handler, CORS included.
            app = build.metadata_apps[name] = route.app
        return app

    async def _dispatch_rpc(
        self, build: _Build, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """After auth: per-agent scope check, then the agent's JSON-RPC app."""
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        from olano.mcp_server.auth import token_allows_agent

        name = scope.get("state", {}).get("olano_a2a_agent") or scope.get("olano_a2a_agent")
        user = scope.get("user")
        if not isinstance(user, AuthenticatedUser) or not name:
            await _json(401, "authentication required")(scope, receive, send)
            return
        if not token_allows_agent(user.access_token, name):
            headers = {
                "WWW-Authenticate": (
                    f'Bearer error="insufficient_scope", scope="agent:{name}", '
                    f'error_description="this token is not authorized for agent {name}", '
                    f'resource_metadata="{a2a_metadata_url(build.origin, name)}"'
                )
            }
            await JSONResponse(
                status_code=403,
                content={"detail": "insufficient_scope", "agent": name},
                headers=headers,
            )(scope, receive, send)
            return
        app = self._agent_app(build, name)
        # Route inside the per-agent app is "/": present the request as such.
        inner = dict(scope)
        inner["path"] = "/"
        inner["raw_path"] = b"/"
        inner["root_path"] = ""
        await app(inner, receive, send)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """``/api/a2a/agents/<name>`` and everything under it."""
        path = str(scope.get("path") or "")
        rest = path[len(A2A_PREFIX) :] if path.startswith(A2A_PREFIX) else ""
        await self._handle_agent(scope, receive, send, rest)

    async def handle_metadata(self, scope: Scope, receive: Receive, send: Send) -> None:
        """RFC 9728 metadata at its RFC-derived location,
        ``/.well-known/oauth-protected-resource/api/a2a/agents/<name>``."""
        path = str(scope.get("path") or "")
        name = path[len(METADATA_SUFFIX + A2A_PREFIX) :].strip("/")
        if not name or "/" in name:
            await _json(404, "not_found")(scope, receive, send)
            return
        await self._handle_agent(scope, receive, send, f"/{name}{METADATA_SUFFIX}")

    async def _handle_agent(self, scope: Scope, receive: Receive, send: Send, rest: str) -> None:
        """``rest`` is the path below ``A2A_PREFIX``: ``/<name>[<tail>]``."""
        if scope["type"] != "http":  # pragma: no cover
            await _json(404, "not_found")(scope, receive, send)
            return
        settings = self.settings()
        if not settings.a2a.enabled:
            await _json(
                404,
                "a2a_server_disabled",
                hint="Enable it home-wide: Config → External access → A2A "
                "(config.yaml → external_access.a2a.enabled: true)",
            )(scope, receive, send)
            return
        parts = [p for p in rest.split("/") if p]
        if not parts:
            await _json(404, "not_found")(scope, receive, send)
            return
        name = parts[0]
        tail = "/" + "/".join(parts[1:]) if len(parts) > 1 else ""
        if name not in self._agents:
            await _json(404, "agent_not_exposed", agent=name)(scope, receive, send)
            return
        origin = self.resolve_origin(scope)
        if not origin:
            await _json(503, "public_origin_unknown", hint="Set LIONCLAW_PUBLIC_URL")(
                scope, receive, send
            )
            return
        if not self._https_ok(origin, settings):
            await _json(403, "a2a_server_requires_https")(scope, receive, send)
            return
        build = await self._ensure_build(origin)
        if not build.host_ok(scope):
            await _json(421, "Invalid Host header")(scope, receive, send)
            return
        method = scope.get("method", "GET")
        if tail == CARD_SUFFIX:
            if method not in ("GET", "HEAD"):
                await _json(405, "method_not_allowed")(scope, receive, send)
                return
            from a2a.server.routes.agent_card_routes import agent_card_to_dict

            await JSONResponse(agent_card_to_dict(self.agent_card(name, origin)))(
                scope, receive, send
            )
            return
        if tail == METADATA_SUFFIX:
            if method not in ("GET", "HEAD", "OPTIONS"):
                await _json(405, "method_not_allowed")(scope, receive, send)
                return
            await self._metadata_app(build, name)(scope, receive, send)
            return
        if tail == "":
            if method != "POST":
                await _json(405, "method_not_allowed", hint="POST JSON-RPC here")(
                    scope, receive, send
                )
                return
            scope.setdefault("state", {})["olano_a2a_agent"] = name
            await build.auth_app(name)(scope, receive, send)
            return
        await _json(404, "not_found")(scope, receive, send)


class _AsgiEndpoint:
    def __init__(self, fn: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self._fn = fn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._fn(scope, receive, send)


def register_a2a_server_routes(app: Any, server: OlanoA2AServer) -> None:
    from starlette.routing import Route

    endpoint = _AsgiEndpoint(server.handle)
    app.router.routes.append(Route(A2A_PREFIX + "/{name}", endpoint=endpoint))
    app.router.routes.append(Route(A2A_PREFIX + "/{name}/{rest:path}", endpoint=endpoint))
    app.router.routes.append(
        Route(
            METADATA_SUFFIX + A2A_PREFIX + "/{name}",
            endpoint=_AsgiEndpoint(server.handle_metadata),
        )
    )


def _default_origin() -> str | None:
    try:
        from olano.core.mcp_setup import public_origin

        return public_origin()
    except Exception:  # pragma: no cover
        return None


__all__ = [
    "A2A_PREFIX",
    "CARD_SUFFIX",
    "METADATA_SUFFIX",
    "OlanoA2AServer",
    "OlanoAgentExecutor",
    "a2a_agent_url",
    "a2a_metadata_url",
    "register_a2a_server_routes",
]
