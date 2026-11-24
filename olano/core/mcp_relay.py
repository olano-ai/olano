"""Client for the central MCP OAuth relay.

A domain-less olano deployment (a bare cloud instance with only a public IP)
cannot host a **trusted-HTTPS** OAuth redirect of its own — providers reject
non-HTTPS/self-signed redirect URIs and you can't get a CA cert for a raw IP. The
relay solves that: a small always-on service on a domain **you** own (e.g.
``https://auth.olano.ai``) owns the single registered redirect URI for every
provider and shuttles the short-lived authorization ``code`` back to the box
that started the flow.

Privacy / multi-tenancy — the relay only ever sees the opaque OAuth ``state`` and
``code``. The **token exchange and storage happen on the box**, keyed per
``(agent, server-url)`` (see :func:`olano.core.mcp_setup.token_store_path`). So:

- different **customers** (separate deployments / homes) are isolated by
  construction;
- two **agents** in one deployment authorizing the *same* MCP each sign into
  whatever account they choose and get **separate** token files;
- customer accounts and tokens **never touch** the central relay.

This module is the box side: resolve the relay config, sign requests,
register a pending ``state``, and poll for the returned ``code``. The relay HTTP
contract it speaks to is implemented by ``services/mcp_oauth_relay``.

Signing — two schemes, same signed message
------------------------------------------
Both sign ``"\\n".join([action, instance_id, state, ts])`` (UTF-8); only the key
and the signature encoding differ.

- **Ed25519** (``LIONCLAW_MCP_RELAY_PRIVATE_KEY``) — **preferred.** This box holds
  the private key; the relay stores only the matching *public* key, so reading
  the control-plane database yields nothing that can sign a request. Under the
  old symmetric scheme the verifier needed the real signing key, so it sat in the
  database in plaintext, where a database read yields it. Signature goes
  on the wire base64url-unpadded.
- **HMAC-SHA256** (``LIONCLAW_MCP_RELAY_SECRET``) — the shared-secret scheme, kept
  because it is still the right one in two cases: a box that has not been
  re-keyed yet, and the single-secret **reference relay** in
  ``services/mcp_oauth_relay`` that a self-hoster runs. Signature is lowercase hex.

A box with both configured uses Ed25519. Cloud-provisioned boxes get their
private key from cloud-init or mint it themselves via the relay-key agent.

**Frozen contract.** This scheme must stay byte-for-byte identical to the relay's
verifier (``services/mcp_oauth_relay/hmac_contract.py``); a mismatch
silently breaks every in-flight authorization. ``tests/fixtures/relay/`` holds the
golden vector committed to both repos so drift fails CI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

# Relay HTTP contract (paths under the relay base URL).
RELAY_CALLBACK_PATH = "/mcp/callback"
REGISTER_PATH = "/mcp/register"
PENDING_PATH = "/mcp/pending"

# Signed requests carry a unix ``ts``; the relay rejects anything older than this
# (replay window). Kept generous to tolerate modest clock skew.
SIGNATURE_TTL = 300.0

_RELAY_PRIVATE_KEY_ENV = "LIONCLAW_MCP_RELAY_PRIVATE_KEY"
_RELAY_SECRET_ENV = "LIONCLAW_MCP_RELAY_SECRET"
_RELAY_URL_ENV = "LIONCLAW_MCP_RELAY_URL"
_INSTANCE_ID_ENV = "LIONCLAW_INSTANCE_ID"


@dataclass(frozen=True)
class RelayConfig:
    url: str  # base, no trailing slash
    secret: str  # shared HMAC secret ("" when signing with a private key)
    instance_id: str
    private_key: str = ""  # base64 PKCS8 DER Ed25519 private key; preferred

    @property
    def signs_ed25519(self) -> bool:
        return bool(self.private_key)

    @property
    def callback_uri(self) -> str:
        return f"{self.url}{RELAY_CALLBACK_PATH}"


# ── Config resolution ───────────────────────────────────────────────────────


def configured_relay_url() -> str | None:
    """The relay an operator configured: ``LIONCLAW_MCP_RELAY_URL`` env → home
    ``mcp_relay_url``. ``None`` when neither is set.

    This is what a managed box always has (cloud-init writes the env), and the
    only URL the official-connector relay ever follows
    (``connect_relay.connect_relay_url``): a self-hosted enrollment must never
    turn ``/connect`` on.
    """
    env = os.environ.get(_RELAY_URL_ENV)
    if env and env.strip():
        return env.strip().rstrip("/")
    from olano.config.home import load_home_config

    configured = load_home_config().mcp_relay_url
    return configured.rstrip("/") if configured else None


def relay_url() -> str | None:
    """The relay base URL MCP sign-in uses.

    A configured relay (:func:`configured_relay_url`) always wins, which keeps
    every managed box exactly as it was. With none configured, a self-hosted
    box that enrolled with its Olano API key uses the official relay
    (:func:`olano.core.mcp_relay_selfhost.enrolled_relay_url`); that answer is
    ``None`` on a managed box, without a key, or with an HTTPS
    ``LIONCLAW_PUBLIC_URL`` (the box's own callback wins).
    """
    configured = configured_relay_url()
    if configured:
        return configured
    from olano.core import mcp_relay_selfhost

    return mcp_relay_selfhost.enrolled_relay_url()


def _from_env_or_vault(name: str) -> str | None:
    """A credential: env → vault. Never read from config.yaml (both are secrets)."""
    env = os.environ.get(name)
    if env and env.strip():
        return env.strip()
    try:
        from olano.vault.resolver import get_vault

        got = get_vault().get(name, touch=False)
    except Exception:
        return None
    text = str(got).strip() if got else ""
    return text or None


def relay_private_key() -> str | None:
    """The Ed25519 private key (base64 PKCS8 DER): env → vault."""
    return _from_env_or_vault(_RELAY_PRIVATE_KEY_ENV)


def relay_secret() -> str | None:
    """The shared HMAC secret: env → vault. Never read from config.yaml (secret)."""
    return _from_env_or_vault(_RELAY_SECRET_ENV)


def instance_id() -> str:
    """Stable per-deployment id. Env → home config → generated-and-persisted."""
    env = os.environ.get(_INSTANCE_ID_ENV)
    if env and env.strip():
        return env.strip()
    from olano.config.home import load_home_config, update_home_config

    existing = load_home_config().instance_id
    if existing:
        return existing
    new_id = uuid.uuid4().hex[:16]
    try:
        update_home_config({"instance_id": new_id})
    except Exception:
        pass  # best-effort persistence; the id is still usable for this process
    return new_id


def resolve_relay_config() -> RelayConfig | None:
    """Return the active :class:`RelayConfig`, or ``None`` when no relay is set.

    Prefers the Ed25519 private key and falls back to the shared HMAC secret, so a
    box that has not been re-keyed yet (and a self-hoster pointed at the
    single-secret reference relay) keeps working unchanged.

    Raises ``RuntimeError`` if a relay URL is configured but neither credential is
    — a misconfiguration that would otherwise fail obscurely at request time.
    """
    url = relay_url()
    if not url:
        return None
    private_key = relay_private_key() or ""
    secret = relay_secret() or ""
    if not private_key and not secret:
        raise RuntimeError(
            f"{_RELAY_URL_ENV} is set but no relay credential is configured — set "
            f"{_RELAY_PRIVATE_KEY_ENV} (env or vault) to this instance's Ed25519 "
            f"private key, or {_RELAY_SECRET_ENV} for a shared-secret relay."
        )
    return RelayConfig(url=url, secret=secret, instance_id=instance_id(), private_key=private_key)


# ── Request signing (frozen contract — shared with the relay service) ───────


def signed_message(*parts: str) -> bytes:
    """The exact bytes both schemes sign — the heart of the frozen contract."""
    return "\n".join(parts).encode("utf-8")


def sign(secret: str, *parts: str) -> str:
    """HMAC-SHA256 over the signed message — the shared-secret scheme."""
    return hmac.new(secret.encode("utf-8"), signed_message(*parts), hashlib.sha256).hexdigest()


def verify(secret: str, signature: str, *parts: str) -> bool:
    """Constant-time check of a signature produced by :func:`sign`."""
    return hmac.compare_digest(sign(secret, *parts), signature)


def load_private_key(private_key_b64: str) -> Any:
    """Load this instance's Ed25519 private key from base64 PKCS8 DER.

    That serialization is what the provisioning worker and the relay-key agent
    both emit, and it matches
    how ``scripts/settings-keygen.mjs`` stores the deployment-settings keys — one
    convention for every Ed25519 private key in the system.

    **Deliberately strict about PKCS8.** A raw 32-byte value is also a
    syntactically valid Ed25519 seed, so accepting one would silently accept a
    misconfigured *public* key — which is likewise 32 raw bytes — and turn it into
    a private key that signs garbage. Every relay call would then fail with an
    opaque 401 and nothing pointing at the cause. Nothing in the system writes a
    raw seed, so the tolerance would buy nothing and cost that.

    Either base64 alphabet is fine, padded or not; that is an encoding detail, not
    a key-type ambiguity.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    try:
        raw = base64.b64decode(_pad_b64(private_key_b64), validate=True)
    except ValueError as exc:  # includes binascii.Error
        raise ValueError(f"{_RELAY_PRIVATE_KEY_ENV} is not valid base64") from exc
    try:
        key = serialization.load_der_private_key(raw, password=None)
    except Exception as exc:
        raise ValueError(
            f"{_RELAY_PRIVATE_KEY_ENV} must be base64 of a PKCS8 DER private key "
            "(a raw seed or a public key is not accepted)"
        ) from exc
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError(f"{_RELAY_PRIVATE_KEY_ENV} is not an Ed25519 private key")
    return key


def _pad_b64(text: str) -> str:
    """Normalize either base64 alphabet and restore stripped padding."""
    stripped = text.strip().replace("-", "+").replace("_", "/").rstrip("=")
    return stripped + "=" * (-len(stripped) % 4)


def sign_ed25519(private_key_b64: str, *parts: str) -> str:
    """Ed25519-sign the message; base64url-unpadded, as the relay expects."""
    signature = load_private_key(private_key_b64).sign(signed_message(*parts))
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def public_key_b64(private_key_b64: str) -> str:
    """The matching public key, base64 of the raw 32 bytes — what the relay stores.

    Only needed for enrollment/diagnostics; the relay is told the public key out of
    band (the relay-key agent, or the provisioning worker).
    """
    from cryptography.hazmat.primitives import serialization

    raw = (
        load_private_key(private_key_b64)
        .public_key()
        .public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    )
    return base64.b64encode(raw).decode("ascii")


def sign_request(cfg: RelayConfig, *parts: str) -> str:
    """Sign a relay request with whichever credential *cfg* carries."""
    if cfg.signs_ed25519:
        return sign_ed25519(cfg.private_key, *parts)
    return sign(cfg.secret, *parts)


# ── Box → relay calls ───────────────────────────────────────────────────────


async def register_state(cfg: RelayConfig, state: str, *, timeout: float = 10.0) -> None:
    """Announce a pending authorization ``state`` to the relay (signed)."""
    import httpx

    ts = str(int(time.time()))
    payload = {
        "instance_id": cfg.instance_id,
        "state": state,
        "ts": ts,
        "sig": sign_request(cfg, "register", cfg.instance_id, state, ts),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{cfg.url}{REGISTER_PATH}", json=payload)
        resp.raise_for_status()


async def poll_code(cfg: RelayConfig, state: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Poll the relay for the returned ``code`` for *state*.

    Returns the relay's JSON: ``{"status": "pending" | "ready" | "error", ...}``
    with ``code`` on ready and ``error`` on error.
    """
    import httpx

    ts = str(int(time.time()))
    params = {
        "instance_id": cfg.instance_id,
        "state": state,
        "ts": ts,
        "sig": sign_request(cfg, "pending", cfg.instance_id, state, ts),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{cfg.url}{PENDING_PATH}", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}
