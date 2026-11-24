"""Public serving + admin management routes for agent-published pages.

Registered from ``create_api`` (api.py). The public ``/pages/<slug>/…`` routes
are exempted from the dashboard bearer/session gates by the security
middleware — they are meant for external human viewers — and self-authenticate
with HTTP Basic Auth against the owner-set credential **for the slug's owning
agent** in ``olano/pages/registry.py`` (secure default: 503 until the owner
sets that agent's password, never unauthenticated serving). The ``/api/pages``
management routes (including ``GET|POST|DELETE /api/pages/{agent}/auth``) go
through the normal dashboard gates and are admin-only in multi-user mode
(``auth._ADMIN_ONLY_PREFIXES``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

# Imported at module level (not inside register_pages_routes) so FastAPI can
# resolve the handlers' postponed annotations ("Request") against this
# module's globals. The module is only imported from create_api, which has
# already verified FastAPI is installed.
from fastapi import HTTPException, Request
from pydantic import BaseModel
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from olano.pages import proxy as page_proxy
from olano.pages import registry as pages_registry
from olano.pages import releases as page_releases
from olano.pages import services as page_services
from olano.pages.spec import KIND_SERVICE, record_spec
from olano.pages.workspaces import _WS_ROOT_CACHE, agent_workspace_root

logger = logging.getLogger(__name__)

_BASIC_CHALLENGE = {"WWW-Authenticate": 'Basic realm="olano pages", charset="UTF-8"'}

# PBKDF2 verification is deliberately slow (~600k iterations), and Basic Auth
# resends the credential on EVERY asset request — so cache verdicts briefly.
# Wrong credentials are cached too (this is an unauthenticated surface: a
# client replaying the same bad password must cost one KDF, not one per
# request). Keys include the stored hash, so rotating the password immediately
# orphans every cached verdict for the old credential. At the cap the oldest
# entry is evicted — never the whole cache, or a flood of distinct wrong
# credentials would keep forcing fresh KDFs for already-seen ones.
_AUTH_CACHE: dict[str, tuple[float, bool]] = {}
_AUTH_CACHE_TTL = 300.0
_AUTH_CACHE_MAX = 256


async def _verify_basic_header(agent: str, header: str) -> bool:
    auth = pages_registry.get_page_auth(agent)
    if auth is None:
        return False
    if not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    username, sep, password = raw.partition(":")
    if not sep:
        return False
    # Key on the agent + submitted credential + stored hash so verdicts never
    # cross agents and rotating one agent's password orphans its cached verdicts.
    key = hashlib.sha256(
        (agent + "\x00" + raw + "\x00" + str(auth["password_hash"])).encode("utf-8")
    ).hexdigest()
    now = time.monotonic()
    hit = _AUTH_CACHE.get(key)
    if hit is not None and now - hit[0] < _AUTH_CACHE_TTL:
        return hit[1]
    # The KDF costs ~0.5s of pure CPU — run it off the event loop so a
    # credential probe can't stall the API and every channel adapter.
    ok = await asyncio.to_thread(pages_registry.verify_page_auth, agent, username, password)
    while len(_AUTH_CACHE) >= _AUTH_CACHE_MAX:
        _AUTH_CACHE.pop(next(iter(_AUTH_CACHE)))
    _AUTH_CACHE[key] = (now, ok)
    return ok


def _agent_workspace_root(agent: str) -> Path | None:
    """The agent's workspace root (cached; see ``pages/workspaces.py``).

    A module-level name on purpose: the route calls it through this module so
    tests can point every agent at a temp workspace.
    """
    return agent_workspace_root(agent)


__all__ = ["_WS_ROOT_CACHE", "register_pages_routes"]


def _viewer_name(header: str) -> str:
    """The Basic-Auth username of an already-verified ``Authorization`` header."""
    try:
        raw = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""
    return raw.partition(":")[0]


_STATIC_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private"}


def _status_response(
    request: Request, status: int, title: str, message: str, *, retry_after: int | None = None
) -> Response:
    headers = {"Cache-Control": "no-store"}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    if "text/html" in request.headers.get("accept", "") and request.method in ("GET", "HEAD"):
        refresh = retry_after if status == 503 else None
        return HTMLResponse(
            page_proxy.status_page(title, message, refresh=refresh),
            status_code=status,
            headers=headers,
        )
    return PlainTextResponse(f"{title}. {message}", status_code=status, headers=headers)


def _waiting_message(state: dict[str, Any] | None) -> tuple[str, str]:
    """Title and explanation for a page that is not serving yet."""
    if not state:
        return "This page is starting", "It will load automatically in a moment."
    current = state.get("state")
    detail = str(state.get("detail") or "")
    if current == page_services.BUILDING:
        return (
            "This page is being built",
            detail or "It will load automatically when the build finishes.",
        )
    if current == page_services.FAILED:
        return "This page is not running", detail or "Its server stopped. The owner has been told."
    if current == page_services.WAITING:
        return "This page is waiting to start", detail or "It will load automatically in a moment."
    if current == page_services.BACKOFF:
        return "This page is restarting", detail or "It will load automatically in a moment."
    return "This page is starting", detail or "It will load automatically in a moment."


class _ProxiedResponse(StreamingResponse):
    """A streamed upstream response whose cleanup runs whatever happens.

    Starlette may cancel a streaming response before its body generator ever
    starts (the viewer hung up), and a generator that never started never runs
    its ``finally``. Cleaning up around ``__call__`` instead returns the
    upstream connection to the pool and the request slot to the page's limit
    on every path, including cancellation.
    """

    def __init__(self, *args: Any, on_close: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._on_close()


class _BodyTooLarge(Exception):
    pass


async def _proxy_to_service(
    request: Request, record: dict[str, Any], slug: str, viewer: str
) -> Response:
    """Forward one viewer request to the page's running server."""
    import httpcore

    manager = page_services.get_manager()
    runner = manager.runner(slug) if manager is not None else None
    if manager is None or runner is None:
        title, message = _waiting_message(page_services.page_status(slug))
        return _status_response(request, 503, title, message, retry_after=3)
    if not await runner.wait_ready(timeout=15.0):
        title, message = _waiting_message(runner.state.to_dict())
        failed = runner.state.state == page_services.FAILED
        return _status_response(request, 503, title, message, retry_after=None if failed else 3)

    host = request.headers.get("host", "")
    lowered = {k.lower(): v for k, v in request.headers.items()}
    if page_proxy.cross_site_unsafe(request.method, lowered, host):
        return PlainTextResponse(
            "Refused: this request came from another site. Forms and scripts on the page "
            "itself can submit; other sites cannot.",
            status_code=403,
        )

    settings = manager.settings()
    max_body = int(settings.max_request_body_mb) * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_body:
                return PlainTextResponse(
                    f"Request body larger than {settings.max_request_body_mb} MB.", status_code=413
                )
        except ValueError:
            return PlainTextResponse("Bad Content-Length.", status_code=400)

    spec = runner.spec
    pool = runner.pool
    port = runner.state.port
    if spec is None or pool is None or not port:
        title, message = _waiting_message(runner.state.to_dict())
        return _status_response(request, 503, title, message, retry_after=3)
    target = page_proxy.upstream_target(
        request.scope.get("raw_path") or request.url.path.encode(),
        request.scope.get("query_string") or b"",
        slug=slug,
        strip_prefix=spec.strip_prefix,
    )
    if target is None:
        return PlainTextResponse("Non-canonical page URL.", status_code=400)

    semaphore = runner.inflight
    if semaphore.locked():
        return PlainTextResponse(
            "This page is busy. Try again in a moment.",
            status_code=503,
            headers={"Retry-After": "2"},
        )
    await semaphore.acquire()
    released = False

    def _release() -> None:
        nonlocal released
        if not released:
            released = True
            semaphore.release()

    has_body = (
        request.method not in ("GET", "HEAD", "OPTIONS")
        or bool(declared)
        or ("chunked" in lowered.get("transfer-encoding", "").lower())
    )

    async def _body() -> Any:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_body:
                raise _BodyTooLarge
            if chunk:
                yield chunk

    client = request.client
    headers = page_proxy.request_headers(
        request.headers.items(),
        slug=slug,
        port=port,
        viewer=viewer,
        client_ip=client.host if client else "",
        scheme=request.headers.get("x-forwarded-proto") or request.url.scheme,
        host=host,
    )
    if has_body and declared is None and not any(k == b"Transfer-Encoding" for k, _ in headers):
        headers.append((b"Transfer-Encoding", b"chunked"))
    timeout = float(settings.request_timeout_seconds)
    upstream = httpcore.Request(
        request.method,
        httpcore.URL(scheme=b"http", host=b"127.0.0.1", port=port, target=target),
        headers=headers,
        content=_body() if has_body else b"",
        extensions={"timeout": {"connect": 10.0, "read": timeout, "write": timeout, "pool": 10.0}},
    )
    try:
        response = await pool.handle_async_request(upstream)
    except _BodyTooLarge:
        _release()
        return PlainTextResponse(
            f"Request body larger than {settings.max_request_body_mb} MB.", status_code=413
        )
    except httpcore.TimeoutException:
        _release()
        return _status_response(
            request,
            504,
            "The page did not answer in time",
            f"Its server took longer than {timeout:g}s.",
        )
    except Exception as exc:
        _release()
        logger.info("page %s: upstream request failed: %s", slug, exc)
        return _status_response(
            request,
            502,
            "The page's server could not answer",
            "It may be restarting. Try again in a moment.",
            retry_after=3,
        )

    async def _close() -> None:
        with contextlib.suppress(Exception):
            await response.aclose()
        _release()

    async def _stream() -> Any:
        try:
            async for chunk in response.stream:  # type: ignore[union-attr]
                yield chunk
        except httpcore.NetworkError as exc:
            # The server died mid-answer (crash, restart). The status line is
            # already sent, so all that is left is to end the body; the viewer
            # sees a truncated response, the supervisor restarts the server.
            logger.info("page %s: upstream ended mid-response: %s", slug, exc)
        except httpcore.TimeoutException:
            logger.info("page %s: upstream stalled mid-response past %gs", slug, timeout)
        except httpcore.RemoteProtocolError as exc:
            logger.info("page %s: upstream broke the response: %s", slug, exc)

    out_headers = page_proxy.response_headers(response.headers, slug=slug, port=port)
    proxied = _ProxiedResponse(_stream(), status_code=response.status, on_close=_close)
    proxied.raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1", "replace")) for k, v in out_headers
    ]
    return proxied


