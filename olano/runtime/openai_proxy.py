"""Agent-scoped public routes for the OpenAI-compatible API connector.

Registered from ``create_api`` (api.py). These reverse proxy to the per-agent
aiohttp server that ``platform:api_server`` runs (olano/platforms/api_server.py),
so an OpenAI SDK, Open WebUI or LobeChat can reach an agent at::

    https://<dashboard host>/api/openai/<agent>/v1

**Why under /api/**, and not a top-level ``/v1`` or ``/agents/<name>``: every
deployment shape already routes ``/api/*`` to this runtime (the all-in-one
Caddyfile, the host-native one, the Vercel rewrites) and nothing else. A new
top-level prefix would need all three edited *and* would never reach the boxes
already running, because the fleet updater ships images and never re-pulls the
checkout the reverse-proxy config is mounted from. ``/agents/*`` is worse
still: the dashboard SPA owns ``/agents`` and ``/agents/:name`` as client
routes, so proxying that prefix would break agent deep links.

**Why the agent is in the path**: the adapter binds one server per agent on a
port derived from the agent's name, and a request carries no other agent
identity — the OpenAI ``model`` field names a model, not an agent. The path
segment is the only thing that can route deterministically.

Security. This is a self-authenticating ingress surface (like
``/api/webhooks/ingress/`` and ``/pages/``): the security middleware skips the
dashboard bearer/session gates for it, and the adapter's own bearer check is
the authentication. That is why an agent whose connector has **no usable API
key is refused here** — the adapter allows keyless access only because it
binds loopback, and proxying would silently turn that into an open,
internet-reachable agent. Requests still pass the runtime's rate limiter.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from fastapi import Request
from starlette.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# Only what an OpenAI client legitimately sends. ``Origin`` is deliberately
# absent: the adapter 403s browser origins it does not know, and these are
# server-to-server hops — browser policy for the public URL belongs to the
# runtime's own CORS middleware. Cookies and the dashboard's own session
# header never travel either.
_FORWARD_REQUEST_HEADERS = (
    "authorization",
    "content-type",
    "accept",
    "x-olano-session-id",
    "idempotency-key",
)

_FORWARD_RESPONSE_HEADERS = (
    "content-type",
    "cache-control",
    "x-olano-session-id",
    "x-accel-buffering",
)

# read=None: an SSE stream is idle between events by design (the adapter sends
# a keepalive every 30s) and an agent turn can run for minutes. A read timeout
# here would sever exactly the long streams this exists to carry.
_PROXY_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=60.0, pool=5.0)


def _error(message: str, code: str, status: int, err_type: str = "invalid_request_error"):
    """OpenAI-shaped error envelope — these callers are OpenAI SDKs."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": code}},
    )


def _resolve_adapter(agent: str) -> Any:
    """Return the agent's live api_server adapter, or None."""
    try:
        from olano.platforms import get_platform_registry

        # ``for_agent`` rather than ``get``: a miss here is an ordinary 404,
        # not the operational warning ``get`` logs for a missing adapter.
        return get_platform_registry().for_agent(agent).get("api_server")
    except Exception as exc:  # registry not initialised (no agents booted yet)
        logger.debug("openai proxy: registry lookup failed for %r: %s", agent, exc)
        return None


def _gate(agent: str) -> tuple[Any, Optional[JSONResponse]]:
    """Resolve the adapter for *agent* and apply the exposure rules."""
    adapter = _resolve_adapter(agent)
    if adapter is None:
        # Same body whether the agent does not exist or merely lacks the
        # connector: this surface is unauthenticated, so it must not confirm
        # which agent names are real.
        return None, _error(
            f"Agent '{agent}' has no OpenAI-compatible API connector. Add the "
            "'OpenAI-compatible API' connector on the agent's Connectors tab, "
            "set an API key, and restart the agent.",
            code="connector_not_configured",
            status=404,
        )
    if not getattr(adapter, "is_connected", False):
        return None, _error(
            f"The OpenAI-compatible API for agent '{agent}' is not running. It may "
            "still be starting, or it refused to start — check the agent's logs.",
            code="connector_not_running",
            status=503,
            err_type="server_error",
        )
    if not adapter.has_usable_api_key():
        return None, _error(
            f"The OpenAI-compatible API for agent '{agent}' has no usable API key, so "
            "it is not reachable on this public URL. Set the API key field on the "
            "connector (stored in the vault as OLANO_API_KEY) and restart the "
            "agent. Direct access on the box itself still works without a key.",
            code="api_key_required",
            status=403,
        )
    return adapter, None


def _upstream_url(adapter: Any, path: str, query: str) -> str:
    host = adapter.bind_host
    # A server bound to a wildcard is reached over loopback from in-process.
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    elif ":" in host:  # bare IPv6 literal
        host = f"[{host}]"
    return f"http://{host}:{adapter.bind_port}{path}{'?' + query if query else ''}"


async def _proxy(request: Request, agent: str, path: str) -> Any:
    adapter, refusal = _gate(agent)
    if refusal is not None:
        return refusal

    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _FORWARD_REQUEST_HEADERS
    }
    url = _upstream_url(adapter, path, request.url.query)

    client = httpx.AsyncClient(timeout=_PROXY_TIMEOUT)
    try:
        upstream = await client.send(
            client.build_request(request.method, url, content=body, headers=headers),
            stream=True,
        )
    except Exception as exc:
        await client.aclose()
        logger.warning("openai proxy: upstream unreachable for agent %r: %s", agent, exc)
        return _error(
            f"Could not reach the OpenAI-compatible API for agent '{agent}'.",
            code="connector_unreachable",
            status=502,
            err_type="server_error",
        )

    async def _pump():
        # aiter_raw, never aiter_bytes: the body is forwarded byte for byte
        # with no decoding and no buffering, which is what keeps SSE live.
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            try:
                await upstream.aclose()
            finally:
                await client.aclose()

    return StreamingResponse(
        _pump(),
        status_code=upstream.status_code,
        headers={
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in _FORWARD_RESPONSE_HEADERS
        },
    )


def register_openai_proxy_routes(app: Any) -> None:
    """Mount ``/api/openai/{agent}/…`` on the runtime app."""

    @app.api_route("/api/openai/{agent}/health", methods=["GET"])
    async def openai_health(agent: str, request: Request) -> Any:
        """Liveness of one agent's OpenAI-compatible API.

        ``/health/detailed`` is deliberately NOT exposed: it reports
        process-wide runtime state and answers without authentication.
        """
        return await _proxy(request, agent, "/health")

    @app.api_route("/api/openai/{agent}/v1/{path:path}", methods=["GET", "POST", "DELETE"])
    async def openai_v1(agent: str, path: str, request: Request) -> Any:
        """Every ``/v1`` route of one agent's OpenAI-compatible API."""
        return await _proxy(request, agent, f"/v1/{path}")
