"""Client for the central official-connector auth relay (``/connect/*``).

Official Olano connectors let a customer click *Connect* without minting their
own OAuth app: the box builds the authorize URL with the **official client id**
(published through the ``connectors`` catalog), but the redirect URI is the
central relay's — ``https://auth.olano.ai/connect/callback`` — and the code
exchange plus every refresh run **on the relay**, so the official client secret
never reaches a customer box. Tokens still land only here, in the broker's
ordinary per-scope record files.

This is a deliberate **sibling of** :mod:`olano.core.mcp_relay`, not part of
it: the MCP relay shuttles authorization *codes* for MCP-server OAuth (the box
exchanges them itself), while this flow ships *sealed token bundles* and
central refresh for the built-in toolkits' connectors. What the two share is
the instance identity and the frozen signing contract — same key material,
same ``signed_message`` scheme — under **new action names**, so a signature
for one flow can never authorize the other:

    connect_register  ("connect_register", instance_id, state, ts)
    connect_pending   ("connect_pending",  instance_id, state, ts)
    connect_refresh   ("connect_refresh",  instance_id, sha256_hex(refresh_token), ts)

Refresh is stateless — no OAuth state exists — so the state slot binds the
signature to the exact token being refreshed (the relay recomputes the hash
from the body). ``ts`` stays the last part; the relay freshness-checks
``parts[-1]``. The golden vector for all three tuples lives at
``tests/fixtures/relay/connect_signing_golden.json``, committed to both repos.

Everything here is synchronous on purpose: the connector broker
(``core/connectors/base.py``) is a sync module driven from worker threads, and
its relay pump is a daemon thread.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

from olano.core import mcp_relay
from olano.core.mcp_relay import RelayConfig, sign_request

# Relay HTTP contract (paths under the relay base URL).
CONNECT_CALLBACK_PATH = "/connect/callback"
CONNECT_REGISTER_PATH = "/connect/register"
CONNECT_PENDING_PATH = "/connect/pending"
CONNECT_REFRESH_PATH = "/connect/refresh"

ACTION_REGISTER = "connect_register"
ACTION_PENDING = "connect_pending"
ACTION_REFRESH = "connect_refresh"

_CONNECT_RELAY_URL_ENV = "LIONCLAW_CONNECT_RELAY_URL"


def refresh_binding(refresh_token: str) -> str:
    """The state-slot value a refresh signature binds: sha256 hex of the token.

    UTF-8 before hashing — pinned by the golden vector's non-ASCII case.
    """
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


# ── Config resolution ───────────────────────────────────────────────────────


def connect_relay_url() -> str | None:
    """The connect relay base URL.

    ``LIONCLAW_CONNECT_RELAY_URL`` overrides for a split deployment; by default
    the /connect endpoints live on the same service as the MCP relay
    (auth.olano.ai), so the *configured* MCP relay URL is the natural fallback.

    Configured, never the self-hosted default: a box that reaches the MCP relay
    only through an account-key enrollment (``mcp_relay_selfhost``) holds an
    identity the relay refuses on every ``/connect`` route, because official
    connectors spend Olano's own OAuth apps and stay managed-only.
    """
    env = os.environ.get(_CONNECT_RELAY_URL_ENV)
    if env and env.strip():
        return env.strip().rstrip("/")
    return mcp_relay.configured_relay_url()


def resolve_connect_relay_config() -> RelayConfig | None:
    """The active relay config for /connect calls, or ``None`` when unset.

    Reuses the MCP relay's identity wholesale — same instance id, same Ed25519
    private key (or legacy shared secret): one enrolled identity signs both
    flows, distinguished by action names. Raises ``RuntimeError`` when a URL is
    configured but no credential is, mirroring
    :func:`olano.core.mcp_relay.resolve_relay_config`.
    """
    url = connect_relay_url()
    if not url:
        return None
    from olano.core import mcp_relay_selfhost

    if mcp_relay_selfhost.holds_selfhost_identity():
        # The identity was enrolled over an account API key: the relay marks it
        # self-hosted and answers 403 on /connect, so say "no relay" now rather
        # than offer a Connect button that can only fail. A self-hoster's own
        # OAuth app (BYO) is unaffected.
        return None
    private_key = mcp_relay.relay_private_key() or ""
    secret = mcp_relay.relay_secret() or ""
    if not private_key and not secret:
        raise RuntimeError(
            "A connect relay URL is configured but no relay credential is — set "
            "LIONCLAW_MCP_RELAY_PRIVATE_KEY (env or vault) to this instance's "
            "Ed25519 private key, or LIONCLAW_MCP_RELAY_SECRET for a "
            "shared-secret relay."
        )
    return RelayConfig(
        url=url, secret=secret, instance_id=mcp_relay.instance_id(), private_key=private_key
    )


def connect_callback_uri(cfg: RelayConfig) -> str:
    """The redirect URI official flows present — the relay's, never the box's."""
    return f"{cfg.url}{CONNECT_CALLBACK_PATH}"


# ── Box → relay calls (sync — the broker runs in worker threads) ────────────


def _detail_from(resp: Any) -> str:
    """A short error detail from a relay response (its ``detail``/``error`` or text)."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            for key in ("detail", "error"):
                if data.get(key):
                    return str(data[key])
    except ValueError:
        pass
    return (resp.text or "")[:300]


