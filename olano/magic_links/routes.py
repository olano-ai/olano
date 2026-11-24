"""HTTP surface for magic links.

Registered from ``create_api`` alongside ``register_pages_routes``. Two groups:

**Public** — ``/api/connect/{token}``. Exempted from the dashboard bearer /
session gates (the whole point is that the reader has neither), and
self-authenticating on the unguessable token in the path plus the PIN, exactly
like ``/api/webhooks/ingress/{…}/{token}`` and the OAuth callbacks. They live
under ``/api/`` so the production proxies (Caddy all-in-one, Vercel) already
route them — a new top-level path would be swallowed by the SPA fallback.

**Admin** — ``/api/magic-links/links`` (the pre-rename ``/api/handshake/*``
paths stay registered as aliases). Normal dashboard gates; admin-only via
``auth._ADMIN_ONLY_PREFIXES``, which lists both prefixes.

Route-level rules that matter:

* ``GET /api/connect/{token}`` has **no side effects**. Telegram and WhatsApp
  fetch link targets server-side to build previews; if a GET consumed or
  advanced a link, every message would burn its own link before the human ever
  tapped it.
* A token matching nothing is a uniform **404** — never "wrong PIN" vs "no such
  link", which would turn the endpoint into an oracle.
* The unlock cookie is scoped to this link's own path, so one link's page
  session can never act on another's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel
from starlette.responses import HTMLResponse, JSONResponse

from olano.config.system_settings import load_magic_link_settings
from olano.magic_links import page as page_mod
from olano.magic_links import service, store

logger = logging.getLogger(__name__)

UNLOCK_COOKIE = "lc_magic_link"

# Inline CSS + JS only, nothing external, nothing framed. `connect-src 'self'`
# lets the page poll its own endpoints and nothing else.
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
_PAGE_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store, max-age=0",
    "X-Robots-Tag": "noindex, nofollow",
}


class UnlockRequest(BaseModel):
    pin: str = ""


class ActionRequest(BaseModel):
    op: str
    fields: dict[str, str] = {}


class InlineSubmitRequest(BaseModel):
    """An inline form card's submission (dashboard chat / mobile app)."""

    pin: str = ""
    fields: dict[str, str] = {}


class MintRequest(BaseModel):
    flow: str
    params: dict[str, Any] = {}
    # Free-form label for the audit trail ("dashboard", an operator's email).
    note: str = ""
    # Optional per-link TTL override (clamped by the service; None = the
    # flow's/default TTL). Setup flows like channel-connect need longer links.
    ttl_minutes: int | None = None


def _base_path(token: str) -> str:
    return f"/api/connect/{token}"


def _require_link(token: str) -> store.LinkRecord:
    """Resolve a token or 404. Expired/terminal links still resolve — the page
    tells the holder their link is spent, which is friendlier than a 404 and
    reveals nothing they didn't already have."""
    record = service.resolve_token(token)
    if record is None:
        raise HTTPException(status_code=404, detail="Not found")
    return record


def _require_unlocked(record: store.LinkRecord, request: Request) -> None:
    if not record.check_unlock(request.cookies.get(UNLOCK_COOKIE, "")):
        raise HTTPException(status_code=401, detail="locked")


def _set_unlock_cookie(response: Response, token: str, raw_unlock: str) -> None:
    from olano.runtime.auth import secure_cookies_enabled

    response.set_cookie(
        UNLOCK_COOKIE,
        raw_unlock,
        max_age=store.UNLOCK_TTL_SECONDS,
        # Scoped to this link only: the browser never sends it to another
        # magic link, the dashboard, or any other part of the API.
        path=_base_path(token),
        httponly=True,
        samesite="lax",
        secure=secure_cookies_enabled(),
    )