def _static_base(record: dict[str, Any], root: Path, slug: str) -> Path | None:
    """The directory a static page serves from: its live release, or its folder."""
    spec = record_spec(record)
    if spec is not None and spec.build:
        return page_releases.current_release_dir(slug)
    return (root / str(record.get("directory", "."))).resolve()


class PageAuthRequest(BaseModel):
    username: str = "owner"
    password: str


def register_pages_routes(app: Any) -> None:
    """Attach the /pages/* viewer routes and /api/pages management routes."""

    # ── public viewer surface ────────────────────────────────────────────────

    @app.api_route(
        "/pages/{slug}", methods=list(page_proxy.PROXIED_METHODS), include_in_schema=False
    )
    async def page_root_redirect(slug: str, request: Request) -> Any:
        # Relative asset links inside the page only resolve under the
        # trailing-slash form, so canonicalize before serving. 307 keeps the
        # method and body, so a form posting to the bare URL still arrives.
        query = request.url.query
        return RedirectResponse(
            url=f"/pages/{slug}/" + (f"?{query}" if query else ""), status_code=307
        )

    @app.api_route("/pages/{slug}/{file_path:path}", methods=list(page_proxy.PROXIED_METHODS))
    async def serve_page(slug: str, file_path: str, request: Request) -> Any:
        """Serve one request for an agent-published page, behind HTTP Basic Auth."""
        record = pages_registry.get_page(slug)
        if record is None or not record.get("enabled"):
            raise HTTPException(status_code=404, detail="Page not found")
        agent = str(record["agent"])
        if not pages_registry.page_auth_configured(agent):
            return PlainTextResponse(
                "This page is locked: the owner has not set a viewer password "
                f"for agent '{agent}' yet (dashboard Agent → Pages, or "
                "olano pages set-password --agent " + agent + ").",
                status_code=503,
            )
        authorization = request.headers.get("Authorization", "")
        if not await _verify_basic_header(agent, authorization):
            return PlainTextResponse(
                "Authentication required",
                status_code=401,
                headers=_BASIC_CHALLENGE,
            )

        spec = record_spec(record)
        if spec is not None and spec.kind == KIND_SERVICE:
            return await _proxy_to_service(request, record, slug, _viewer_name(authorization))

        if request.method not in ("GET", "HEAD"):
            return PlainTextResponse(
                "This page serves files only.", status_code=405, headers={"Allow": "GET, HEAD"}
            )
        # to_thread keeps the cache-miss json5 parse off the event loop.
        root = await asyncio.to_thread(_agent_workspace_root, agent)
        if root is None:
            raise HTTPException(status_code=404, detail="Page not found")
        base = _static_base(record, root, slug)
        if base is None:
            title, message = _waiting_message(page_services.page_status(slug))
            return _status_response(request, 503, title, message, retry_after=3)
        if spec is None or not spec.build:
            try:
                base.relative_to(root.resolve())
            except ValueError:
                raise HTTPException(status_code=403, detail="Access denied")
        if not base.is_dir():
            raise HTTPException(status_code=404, detail="Page directory no longer exists")

        target = (base / file_path).resolve() if file_path else base
        try:
            target.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")
        if target.is_dir():
            target = target / "index.html"
        if (
            not target.is_file()
            and record.get("spa")
            and page_proxy.is_navigation(file_path, request.headers.get("accept", ""))
        ):
            # Client-side routing: a navigation to /app/settings is the app's
            # index.html; a missing /app/main.js stays a 404.
            target = base / "index.html"
        if not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        mime, _ = mimetypes.guess_type(str(target))
        return FileResponse(
            str(target), media_type=mime or "application/octet-stream", headers=_STATIC_HEADERS
        )

    # ── owner/admin management surface (admin-only in multi-user mode) ───────

    @app.get("/api/pages")
    async def list_pages_endpoint() -> dict:
        """All published pages plus per-agent viewer-credential state."""
        auth = pages_registry.list_page_auth()
        pages = pages_registry.list_pages()
        states = page_services.all_page_status()
        for page in pages:
            spec = record_spec(page)
            page["kind"] = spec.kind if spec is not None else "static"
            # What the supervisor observes right now (deployed pages only):
            # state, detail, port, restarts, last build, last error.
            page["runtime"] = states.get(page["slug"]) if spec is not None else None
        return {
            "pages": pages,
            # {agent: {configured: bool, username: str}} for every agent that has
            # a credential set. Agents absent from the map have no password.
            "auth_by_agent": {
                agent: {"configured": True, "username": rec.get("username", "")}
                for agent, rec in auth.items()
            },
        }

    @app.get("/api/pages/{agent}/auth")
    async def get_agent_pages_auth(agent: str) -> dict:
        """Whether ``agent`` has a viewer credential, and its username (no hash)."""
        auth = pages_registry.get_page_auth(agent)
        return {
            "agent": agent,
            "configured": auth is not None,
            "username": (auth or {}).get("username", "owner"),
        }

    @app.post("/api/pages/{agent}/auth")
    async def set_agent_pages_auth(agent: str, body: PageAuthRequest) -> dict:
        """Set ``agent``'s viewer credential. The plaintext is hashed and discarded."""
        try:
            pages_registry.set_page_auth(agent, body.username, body.password)
        except pages_registry.PagesError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _AUTH_CACHE.clear()
        return {"ok": True, "agent": agent, "username": body.username.strip()}

    @app.delete("/api/pages/{agent}/auth")
    async def clear_agent_pages_auth(agent: str) -> dict:
        """Remove ``agent``'s viewer credential (its pages revert to 503)."""
        removed = pages_registry.clear_page_auth(agent)
        _AUTH_CACHE.clear()
        return {"ok": True, "agent": agent, "removed": removed}

    @app.delete("/api/pages/{slug}")
    async def delete_page_endpoint(slug: str) -> dict:
        """Release any agent's slug (owner cleanup), stopping its server if any."""
        try:
            pages_registry.admin_delete_page(slug)
        except pages_registry.PagesError as e:
            raise HTTPException(status_code=404, detail=str(e))
        normalized = pages_registry.normalize_slug(slug)
        await asyncio.to_thread(page_releases.delete_releases, normalized)
        page_services.notify_page_changed(normalized)
        return {"ok": True}

    @app.post("/api/pages/{slug}/stop")
    async def stop_page_endpoint(slug: str) -> dict:
        """Stop serving any agent's page and stop its server (owner surface)."""
        try:
            record = pages_registry.admin_set_enabled(slug, False)
        except pages_registry.PagesError as e:
            raise HTTPException(status_code=404, detail=str(e))
        page_services.notify_page_changed(record["slug"])
        return {"ok": True, "page": record}

    @app.post("/api/pages/{slug}/restart")
    async def restart_page_endpoint(slug: str) -> dict:
        """Re-enable any agent's page; a deployed one rebuilds and restarts."""
        record = pages_registry.get_page(pages_registry.normalize_slug(slug))
        if record is None:
            raise HTTPException(status_code=404, detail=f"no page registered under slug '{slug}'")
        root = await asyncio.to_thread(_agent_workspace_root, str(record["agent"]))
        try:
            record = pages_registry.admin_set_enabled(slug, True, root)
        except pages_registry.PagesError as e:
            raise HTTPException(status_code=400, detail=str(e))
        page_services.notify_page_changed(record["slug"])
        return {"ok": True, "page": record}

    @app.get("/api/pages/{slug}/logs")
    async def page_logs_endpoint(slug: str, which: str = "service", lines: int = 200) -> dict:
        """The end of a deployed page's server or build log (owner surface)."""
        from olano.pages.paths import build_log_path, service_log_path

        if which not in ("service", "build"):
            raise HTTPException(status_code=400, detail="which must be 'service' or 'build'")
        record = pages_registry.get_page(pages_registry.normalize_slug(slug))
        if record is None:
            raise HTTPException(status_code=404, detail=f"no page registered under slug '{slug}'")
        root = await asyncio.to_thread(_agent_workspace_root, str(record["agent"]))
        if root is None:
            raise HTTPException(status_code=404, detail="the page's agent has no workspace")
        path = (build_log_path if which == "build" else service_log_path)(root, record["slug"])
        count = max(1, min(1000, int(lines)))
        text = await asyncio.to_thread(page_services.tail_text, path, count, 512 * 1024)
        return {"slug": record["slug"], "which": which, "lines": count, "text": text}