def register_connect_state(
    cfg: RelayConfig,
    state: str,
    *,
    provider: str,
    code_verifier: str | None,
    params: dict[str, str],
    timeout: float = 10.0,
) -> None:
    """Announce a pending official authorization to the relay (signed).

    Raises ``RuntimeError`` with the relay's detail on refusal — the caller
    turns that into an actionable connect-panel error (the BYO path is
    unaffected by a dead relay).
    """
    import httpx

    ts = str(int(time.time()))
    payload: dict[str, Any] = {
        "instance_id": cfg.instance_id,
        "state": state,
        "ts": ts,
        "sig": sign_request(cfg, ACTION_REGISTER, cfg.instance_id, state, ts),
        "provider": provider,
        "params": dict(params),
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    resp = httpx.post(f"{cfg.url}{CONNECT_REGISTER_PATH}", json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(
            f"auth relay refused the authorization ({resp.status_code}): {_detail_from(resp)}"
        )


def poll_connect_bundle(cfg: RelayConfig, state: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Poll the relay for the finished authorization's token bundle (signed).

    Returns the relay's JSON: ``{"status": "pending" | "unknown" | "error"
    (+error) | "ready" (+bundle)}`` where ``bundle`` is
    ``{provider, token_response, callback_query, obtained_at}`` — delivered
    exactly once.
    """
    import httpx

    ts = str(int(time.time()))
    params = {
        "instance_id": cfg.instance_id,
        "state": state,
        "ts": ts,
        "sig": sign_request(cfg, ACTION_PENDING, cfg.instance_id, state, ts),
    }
    resp = httpx.get(f"{cfg.url}{CONNECT_PENDING_PATH}", params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def refresh_via_relay(
    cfg: RelayConfig,
    provider: str,
    refresh_token: str,
    *,
    params: dict[str, str] | None = None,
    timeout: float = 35.0,
) -> dict[str, Any]:
    """Run a central refresh; returns the provider's token response verbatim.

    The signature carries ``sha256_hex(refresh_token)`` in the state slot, so
    it authorizes refreshing exactly this token. Raises ``RuntimeError`` on any
    refusal (relay 4xx/5xx, provider rejection relayed as 502
    ``provider_error``) — the broker maps that to its existing
    stale-while-valid semantics. The timeout leaves room for the relay's own
    30s provider deadline.
    """
    import httpx

    ts = str(int(time.time()))
    binding = refresh_binding(refresh_token)
    payload: dict[str, Any] = {
        "instance_id": cfg.instance_id,
        "ts": ts,
        "sig": sign_request(cfg, ACTION_REFRESH, cfg.instance_id, binding, ts),
        "provider": provider,
        "refresh_token": refresh_token,
        "params": dict(params or {}),
    }
    resp = httpx.post(f"{cfg.url}{CONNECT_REFRESH_PATH}", json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"relay refresh failed ({resp.status_code}): {_detail_from(resp)}")
    data = resp.json()
    token_response = data.get("token_response") if isinstance(data, dict) else None
    if not isinstance(token_response, dict) or not token_response.get("access_token"):
        raise RuntimeError("relay refresh returned no token_response")
    return token_response
