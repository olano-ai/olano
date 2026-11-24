"""Central MCP OAuth relay — the domain-side half of the domain-less OAuth flow.

Deploy this once on a host you own with a real HTTPS certificate (e.g.
``https://auth.olano.ai`` behind Caddy — see ``Caddyfile``). Register its single
callback URL — ``https://<relay-host>/mcp/callback`` — as the OAuth redirect URI
with any provider that needs pre-registration (HubSpot, etc.). Every olano
deployment then points ``LIONCLAW_MCP_RELAY_URL`` at this host.

Flow
----
1. A droplet starts an OAuth flow and ``POST /mcp/register`` s its opaque
   ``state`` (HMAC-signed with the shared secret).
2. The user opens the provider sign-in link on any device; the provider redirects
   the browser to ``GET /mcp/callback?code=…&state=…`` here.
3. The relay stores ``code`` under ``state`` (only if that state was registered).
4. The droplet polls ``GET /mcp/pending?state=…`` (HMAC-signed) and receives the
   ``code`` **once**, then does the token exchange itself.

Security / privacy
------------------
- The relay only ever holds the opaque ``state`` and the short-lived ``code`` —
  **never** access/refresh tokens or account identities. The token exchange
  happens on the droplet. So customer accounts never touch this service.
- ``/register`` and ``/pending`` require a valid HMAC signature + fresh timestamp
  (replay window ``SIGNATURE_TTL``). ``/callback`` is public (providers can't
  sign) but only accepts states that were registered, and a forged ``code`` is
  useless — it simply fails the droplet's token exchange.
- Entries expire (``STATE_TTL``) and the store is size-capped to bound abuse.

This module intentionally has **no olano imports** so it can be deployed
standalone (copy this folder, ``pip install fastapi uvicorn``). The HMAC scheme
mirrors ``olano/core/mcp_relay.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

# Shared secret with every droplet. REQUIRED — refuse to run open.
RELAY_SECRET = os.environ.get("LIONCLAW_MCP_RELAY_SECRET", "").strip()

SIGNATURE_TTL = 300.0  # max age (s) of a signed request's timestamp
STATE_TTL = 900.0  # how long a registered/pending state lives (s)
MAX_STATES = 10_000  # hard cap on tracked states (abuse backstop)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if not RELAY_SECRET:
        raise RuntimeError(
            "LIONCLAW_MCP_RELAY_SECRET is not set — refusing to start an unauthenticated relay."
        )
    yield


app = FastAPI(title="Olano MCP OAuth Relay", lifespan=_lifespan)


@dataclass
class _Pending:
    instance_id: str
    created_at: float
    code: str | None = None
    error: str | None = None


_STATES: dict[str, _Pending] = {}
# NOTE: in-memory store — fine for a single relay process. For HA, back this with
# Redis (SETEX by state) and keep the same endpoints.


def _sign(*parts: str) -> str:
    msg = "\n".join(parts).encode("utf-8")
    return hmac.new(RELAY_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _valid_sig(sig: str, ts: str, *parts: str) -> bool:
    try:
        if abs(time.time() - float(ts)) > SIGNATURE_TTL:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(_sign(*parts), sig)


def _prune(now: float) -> None:
    stale = [s for s, p in _STATES.items() if now - p.created_at > STATE_TTL]
    for s in stale:
        _STATES.pop(s, None)


@app.post("/mcp/register")
async def register(request: Request) -> JSONResponse:
    body = await request.json()
    instance_id = str(body.get("instance_id", ""))
    state = str(body.get("state", ""))
    ts = str(body.get("ts", ""))
    sig = str(body.get("sig", ""))
    if not state or not _valid_sig(sig, ts, "register", instance_id, state, ts):
        raise HTTPException(status_code=401, detail="bad signature")
    now = time.time()
    _prune(now)
    if len(_STATES) >= MAX_STATES:
        raise HTTPException(status_code=503, detail="relay busy; try again")
    _STATES[state] = _Pending(instance_id=instance_id, created_at=now)
    return JSONResponse({"ok": True})


@app.get("/mcp/pending")
async def pending(instance_id: str, state: str, ts: str, sig: str) -> JSONResponse:
    if not _valid_sig(sig, ts, "pending", instance_id, state, ts):
        raise HTTPException(status_code=401, detail="bad signature")
    entry = _STATES.get(state)
    if entry is None:
        return JSONResponse({"status": "unknown"})
    if entry.error is not None:
        _STATES.pop(state, None)
        return JSONResponse({"status": "error", "error": entry.error})
    if entry.code is not None:
        _STATES.pop(state, None)  # one-time delivery
        return JSONResponse({"status": "ready", "code": entry.code})
    return JSONResponse({"status": "pending"})


@app.get("/mcp/callback")
async def callback(request: Request) -> HTMLResponse:
    params = request.query_params
    state = params.get("state") or ""
    code = params.get("code")
    error = params.get("error_description") or params.get("error")
    entry = _STATES.get(state)
    if entry is not None:
        if error:
            entry.error = error
        elif code:
            entry.code = code
    ok = entry is not None and error is None and bool(code)
    title = "Authorization received" if ok else "Authorization problem"
    detail = (
        "You can close this tab and return to your assistant."
        if ok
        else "This link has expired or was already used. Ask your assistant to try again."
    )
    accent = "#48d389" if ok else "#f46363"
    html = (
        f'<!doctype html><html><head><meta charset="utf-8"><title>Olano · MCP OAuth</title>'
        "<style>body{margin:0;min-height:100vh;display:flex;align-items:center;"
        "justify-content:center;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#0b0d12;color:#e8eaed}.card{max-width:420px;padding:32px;border-radius:14px;"
        "background:#13161d;border:1px solid rgba(255,255,255,.08);text-align:center}"
        f".dot{{width:10px;height:10px;border-radius:50%;background:{accent};display:inline-block;"
        "margin-right:8px}h1{font-size:18px;margin:0 0 12px}p{font-size:14px;color:#a9b0bd;margin:0}"
        f'</style></head><body><div class="card"><h1><span class="dot"></span>{title}</h1>'
        f"<p>{detail}</p></div></body></html>"
    )
    return HTMLResponse(html)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {"ok": True, "pending": len(_STATES)}
