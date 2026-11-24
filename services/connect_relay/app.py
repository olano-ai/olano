"""Reference official-connector auth relay — the ``/connect/*`` sibling service.

The domain-side half of Olano's **official connector** flow (box client:
``olano/core/connect_relay.py``). Where the MCP relay
(``services/mcp_oauth_relay``) shuttles authorization *codes* and the box does
its own exchange, this service performs the **code exchange and every refresh
here**, so the OAuth client secret never reaches the boxes that use it. Tokens
still land only on each box; what rests here (in memory, ≤``STATE_TTL``) is
the exchanged bundle awaiting one-time pickup.

Deploy it standalone behind the same HTTPS front as the MCP relay (one Caddy,
two upstream paths — boxes default their connect relay URL to the MCP relay
host) or on its own host via ``LIONCLAW_CONNECT_RELAY_URL``. Register
``https://<relay-host>/connect/callback`` as the redirect URI on each OAuth
app whose client you configure below.

Configuration (env)
-------------------
- ``LIONCLAW_MCP_RELAY_SECRET`` — REQUIRED. The same shared HMAC secret the
  boxes sign with (one instance identity signs both relay flows; only the
  action names differ).
- ``CONNECT_RELAY_PROVIDERS`` — REQUIRED to be useful. A JSON object keyed by
  provider id; each entry carries ``client_id``, ``client_secret``,
  ``token_url`` (may contain ``{param}`` placeholders), ``client_auth``,
  ``pkce``, ``token_headers`` and ``params`` — see the README for a complete
  Microsoft 365 example. ``client_auth`` is ``post`` (secret in the body) or
  ``basic``;
  ``token_headers`` adds exchange/refresh headers (e.g. Reddit's User-Agent);
  ``params`` lists the allowed token-URL template fields with their defaults —
  **unknown params from a box are refused**, and the token URL comes only from
  this config, never from a box.

Signed tuples (frozen contract — ``tests/fixtures/relay/connect_signing_golden.json``)::

    connect_register("connect_register", instance_id, state, ts)
    connect_pending("connect_pending", instance_id, state, ts)
    connect_refresh("connect_refresh", instance_id, sha256_hex(refresh_token), ts)

This module intentionally has **no olano imports** so it can be deployed
standalone (copy this folder, ``pip install fastapi uvicorn httpx``). The
multi-tenant production edition (Postgres registry, Ed25519 per-tenant keys,
AES-GCM sealed secrets/bundles) lives in ``olano-ai/cloud``
``services/mcp_oauth_relay/connect_*.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

# Shared secret with every box. REQUIRED — refuse to run open.
RELAY_SECRET = os.environ.get("LIONCLAW_MCP_RELAY_SECRET", "").strip()

SIGNATURE_TTL = 300.0  # max age (s) of a signed request's timestamp
STATE_TTL = 900.0  # how long a registered/pending state lives (s)
MAX_STATES = 10_000  # hard cap on tracked states (abuse backstop)
EXCHANGE_TIMEOUT = 30.0  # deadline per token-endpoint POST

# Same conservative charset the box applies to connect-time params — a hostile
# value can't rewrite the URL a template formats it into.
_PARAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


@dataclass(frozen=True)
class _Provider:
    provider_id: str
    client_id: str
    client_secret: str
    token_url: str  # may contain {param} placeholders
    client_auth: str = "post"  # "post" | "basic"
    pkce: bool = False
    token_headers: dict[str, str] = field(default_factory=dict)
    allowed_params: dict[str, str] = field(default_factory=dict)  # name -> default


def _load_providers(raw: str) -> dict[str, _Provider]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError("CONNECT_RELAY_PROVIDERS is not valid JSON") from None
    providers: dict[str, _Provider] = {}
    for provider_id, entry in (data or {}).items():
        if not isinstance(entry, dict):
            continue
        providers[str(provider_id)] = _Provider(
            provider_id=str(provider_id),
            client_id=str(entry.get("client_id", "")),
            client_secret=str(entry.get("client_secret", "")),
            token_url=str(entry.get("token_url", "")),
            client_auth=str(entry.get("client_auth", "post")),
            pkce=bool(entry.get("pkce", False)),
            token_headers={str(k): str(v) for k, v in (entry.get("token_headers") or {}).items()},
            allowed_params={str(k): str(v) for k, v in (entry.get("params") or {}).items()},
        )
    return providers


PROVIDERS: dict[str, _Provider] = _load_providers(os.environ.get("CONNECT_RELAY_PROVIDERS", ""))


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if not RELAY_SECRET:
        raise RuntimeError(
            "LIONCLAW_MCP_RELAY_SECRET is not set — refusing to start an unauthenticated relay."
        )
    yield


app = FastAPI(title="Olano Connect Relay", lifespan=_lifespan)


@dataclass
class _Pending:
    instance_id: str
    provider: str
    created_at: float
    code_verifier: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    bundle: dict[str, Any] | None = None
    error: str | None = None


_STATES: dict[str, _Pending] = {}
# NOTE: in-memory store — fine for a single relay process; a bundle rests here
# at most STATE_TTL seconds and is delivered once. The HA edition (Postgres,
# sealed at rest) is the olano-ai/cloud service.


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


def _merged_params(provider: _Provider, params: dict[str, Any]) -> dict[str, str] | None:
    """Request params over the provider's defaults; ``None`` on any violation.

    Unknown keys are refusals, not silently dropped — same posture as the box.
    """
    merged = dict(provider.allowed_params)
    for key, value in (params or {}).items():
        if key not in provider.allowed_params:
            return None
        merged[str(key)] = str(value)
    for value in merged.values():
        if not _PARAM_RE.match(value):
            return None
    return merged


async def _exchange(
    url: str,
    data: dict[str, str],
    headers: dict[str, str],
    auth: tuple[str, str] | None,
) -> tuple[int, dict[str, Any] | None, str]:
    """POST a token-endpoint request → (status, json_or_None, text).

    The one outbound seam — tests monkeypatch this.
    """
    import httpx

    async with httpx.AsyncClient(timeout=EXCHANGE_TIMEOUT) as client:
        resp = await client.post(url, data=data, headers=headers, auth=auth)
    text = resp.text or ""
    try:
        payload = resp.json()
        if not isinstance(payload, dict):
            payload = None
    except ValueError:
        payload = None
    return resp.status_code, payload, text


async def _post_token(
    provider: _Provider, data: dict[str, str], merged: dict[str, str]
) -> tuple[int, dict[str, Any] | None, str]:
    url = provider.token_url.format(**merged)
    headers = {"Accept": "application/json", **provider.token_headers}
    auth: tuple[str, str] | None = None
    if provider.client_auth == "basic":
        auth = (provider.client_id, provider.client_secret)
    else:
        data = {**data, "client_secret": provider.client_secret}
    return await _exchange(url, data, headers, auth)


def _token_ok(status: int, payload: dict[str, Any] | None) -> bool:
    return status < 400 and isinstance(payload, dict) and bool(payload.get("access_token"))


def _provider_detail(payload: dict[str, Any] | None, text: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        desc = payload.get("error_description")
        if err or desc:
            return " — ".join(str(p) for p in (err, desc) if p)[:400]
    return (text or "")[:400]


def _provider_or_400(provider_id: str) -> _Provider:
    provider = PROVIDERS.get(provider_id)
    if provider is None or not provider.client_id or not provider.token_url:
        raise HTTPException(status_code=400, detail="provider_unavailable")
    return provider


@app.post("/connect/register")
async def connect_register(request: Request) -> JSONResponse:
    body = await request.json()
    instance_id = str(body.get("instance_id", ""))
    state = str(body.get("state", ""))
    ts = str(body.get("ts", ""))
    sig = str(body.get("sig", ""))
    if not state or not _valid_sig(sig, ts, "connect_register", instance_id, state, ts):
        raise HTTPException(status_code=401, detail="bad signature")
    provider = _provider_or_400(str(body.get("provider", "")))
    merged = _merged_params(provider, body.get("params") or {})
    if merged is None:
        raise HTTPException(status_code=400, detail="invalid_params")
    code_verifier = str(body.get("code_verifier") or "") or None
    if provider.pkce and not code_verifier:
        raise HTTPException(status_code=400, detail="code_verifier required")
    now = time.time()
    _prune(now)
    if len(_STATES) >= MAX_STATES:
        raise HTTPException(status_code=503, detail="relay busy; try again")
    _STATES[state] = _Pending(
        instance_id=instance_id,
        provider=provider.provider_id,
        created_at=now,
        code_verifier=code_verifier,
        params=merged,
    )
    return JSONResponse({"ok": True})


@app.get("/connect/callback")
async def connect_callback(request: Request) -> HTMLResponse:
    params = request.query_params
    state = params.get("state") or ""
    code = params.get("code")
    error = params.get("error_description") or params.get("error")
    entry = _STATES.get(state)
    if entry is None or entry.bundle is not None or entry.error is not None:
        return HTMLResponse(_callback_html(False))
    if error:
        entry.error = str(error)[:400]
        return HTMLResponse(_callback_html(False))
    if not code:
        entry.error = "provider returned no code"
        return HTMLResponse(_callback_html(False))
    provider = PROVIDERS.get(entry.provider)
    if provider is None:
        entry.error = "provider_unavailable"
        return HTMLResponse(_callback_html(False))

    data = {
        "grant_type": "authorization_code",
        "code": code,
        # Must byte-match the authorize request's redirect_uri: this endpoint.
        "redirect_uri": str(request.url.replace(query="")),
        "client_id": provider.client_id,
    }
    if provider.pkce and entry.code_verifier:
        data["code_verifier"] = entry.code_verifier
    try:
        status, payload, text = await _post_token(provider, data, entry.params)
    except Exception as exc:  # network/TLS/DNS — degrade, never 500
        entry.error = f"exchange failed: {type(exc).__name__}"
        return HTMLResponse(_callback_html(False))
    if not _token_ok(status, payload):
        entry.error = f"exchange failed ({status}): {_provider_detail(payload, text)}"[:400]
        return HTMLResponse(_callback_html(False))
    entry.bundle = {
        "provider": entry.provider,
        "token_response": payload,
        "callback_query": {k: v for k, v in params.items() if k != "code"},
        "obtained_at": time.time(),
    }
    return HTMLResponse(_callback_html(True))


@app.get("/connect/pending")
async def connect_pending(instance_id: str, state: str, ts: str, sig: str) -> JSONResponse:
    if not _valid_sig(sig, ts, "connect_pending", instance_id, state, ts):
        raise HTTPException(status_code=401, detail="bad signature")
    entry = _STATES.get(state)
    if entry is None or entry.instance_id != instance_id:
        return JSONResponse({"status": "unknown"})
    if entry.error is not None:
        _STATES.pop(state, None)
        return JSONResponse({"status": "error", "error": entry.error})
    if entry.bundle is not None:
        _STATES.pop(state, None)  # one-time delivery
        return JSONResponse({"status": "ready", "bundle": entry.bundle})
    return JSONResponse({"status": "pending"})


@app.post("/connect/refresh")
async def connect_refresh(request: Request) -> JSONResponse:
    body = await request.json()
    instance_id = str(body.get("instance_id", ""))
    ts = str(body.get("ts", ""))
    sig = str(body.get("sig", ""))
    token = str(body.get("refresh_token", ""))
    if not token:
        raise HTTPException(status_code=400, detail="invalid_request")
    # Stateless: the state slot binds the signature to this exact token.
    binding = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not _valid_sig(sig, ts, "connect_refresh", instance_id, binding, ts):
        raise HTTPException(status_code=401, detail="bad signature")
    provider = _provider_or_400(str(body.get("provider", "")))
    merged = _merged_params(provider, body.get("params") or {})
    if merged is None:
        raise HTTPException(status_code=400, detail="invalid_params")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": provider.client_id,
    }
    try:
        status, payload, text = await _post_token(provider, data, merged)
    except Exception as exc:
        return JSONResponse(
            {
                "error": "provider_error",
                "status": 0,
                "detail": f"unreachable: {type(exc).__name__}",
            },
            status_code=502,
        )
    if not _token_ok(status, payload):
        return JSONResponse(
            {
                "error": "provider_error",
                "status": status,
                "detail": _provider_detail(payload, text),
            },
            status_code=502,
        )
    return JSONResponse({"ok": True, "token_response": payload})


def _callback_html(ok: bool) -> str:
    title = "Authorization received" if ok else "Authorization problem"
    detail = (
        "You can close this tab and return to your assistant."
        if ok
        else "This link has expired or was already used. Ask your assistant to try again."
    )
    accent = "#48d389" if ok else "#f46363"
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>Olano · Connect</title>'
        "<style>body{margin:0;min-height:100vh;display:flex;align-items:center;"
        "justify-content:center;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#0b0d12;color:#e8eaed}.card{max-width:420px;padding:32px;border-radius:14px;"
        "background:#13161d;border:1px solid rgba(255,255,255,.08);text-align:center}"
        f".dot{{width:10px;height:10px;border-radius:50%;background:{accent};display:inline-block;"
        "margin-right:8px}h1{font-size:18px;margin:0 0 12px}p{font-size:14px;color:#a9b0bd;margin:0}"
        f'</style></head><body><div class="card"><h1><span class="dot"></span>{title}</h1>'
        f"<p>{detail}</p></div></body></html>"
    )


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {"ok": True, "pending": len(_STATES), "providers": sorted(PROVIDERS)}