def register_magic_link_routes(app: Any) -> None:
    """Attach /api/connect/* (public) and /api/magic-links/* (admin) routes."""

    # ── public: the page ─────────────────────────────────────────────────────

    @app.get("/api/connect/{token}", include_in_schema=False)
    async def magic_link_page(token: str, request: Request) -> Any:
        """Render the magic-link page. Deliberately free of side effects."""
        if not load_magic_link_settings().enabled:
            return HTMLResponse(
                page_mod.render_notice(
                    "Magic links are turned off",
                    "This deployment has magic links disabled. Ask the owner "
                    "to enable them, or use the dashboard.",
                ),
                status_code=404,
                headers=_PAGE_HEADERS,
            )
        record = _require_link(token)
        unlocked = record.check_unlock(request.cookies.get(UNLOCK_COOKIE, ""))
        # A spent link needs no PIN to explain itself — the holder had the token.
        show_state = unlocked or not record.serves_actions()
        state = service.link_state(record) if show_state else None
        return HTMLResponse(
            page_mod.render_page(
                base_path=_base_path(token),
                requested_by=record.requested_by,
                state=state,
                locked=not show_state,
            ),
            headers=_PAGE_HEADERS,
        )

    @app.post("/api/connect/{token}/unlock", include_in_schema=False)
    async def magic_link_unlock(token: str, body: UnlockRequest) -> Any:
        record = _require_link(token)
        ok, raw_unlock, error = service.unlock_link(record, body.pin)
        if not ok:
            return JSONResponse(
                status_code=403,
                content={"unlocked": False, "detail": error or "Incorrect PIN."},
            )
        fresh = store.get(record.id) or record
        result = JSONResponse(content={"unlocked": True, "state": service.link_state(fresh)})
        if raw_unlock:
            _set_unlock_cookie(result, token, raw_unlock)
        return result

    @app.get("/api/connect/{token}/state", include_in_schema=False)
    async def magic_link_state(token: str, request: Request) -> Any:
        record = _require_link(token)
        # A spent link (done/error/expired/revoked) serves its own outcome to
        # whoever holds the token, PIN-or-not — the same rule the GET page uses.
        # This is load-bearing: when the background watcher finishes a redirect
        # flow before the page's next poll, `finish()` has already cleared the
        # unlock session, and requiring it here would 401 the page into a
        # perpetual "waiting…" over a sign-in that actually succeeded.
        if record.serves_actions():
            _require_unlocked(record, request)
        return JSONResponse(
            content=service.link_state(record), headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/connect/{token}/action", include_in_schema=False)
    async def magic_link_action(token: str, body: ActionRequest, request: Request) -> Any:
        record = _require_link(token)
        _require_unlocked(record, request)

        # owner-login is the one flow whose completion is a browser session, not
        # a stored token — so it can't go through the generic action path (which
        # returns a state dict). Route its submit through authorize(), which
        # returns a raw session token the ROUTE turns into a Set-Cookie + a
        # safe-enum redirect. The token lives only in that header — never in the
        # link store, the JSON body, the audit, or the chat.
        if record.flow == "owner-login" and body.op in ("submit", "start"):
            return await _authorize_owner_login(record, body.fields)

        try:
            state = await service.act_on_link(record, body.op, body.fields)
        except service.MagicLinkError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        except Exception:  # an adapter blew up — say so without leaking internals
            logger.exception("magic-link: action %s failed for %s", body.op, record.id)
            return JSONResponse(
                status_code=500,
                content={"detail": "Something went wrong on our side. Ask for a new link."},
            )
        return JSONResponse(content=state, headers={"Cache-Control": "no-store"})

    async def _authorize_owner_login(record: store.LinkRecord, fields: dict[str, str]) -> Any:
        from olano.magic_links import owner_login
        from olano.runtime import auth as auth_layer

        try:
            # authorize() is blocking on two counts — the password KDF and
            # set_binding's interprocess deployment lock, whose flock spin can
            # hold for up to 10s — so it never runs on the event loop. The
            # cookie is issued back here, where the response object lives.
            result = await asyncio.to_thread(owner_login.authorize, record, fields or {})
        except Exception:  # never leak internals from the sign-in path
            logger.exception("owner-login: authorize failed for %s", record.id)
            return JSONResponse(
                status_code=500,
                content={"detail": "Something failed on our side. Ask for a new link."},
            )
        if not result.ok:
            return JSONResponse(status_code=result.status_code, content={"detail": result.error})
        payload = {"status": "done", "redirect": result.redirect, "result": result.message}
        response = JSONResponse(content=payload, headers={"Cache-Control": "no-store"})
        auth_layer.issue_session_cookie(response, result.raw_token, max_age=result.max_age)
        return response

    # ── admin: mint / list / revoke ──────────────────────────────────────────

    @app.get("/api/magic-links/links")
    @app.get("/api/handshake/links", include_in_schema=False)  # legacy alias
    async def list_magic_links(include_terminal: bool = True) -> dict:
        """Recent magic links (never a token, PIN, or secret value)."""
        from olano.runtime import auth as auth_layer

        auth_layer.assert_admin()
        ok, reason = service.magic_links_available()
        return {
            "available": ok,
            "reason": reason,
            "links": service.list_links(include_terminal=include_terminal),
        }

    @app.get("/api/magic-links/flows")
    @app.get("/api/handshake/flows", include_in_schema=False)  # legacy alias
    async def list_magic_link_flows() -> dict:
        """The flows this deployment can mint links for (dashboard picker).

        The ``channel-connect`` entry additionally carries ``platforms`` — the
        live connector registry — so the dashboard's platform picker always
        matches what the page can actually offer.
        """
        from olano.magic_links import flows as flow_registry
        from olano.platforms import channel_setup
        from olano.runtime import auth as auth_layer

        auth_layer.assert_admin()
        out: list[dict[str, Any]] = []
        for flow in flow_registry.all_flows():
            entry: dict[str, Any] = {
                "id": flow.id,
                "label": flow.label,
                "kind": flow.kind,
                "instructions": getattr(flow, "instructions", ""),
            }
            if flow.id == "channel-connect":
                entry["platforms"] = [c.public() for c in channel_setup.all_connectors()]
            out.append(entry)
        return {"flows": out}

    @app.post("/api/magic-links/links")
    @app.post("/api/handshake/links", include_in_schema=False)  # legacy alias
    async def mint_magic_link(body: MintRequest, request: Request) -> dict:
        """Mint a link from the dashboard. Admin-only.

        Both ``/api/magic-links`` and the legacy ``/api/handshake`` prefix are
        in ``auth._ADMIN_ONLY_PREFIXES``, so a non-admin is already 403'd by
        ``access_gate`` before this runs — a
        per-agent-owner branch here would be dead code. ``agent`` in ``params``
        only scopes *which* agent the resulting token is stored for; the mint
        itself stays admin-gated (an admin passes ``assert_agent_admin`` for any
        agent regardless). Chat-side mints, where a non-admin owner legitimately
        acts, go through the agent tool / ``/olano connect`` — not this route."""
        from olano.runtime import auth as auth_layer

        auth_layer.assert_admin()
        agent = str((body.params or {}).get("agent") or "").strip()
        user = auth_layer.current_user()
        actor = getattr(user, "email", "") or "dashboard"
        try:
            minted = service.mint_link(
                body.flow,
                params=body.params,
                requested_by={"channel": "dashboard", "username": actor, "agent": agent or None},
                notify={},  # the dashboard watches over SSE; no chat to push to
                minted_by=f"dashboard:{actor}",
                ttl_minutes=body.ttl_minutes,
            )
        except service.MagicLinkError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        from olano.magic_links import watch as watch_mod

        watch_mod.spawn(minted.id)
        return minted.public()

    # ── admin: inline forms (the chat's secret card) ─────────────────────────
    #
    # The dashboard chat and the mobile app render a `secret-entry` /
    # `provider-key` link as a form card under the agent's reply instead of a
    # link to open: the person is already signed in on this origin, so the
    # new-tab-plus-PIN detour buys nothing there. These two routes are that
    # card's backend. They drive the SAME link record and the same flow
    # `submit` as the public page (one code path, one audit trail, the same
    # single-use semantics and the same completion notice in the chat); they
    # only replace the page's unlock cookie with the dashboard session plus the
    # PIN the tool result carried. The value still never becomes a chat message.

    @app.get("/api/magic-links/links/{link_id}/form")
    async def inline_magic_link_form(link_id: str) -> dict:
        """The form an inline card renders: title, instructions, fields, status."""
        from olano.runtime import auth as auth_layer

        auth_layer.assert_admin()
        record = store.get(link_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown magic link")
        try:
            return service.inline_form_state(record)
        except service.MagicLinkError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/magic-links/links/{link_id}/submit")
    async def submit_inline_magic_link(link_id: str, body: InlineSubmitRequest) -> Any:
        """Submit an inline card. PIN-checked when the policy issued one."""
        from olano.runtime import auth as auth_layer

        auth_layer.assert_admin()
        record = store.get(link_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown magic link")
        try:
            state = await service.submit_inline_form(record, body.pin, body.fields)
        except service.InlinePinRefused as exc:
            return JSONResponse(status_code=403, content={"detail": str(exc)})
        except service.MagicLinkError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        except Exception:  # an adapter blew up: say so without leaking internals
            logger.exception("magic-link: inline submit failed for %s", link_id)
            return JSONResponse(
                status_code=500,
                content={"detail": "Something went wrong on our side. Ask for a new link."},
            )
        return JSONResponse(content=state, headers={"Cache-Control": "no-store"})

    @app.delete("/api/magic-links/links/{link_id}")
    @app.delete("/api/handshake/links/{link_id}", include_in_schema=False)  # legacy alias
    async def revoke_magic_link(link_id: str) -> dict:
        from olano.runtime import auth as auth_layer

        auth_layer.assert_admin()
        if not service.revoke_link(link_id):
            raise HTTPException(status_code=404, detail="Unknown magic link")
        return {"ok": True, "id": link_id}

    # ── admin: chat-account bindings (owner-login) ───────────────────────────

    @app.get("/api/magic-links/bindings")
    @app.get("/api/handshake/bindings", include_in_schema=False)  # legacy alias
    async def list_owner_bindings() -> dict:
        """Chat identities linked to dashboard accounts. Carries no secrets."""
        from olano.magic_links import owner_login
        from olano.runtime import auth as auth_layer

        auth_layer.assert_admin()
        return {"bindings": owner_login.list_bindings()}

    @app.delete("/api/magic-links/bindings/{key:path}")
    @app.delete("/api/handshake/bindings/{key:path}", include_in_schema=False)  # legacy alias
    async def revoke_owner_binding(key: str) -> dict:
        """Unlink a chat identity — its next dashboard link must re-prove the
        account (password again)."""
        from olano.magic_links import owner_login
        from olano.runtime import auth as auth_layer

        auth_layer.assert_admin()
        if not owner_login.delete_binding(key):
            raise HTTPException(status_code=404, detail="Unknown binding")
        return {"ok": True, "key": key}
