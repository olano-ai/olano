"""Generic OAuth2 connector broker — provider spec, flow machinery, token store.

Generalizes the headless Google OAuth flow (``tools/google_tools.py``) into a
provider registry so the dashboard "Connections" tab can mint, store, refresh,
and scope tokens for any OAuth service. Google itself keeps its bespoke
implementation and legacy token paths; the runtime API dispatches
``service == "google"`` to ``google_tools`` and everything else here.

Shape parity with the Google flow is deliberate:

- the same session statuses (``awaiting_authorization | exchanging |
  authorized | error``) and ``public()`` payload, so the dashboard's
  ConnectPanel poll loop works unchanged for every service;
- the same three-tier token scoping (own agent → any agent whose token is
  marked shared → system), stored via the same ``connections.json`` meta file;
- headless-safe: the server never opens a browser — the dashboard surfaces the
  authorization link and polls until the callback lands.

Tokens are JSON records at ``<home>/connectors/<service>_token.json`` (system)
or ``<home>/agents/<name>/connectors/<service>_token.json`` (per-agent),
written ``0600``. Toolkits do not read these files: the credential resolver in
``tools/_credentials.py`` consults :func:`resolve_exposed_credential` so a
live (auto-refreshed) access token surfaces through the credential names each
toolkit already reads (e.g. ``MS365_ACCESS_TOKEN``).

All outbound HTTP funnels through :func:`_post_token` / :func:`_get_json` so
tests (and provider hooks) have exactly one seam to mock.

Official connectors (the relay fork)
------------------------------------
When no BYO client resolves and the ``connectors`` catalog publishes an
official Olano client for the provider, the flow forks to **relay mode**
(``auth_mode == "relay"``): the authorize URL carries the official client id
and the central relay's redirect URI, the relay performs the code exchange
(the official client secret never reaches this box), and a daemon-thread pump
polls the sealed token bundle back. The record it saves is a completely
ordinary broker record — same path, same fields, ``client_secret`` empty,
plus ``auth_mode``/``client_source`` — and refresh forks per record on
``auth_mode``, so BYO connections and existing tokens behave byte-identically
to before. BYO always beats official: the official client is a resolution
*floor*, engaged only when body/vault/env yield nothing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

# Refresh when the access token is within this many seconds of expiry.
_REFRESH_MARGIN_S = 120.0
# Drop pending authorization sessions older than this (mirrors google flow).
_SESSION_MAX_AGE_S = 1800.0
# Connect-time params (shop, tenant, …) are embedded in provider URLs — keep
# them to a conservative charset so a hostile value can't rewrite the URL.
_PARAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
# Relay-mode pump: poll cadence, and how many consecutive "unknown" answers
# (the relay lost the state — restart, TTL expiry) before giving up early.
_RELAY_POLL_S = 2.0
_RELAY_UNKNOWN_LIMIT = 5

# A token the SERVICE refused. The status probe reads the file, and a file
# cannot know that an admin revoked the session, that the org's session policy
# is shorter than ``expires_default``, or that the grant died with the user's
# password change, so a dead connection read "Connected" until someone
# disconnected and connected it again by hand. The toolkits know, because they
# are the ones handed the 401: :func:`refresh_record_now` is their first move
# (a refresh regardless of expiry, the honest fix when the grant is alive) and
# :func:`mark_auth_failed` the second, when the refreshed or unrefreshable token
# is refused too. A marked record still resolves (the mark is a diagnosis, not a
# deletion) but reports ``connected: False`` + ``needs_reauth``; the next
# successful refresh or a fresh sign-in clears it, because both prove the grant.
_AUTH_FAILED_AT = "auth_failed_at"
_AUTH_ERROR = "auth_error"
# A toolkit in a retry loop must not hammer the token endpoint: a forced
# refresh on a record marked within this window is answered from the mark.
_FORCED_REFRESH_COOLDOWN_S = 60.0
_AUTH_ERROR_MAX_CHARS = 240


# ── HTTP seams (monkeypatch these in tests) ────────────────────────────────


def _post_token(
    url: str,
    data: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """POST a token-endpoint request and return the parsed JSON body.

    Form-encoded by default (RFC 6749 §4.1.3); a ``Content-Type:
    application/json`` header switches to a JSON body for the providers that
    insist on one (Notion). Raises ``RuntimeError`` with the response text on
    a non-2xx status so callers surface the provider's actual error message.
    """
    import httpx

    hdrs = {"Accept": "application/json", **(headers or {})}
    if hdrs.get("Content-Type", "").lower().startswith("application/json"):
        resp = httpx.post(url, json=data, headers=hdrs, auth=auth, timeout=30)
    else:
        resp = httpx.post(url, data=data, headers=hdrs, auth=auth, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Token endpoint returned {resp.status_code}: {resp.text[:500]}")
    return dict(resp.json())


def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """GET a JSON resource (used by provider hooks like DocuSign userinfo)."""
    import httpx

    hdrs = {"Accept": "application/json", **(headers or {})}
    resp = httpx.get(url, headers=hdrs, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"GET {url} returned {resp.status_code}: {resp.text[:500]}")
    return dict(resp.json())


# ── Provider spec ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeviceAuth:
    """The device-code recipe (RFC 8628) a provider may declare beside OAuth.

    A *public* client: the id ships in the wheel, there is no secret, and the
    person types a short code on the provider's site from any device — no
    OAuth app to register, no redirect URI, which is what a phone-first sign-in
    and a self-hosted box both need. The result is saved as an ordinary broker
    token record (``auth_mode: "device"``), so every consumer that resolves
    the provider's ``exposes`` names sees it exactly like a redirect-flow
    token. ``client_id_env`` / ``scopes_env`` let an enterprise front its own
    app without a code change.
    """

    client_id: str
    scopes: tuple[str, ...]
    device_code_url: str
    token_url: str
    verification_url: str
    client_id_env: str | None = None
    scopes_env: str | None = None
    # Optional "who am I" lookup after approval: GET ``account_url`` with the
    # bearer token and store ``payload[account_field]`` as ``extras.account``
    # so the Connections card can show the signed-in login.
    account_url: str | None = None
    account_field: str = "login"
    grant_type: str = "urn:ietf:params:oauth:grant-type:device_code"


@dataclass(frozen=True)
class ConnectorProvider:
    """Declarative description of one service behind the broker.

    The redirect (authorization-code) recipe is the fields below; a provider
    may additionally declare a ``device`` recipe. The ``key`` method needs no
    recipe: a pasted key lands in the vault under one of the ``exposes`` names
    and resolves through the same ``get_credential`` chain.
    """

    id: str
    name: str
    # May contain {param} placeholders filled from connect-time params
    # (e.g. Shopify's per-shop URLs, Microsoft's tenant).
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    # Vault/env key holding a JSON blob {"client_id": …, "client_secret": …}.
    creds_vault_key: str
    # (CLIENT_ID_KEY, CLIENT_SECRET_KEY) fallback pair resolved via
    # get_credential — matches the names in subagents/_defaults.py.
    client_env_pair: tuple[str, str] | None = None
    # credential name → "access_token" | "extras.<key>" | "params.<key>"
    exposes: dict[str, str] = field(default_factory=dict)
    pkce: bool = False
    client_auth: str = "post"  # "post" (creds in body) | "basic" (Authorization header)
    token_method: str = "post"  # "post" | "get" (Meta's Graph token endpoint)
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    scope_separator: str = " "
    required_params: tuple[str, ...] = ()
    optional_params: dict[str, str] = field(default_factory=dict)
    supports_refresh: bool = True
    refresh_includes_scope: bool = False
    # OAuth callback query params to capture into extras (e.g. QuickBooks realmId).
    callback_extras: tuple[str, ...] = ()
    # Token-response fields to capture into extras (e.g. Salesforce instance_url).
    token_extras: tuple[str, ...] = ()
    # Assumed token lifetime (s) when the response has no expires_in; None → non-expiring.
    expires_default: float | None = None
    # Extra headers on token-endpoint requests (exchange AND refresh) — e.g.
    # Reddit rejects generic User-Agents.
    token_headers: dict[str, str] = field(default_factory=dict)
    # Mutates the record dict in place after code exchange, before save
    # (Meta long-lived exchange, DocuSign userinfo).
    post_exchange: Callable[[ConnectorProvider, dict[str, Any]], None] | None = None
    # Normalize connect-time params (e.g. strip ".myshopify.com" from a shop).
    normalize_params: Callable[[dict[str, str]], dict[str, str]] | None = None
    # Full override of the token-request build → (url, data, headers, auth)
    # for non-standard exchanges like Shopify's grant_type-less endpoint.
    token_request: (
        Callable[
            [ConnectorProvider, OAuthSession, str],
            tuple[str, dict[str, str], dict[str, str] | None, tuple[str, str] | None],
        ]
        | None
    ) = None
    # Device-code sign-in (see DeviceAuth); None = redirect flow only.
    device: DeviceAuth | None = None


def auth_methods(provider: ConnectorProvider) -> list[str]:
    """The sign-in methods this provider declares: ``oauth``, ``device``, ``key``."""
    methods = ["oauth"]
    if provider.device is not None:
        methods.append("device")
    if provider.exposes:
        methods.append("key")
    return methods


# ── Session (mirrors GoogleAuthSession) ────────────────────────────────────


@dataclass
class OAuthSession:
    service: str
    session_id: str
    agent: str | None
    shared: bool
    token_path: Path
    redirect_uri: str
    client_id: str
    client_secret: str
    params: dict[str, str] = field(default_factory=dict)
    state: str | None = None
    code_verifier: str | None = None
    authorization_url: str | None = None
    # awaiting_authorization | exchanging | authorized | error
    status: str = "awaiting_authorization"
    error: str | None = None
    created_at: float = 0.0
    logs: list[dict] = field(default_factory=list)
    # "direct" (on-box callback + exchange) | "relay" (central exchange via
    # the official client — see the module docstring's relay fork).
    auth_mode: str = "direct"
    # Where the client came from: "body" | "vault" | "env" (all BYO) | "official".
    client_source: str = "byo"

    def log(self, level: str, message: str) -> None:
        self.logs.append({"ts": time.time(), "level": level, "message": message})
        logger.log(
            logging.ERROR if level == "error" else logging.INFO,
            "connector-oauth[%s/%s]: %s",
            self.service,
            self.session_id,
            message,
        )

    def public(self) -> dict:
        return {
            "session_id": self.session_id,
            "service": self.service,
            "status": self.status,
            "authorization_url": self.authorization_url,
            "redirect_uri": self.redirect_uri,
            "error": self.error,
            "agent": self.agent,
            "shared": self.shared,
            "logs": self.logs,
            "auth_mode": self.auth_mode,
        }


_SESSIONS: dict[str, OAuthSession] = {}
_BY_STATE: dict[str, str] = {}

# Token records are cached by path + mtime so get_credential stays cheap, and
# refreshes are serialized per path so concurrent tool calls don't double-refresh.
_RECORD_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _prune_sessions(max_age: float = _SESSION_MAX_AGE_S) -> None:
    now = time.time()
    stale = [sid for sid, s in _SESSIONS.items() if now - s.created_at > max_age]
    for sid in stale:
        sess = _SESSIONS.pop(sid, None)
        if sess and sess.state:
            _BY_STATE.pop(sess.state, None)


# ── Connections meta (shared flag — same file Google uses) ─────────────────


def _load_connections_meta(agent_name: str) -> dict:
    path = get_olano_paths().agent_connections_meta_path(agent_name)
    if not path.exists():
        return {}
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return {}


def _save_connections_meta(agent_name: str, meta: dict) -> None:
    path = get_olano_paths().agent_connections_meta_path(agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def is_connector_shared(service: str, agent_name: str) -> bool:
    return bool(_load_connections_meta(agent_name).get(service, {}).get("shared", False))


def set_connector_shared(service: str, agent_name: str, shared: bool) -> None:
    meta = _load_connections_meta(agent_name)
    meta.setdefault(service, {})["shared"] = shared
    _save_connections_meta(agent_name, meta)


def clear_connector_meta(service: str, agent_name: str) -> None:
    """Drop a service's entry from the agent's connections meta.

    Called on disconnect: a leftover ``shared: true`` would silently re-share
    the *next* token connected for this service, even though the reconnect
    asked for a private one.
    """
    meta = _load_connections_meta(agent_name)
    if meta.pop(service, None) is not None:
        _save_connections_meta(agent_name, meta)


# ── Token store ────────────────────────────────────────────────────────────


def _token_path_for(service: str, agent: str | None) -> Path:
    paths = get_olano_paths()
    if agent:
        return paths.agent_connector_token_path(agent, service)
    return paths.connector_token_path(service)


def _find_shared_token(service: str, exclude: str | None = None) -> Path | None:
    """First agent token for ``service`` marked shared=True, skipping ``exclude``."""
    paths = get_olano_paths()
    agents_dir = paths.agents_dir
    if not agents_dir.exists():
        return None
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name == exclude:
            continue
        token = paths.agent_connector_token_path(agent_dir.name, service)
        if token.exists() and is_connector_shared(service, agent_dir.name):
            return token
    return None


def _resolve_token_path(service: str, agent: str | None) -> Path:
    """Resolve own → shared → system, mirroring the Google three-tier lookup."""
    paths = get_olano_paths()
    if agent is None:
        return paths.connector_token_path(service)
    own = paths.agent_connector_token_path(agent, service)
    if own.exists():
        return own
    shared = _find_shared_token(service, exclude=agent)
    if shared:
        return shared
    return paths.connector_token_path(service)


def _save_record(record: dict[str, Any], path: Path) -> None:
    from olano.security.private_files import write_private_text

    # Owner-only from the first byte: the record holds the connector's tokens.
    write_private_text(path, json.dumps(record, indent=2))
    _RECORD_CACHE.pop(str(path), None)


def _load_record(path: Path) -> dict[str, Any] | None:
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _RECORD_CACHE.pop(key, None)
        return None
    cached = _RECORD_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        record = dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None
    _RECORD_CACHE[key] = (mtime, record)
    return record


def _refresh_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(key)
        if lock is None:
            lock = _REFRESH_LOCKS[key] = threading.Lock()
        return lock


# ── Client-credential resolution ───────────────────────────────────────────


def relay_capable(provider: ConnectorProvider) -> bool:
    """True when the provider's exchange is the standard POST the relay runs.

    The central relay implements exactly one exchange shape: an
    authorization-code POST (``post``/``basic`` client auth, optional PKCE,
    templated token URL). A provider with a custom ``token_request`` builder
    (Shopify) or a GET token method (Meta) can't route through it and stays
    BYO-only. Buffer is relay-shaped but excluded the other way: no official
    client is obtainable, so it simply never gets a catalog row.
    """
    return provider.token_request is None and provider.token_method == "post"


def _official_client(provider: ConnectorProvider) -> str | None:
    """The usable official client id for *provider*, else ``None``.

    "Usable" = the exchange is relay-shaped, the ``connectors`` catalog
    publishes a client id, and this box has relay credentials to sign with.
    Never raises — a half-configured relay (URL without a credential) reads as
    "no official client" here; the connect click will say so only if the user
    has no BYO fallback either.
    """
    if not relay_capable(provider):
        return None
    try:
        from olano.catalog.connectors_registry import official_client

        client_id = official_client(provider.id)
    except Exception:
        return None
    if not client_id:
        return None
    try:
        from olano.core.connect_relay import resolve_connect_relay_config

        if resolve_connect_relay_config() is None:
            return None
    except Exception:  # RuntimeError: URL set but no credential — unusable
        return None
    return client_id


def byo_client_available(provider: ConnectorProvider) -> bool:
    """True when a bring-your-own OAuth client is configured for *provider*.

    The vault/env half of :func:`_resolve_client_creds` (a request body cannot
    be known ahead of a click), as a probe: never raises, never touches the
    official floor. What the integration finder and ``connect_methods`` ask.
    """
    try:
        from olano.tools._credentials import get_credential

        raw = get_credential(provider.creds_vault_key)
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("client_id") and parsed.get("client_secret"):
                return True
        if provider.client_env_pair:
            id_key, secret_key = provider.client_env_pair
            return bool(get_credential(id_key) and get_credential(secret_key))
    except Exception:
        logger.debug("byo_client_available(%s) failed", provider.id, exc_info=True)
    return False


def connect_methods(provider: ConnectorProvider) -> dict[str, Any]:
    """Which ways of connecting *provider* are usable on this box, right now.

    ``official_oauth`` — the catalog publishes an Olano app and the relay is
    configured; ``byo_oauth`` — the operator stored their own client;
    ``device`` — the provider declares a device-code recipe; ``key`` — the
    credential names a pasted key may land under (empty list = none). The
    finder ranks them official > byo > device > key; the magic-link router
    picks the first available one when no method is named.
    """
    return {
        "official_oauth": _official_client(provider) is not None,
        "byo_oauth": byo_client_available(provider),
        "device": provider.device is not None,
        "key": list(provider.exposes),
    }


def _relay_params(provider: ConnectorProvider, params: dict[str, str]) -> dict[str, str]:
    """The subset of connect params the relay's exchange needs.

    Exactly the provider ``token_url``'s template fields (tenant, domain, …).
    Box-only params — QuickBooks' ``environment`` is an API-host selector, not
    a token-URL field — stay here: the relay refuses unknown keys by design,
    so sending everything would 400 the registration.
    """
    import string

    fields = {f for _, f, _, _ in string.Formatter().parse(provider.token_url) if f}
    return {k: v for k, v in params.items() if k in fields}


def _resolve_client_creds(
    provider: ConnectorProvider, creds: dict[str, Any] | None
) -> tuple[str, str, dict[str, str], str]:
    """Resolve (client_id, client_secret, params, source) for a connect attempt.

    Order: explicit ``creds`` from the request body → vault/env JSON blob under
    ``provider.creds_vault_key`` → the ``provider.client_env_pair`` credential
    names → the **official client floor** (catalog-published Olano app, relay
    mode — engaged only when every BYO tier came up empty, so a user's own
    client always wins). ``source`` names the tier that supplied the client id:
    ``body`` | ``vault`` | ``env`` | ``official``. Connect-time params (shop,
    tenant, …) come from the request body, fall back to the JSON blob, then to
    ``optional_params`` defaults.
    """
    from olano.tools._credentials import get_credential

    body = {k: str(v).strip() for k, v in (creds or {}).items() if str(v).strip()}
    blob: dict[str, str] = {}
    raw = get_credential(provider.creds_vault_key)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                blob = {k: str(v) for k, v in parsed.items()}
        except ValueError:
            pass

    source = "body" if body.get("client_id") else "vault" if blob.get("client_id") else ""
    client_id = body.get("client_id") or blob.get("client_id") or ""
    client_secret = body.get("client_secret") or blob.get("client_secret") or ""
    if (not client_id or not client_secret) and provider.client_env_pair:
        id_key, secret_key = provider.client_env_pair
        if not client_id:
            client_id = get_credential(id_key)
            source = "env" if client_id else source
        client_secret = client_secret or get_credential(secret_key)
    if not client_id:
        official = _official_client(provider)
        if official:
            client_id, client_secret, source = official, "", "official"
    if not client_id:
        hint = ""
        if relay_capable(provider):
            hint = " (No official Olano app is available for this provider on this deployment yet.)"
        raise ValueError(
            f"{provider.name}: no OAuth client configured. Enter a Client ID/Secret "
            f"in the connect panel or store them in the vault under "
            f"{provider.creds_vault_key} (JSON with client_id/client_secret).{hint}"
        )

    params: dict[str, str] = dict(provider.optional_params)
    for key in (*provider.required_params, *provider.optional_params):
        value = body.get(key) or blob.get(key)
        if value:
            params[key] = value
    if provider.normalize_params:
        params = provider.normalize_params(params)
    missing = [key for key in provider.required_params if not params.get(key)]
    if missing:
        raise ValueError(f"{provider.name}: missing required field(s): {', '.join(missing)}")
    for key, value in params.items():
        if not _PARAM_RE.match(value):
            raise ValueError(f"{provider.name}: invalid value for '{key}'.")
    return client_id, client_secret, params, source


# ── Flow: start / poll / callback ──────────────────────────────────────────


def start_connector_auth(
    provider: ConnectorProvider,
    *,
    creds: dict[str, Any] | None,
    agent: str | None,
    shared: bool,
    redirect_uri: str,
) -> OAuthSession:
    """Begin a headless authorization-code flow for ``provider``.

    Builds the authorization URL (no browser is opened server-side) and
    returns a session the dashboard polls. ``redirect_uri`` is the dashboard's
    ``/api/auth/<service>/callback`` and must be registered on the OAuth app —
    **unless** the client resolves to the official Olano app, in which case the
    flow runs in relay mode: the passed ``redirect_uri`` is ignored in favour
    of the central relay's callback (the one URI registered on the official
    app), and a background pump collects the exchanged tokens from the relay.
    """
    _prune_sessions()
    client_id, client_secret, params, source = _resolve_client_creds(provider, creds)

    auth_mode = "direct"
    relay_cfg = None
    if source == "official":
        from olano.core import connect_relay

        try:
            relay_cfg = connect_relay.resolve_connect_relay_config()
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        if relay_cfg is None:  # pragma: no cover — _official_client checked this
            raise ValueError(f"{provider.name}: no auth relay is configured.")
        auth_mode = "relay"
        redirect_uri = connect_relay.connect_callback_uri(relay_cfg)

    state = uuid.uuid4().hex
    query: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        **provider.extra_auth_params,
    }
    if provider.scopes:
        query["scope"] = provider.scope_separator.join(provider.scopes)

    code_verifier: str | None = None
    if provider.pkce:
        code_verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        query["code_challenge"] = challenge
        query["code_challenge_method"] = "S256"

    authorize_base = provider.authorize_url.format(**params)
    auth_url = f"{authorize_base}{'&' if '?' in authorize_base else '?'}{urlencode(query)}"

    if auth_mode == "relay":
        # Announce the state before handing out the URL: an unregistered state
        # would send the user through a consent screen whose callback the relay
        # then drops. A refusal here is actionable (the BYO path still works).
        from olano.core import connect_relay

        assert relay_cfg is not None  # set whenever auth_mode flipped to "relay" above
        try:
            connect_relay.register_connect_state(
                relay_cfg,
                state,
                provider=provider.id,
                code_verifier=code_verifier,
                params=_relay_params(provider, params),
            )
        except Exception as exc:
            raise ValueError(
                f"{provider.name}: could not start the official connection "
                f"({exc}). Retry, or use your own OAuth app instead."
            ) from exc

    session = OAuthSession(
        service=provider.id,
        session_id=uuid.uuid4().hex,
        agent=agent,
        shared=shared,
        token_path=_token_path_for(provider.id, agent),
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
        params=params,
        state=state,
        code_verifier=code_verifier,
        authorization_url=auth_url,
        created_at=time.time(),
        auth_mode=auth_mode,
        client_source=source if source == "official" else "byo",
    )
    _SESSIONS[session.session_id] = session
    _BY_STATE[state] = session.session_id
    if auth_mode == "relay":
        threading.Thread(
            target=_relay_pump,
            args=(provider, session),
            name=f"connector-relay-pump-{provider.id}",
            daemon=True,
        ).start()
        session.log("info", "Authorization URL ready — connecting via Olano's app.")
    else:
        session.log("info", "Authorization URL ready — open it to grant access.")
    return session


def poll_connector_auth(session_id: str) -> OAuthSession | None:
    return _SESSIONS.get(session_id)


def _build_token_request(
    provider: ConnectorProvider, session: OAuthSession, code: str
) -> tuple[str, dict[str, str], dict[str, str] | None, tuple[str, str] | None]:
    """Default authorization-code exchange request → (url, data, headers, auth)."""
    if provider.token_request:
        return provider.token_request(provider, session, code)
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": session.redirect_uri,
        "client_id": session.client_id,
    }
    if session.code_verifier:
        data["code_verifier"] = session.code_verifier
    auth: tuple[str, str] | None = None
    if provider.client_auth == "basic":
        auth = (session.client_id, session.client_secret)
        data.pop("client_id", None)
    elif session.client_secret:
        data["client_secret"] = session.client_secret
    url = provider.token_url.format(**session.params)
    return url, data, dict(provider.token_headers) or None, auth


def _record_from_token_response(
    provider: ConnectorProvider, session: OAuthSession, payload: dict[str, Any]
) -> dict[str, Any]:
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"Token response had no access_token: {json.dumps(payload)[:300]}")
    now = time.time()
    expires_at: float | None
    expires_in = payload.get("expires_in")
    if expires_in is not None:
        expires_at = now + float(expires_in)
    elif provider.expires_default is not None:
        expires_at = now + provider.expires_default
    else:
        expires_at = None
    record: dict[str, Any] = {
        "service": provider.id,
        "access_token": access_token,
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": expires_at,
        "scopes": list(provider.scopes),
        # Relay-mode records carry an empty client_secret — the official
        # secret never reaches this box; refresh routes through the relay.
        "client_id": session.client_id,
        "client_secret": session.client_secret,
        "params": dict(session.params),
        "extras": {},
        "obtained_at": now,
        "auth_mode": session.auth_mode,
        "client_source": session.client_source,
    }
    if payload.get("refresh_token"):
        record["refresh_token"] = payload["refresh_token"]
    for key in provider.token_extras:
        if payload.get(key) is not None:
            record["extras"][key] = payload[key]
    return record


def _finalize_from_payload(
    provider: ConnectorProvider,
    session: OAuthSession,
    payload: dict[str, Any],
    query: dict[str, str] | None,
) -> None:
    """Token payload → saved record: the shared tail of both exchange paths.

    Extracted so relay-mode finalization (bundle from the pump) and the on-box
    callback run callback_extras / post_exchange / save byte-identically —
    QuickBooks' ``realmId`` capture and DocuSign's userinfo hook must not care
    which side did the code exchange.
    """
    record = _record_from_token_response(provider, session, payload)
    for key in provider.callback_extras:
        value = (query or {}).get(key)
        if value is not None:
            record["extras"][key] = value
    if provider.post_exchange:
        provider.post_exchange(provider, record)
    _save_record(record, session.token_path)
    if session.agent and session.shared:
        set_connector_shared(provider.id, session.agent, True)
    session.status = "authorized"
    session.log("info", f"{provider.name} connected — token saved to {session.token_path}")


def resolve_connector_callback(
    provider: ConnectorProvider,
    state: str | None,
    code: str | None,
    error: str | None = None,
    query: dict[str, str] | None = None,
) -> OAuthSession | None:
    """Feed the OAuth redirect back into the waiting flow (matched by state).

    Exchanges the code for tokens, runs provider hooks, and saves the record.
    Returns the session for the API's status page; ``None`` if nothing matches.
    """
    if not state:
        return None
    sid = _BY_STATE.get(state)
    if not sid:
        return None
    session = _SESSIONS.get(sid)
    if session is None or session.service != provider.id:
        return None
    if session.status in ("authorized", "error"):
        return session
    if session.auth_mode == "relay":
        # Relay flows redirect to the central callback, never here; a stray hit
        # (stale browser tab, misregistered app) must not consume the state or
        # attempt an exchange this box has no secret for. The pump owns it.
        session.log("info", "Ignoring on-box callback for a relay-mode flow.")
        return session

    if error:
        session.status = "error"
        session.error = error
        session.log("error", f"Authorization failed: {error}")
        _BY_STATE.pop(state, None)
        return session
    if not code:
        session.status = "error"
        session.error = "No authorization code was returned."
        session.log("error", session.error)
        _BY_STATE.pop(state, None)
        return session

    try:
        session.status = "exchanging"
        session.log("info", "Authorization code received — exchanging for tokens…")
        url, data, headers, auth = _build_token_request(provider, session, code)
        if provider.token_method == "get":
            import httpx

            resp = httpx.get(url, params=data, headers=headers, timeout=30)
            if resp.status_code >= 400:
                raise RuntimeError(f"Token endpoint returned {resp.status_code}: {resp.text[:500]}")
            payload = dict(resp.json())
        else:
            payload = _post_token(url, data, headers=headers, auth=auth)
        _finalize_from_payload(provider, session, payload, query)
    except Exception as exc:
        session.status = "error"
        session.error = str(exc)
        session.log("error", f"Token exchange failed: {exc}")
    finally:
        _BY_STATE.pop(state, None)
    return session


def _relay_pump(provider: ConnectorProvider, session: OAuthSession) -> None:
    """Poll the relay for a relay-mode session's token bundle (daemon thread).

    The central callback performs the exchange the moment the user consents;
    this pump ships the sealed result home and finalizes exactly like the
    on-box callback would. Network blips keep polling — the flow's own
    ``_SESSION_MAX_AGE_S`` bounds the wait — while ``unknown`` (the relay lost
    the state: restart, TTL expiry) fails fast after a short grace.
    """
    from olano.core import connect_relay

    try:
        cfg = connect_relay.resolve_connect_relay_config()
    except Exception:
        cfg = None
    if cfg is None or not session.state:  # pragma: no cover — start() guaranteed both
        session.status = "error"
        session.error = "The auth relay became unavailable."
        return

    unknown_streak = 0
    deadline = session.created_at + _SESSION_MAX_AGE_S
    while time.time() < deadline:
        if session.status in ("authorized", "error"):
            return
        try:
            result = connect_relay.poll_connect_bundle(cfg, session.state)
        except Exception:
            time.sleep(_RELAY_POLL_S)
            continue
        status = result.get("status")
        if status == "ready":
            bundle = result.get("bundle") or {}
            try:
                session.status = "exchanging"
                session.log("info", "Authorization received — saving tokens…")
                _finalize_from_payload(
                    provider,
                    session,
                    dict(bundle.get("token_response") or {}),
                    {str(k): str(v) for k, v in (bundle.get("callback_query") or {}).items()},
                )
            except Exception as exc:
                session.status = "error"
                session.error = str(exc)
                session.log("error", f"Finalizing the connection failed: {exc}")
            finally:
                _BY_STATE.pop(session.state, None)
            return
        if status == "error":
            session.status = "error"
            session.error = str(result.get("error") or "Authorization failed.")
            session.log("error", f"Authorization failed: {session.error}")
            _BY_STATE.pop(session.state, None)
            return
        if status == "unknown":
            unknown_streak += 1
            if unknown_streak >= _RELAY_UNKNOWN_LIMIT:
                session.status = "error"
                session.error = "The auth relay no longer knows this authorization — start again."
                session.log("error", session.error)
                _BY_STATE.pop(session.state, None)
                return
        else:
            unknown_streak = 0
        time.sleep(_RELAY_POLL_S)
    if session.status not in ("authorized", "error"):
        session.status = "error"
        session.error = "Authorization timed out — start again."
        session.log("error", session.error)
        _BY_STATE.pop(session.state, None)


# ── Status / disconnect (same shapes as google_auth_status) ────────────────


def _is_expired(record: dict[str, Any], margin: float = 0.0) -> bool:
    expires_at = record.get("expires_at")
    return expires_at is not None and time.time() > float(expires_at) - margin


def connector_auth_status(provider: ConnectorProvider, agent: str | None = None) -> dict:
    """Connection status for one service at one scope (mirrors Google's dict)."""
    paths = get_olano_paths()
    if agent is None:
        token_path = paths.connector_token_path(provider.id)
        scope = "system"
        shared: bool | None = None
    else:
        own = paths.agent_connector_token_path(agent, provider.id)
        if own.exists():
            token_path, scope, shared = own, "agent", is_connector_shared(provider.id, agent)
        else:
            fallback = _find_shared_token(provider.id, exclude=agent)
            if fallback:
                token_path, scope, shared = fallback, "shared", True
            else:
                system = paths.connector_token_path(provider.id)
                if system.exists():
                    token_path, scope, shared = system, "system", None
                else:
                    return {
                        "connected": False,
                        "scope": "none",
                        "token_path": None,
                        "shared": None,
                        "methods": auth_methods(provider),
                    }

    record = _load_record(token_path)
    if record is None:
        return {
            "connected": False,
            "scope": scope,
            "token_path": str(token_path),
            "shared": shared,
            "methods": auth_methods(provider),
        }
    refreshable = bool(record.get("refresh_token")) and provider.supports_refresh
    connected = bool(record.get("access_token")) and (not _is_expired(record) or refreshable)
    failure = auth_failure(record)
    if failure is not None:
        # The service refused this token after a refresh attempt (or with no
        # refresh grant to try): a "Connected" badge here would be the stale
        # one the mark exists to retire. A new sign-in replaces the record.
        connected = False
    status = {
        "connected": connected,
        "scope": scope,
        "token_path": str(token_path),
        "shared": shared,
        "expires_at": record.get("expires_at"),
        # "official" = connected via Olano's app (relay refresh); "byo"
        # otherwise — pre-fork records have no field and are BYO by definition.
        # "public" = a device-code sign-in with the provider's public client.
        "client_source": str(record.get("client_source") or "byo"),
        # "direct" | "relay" | "device" — how the token was obtained.
        "auth_mode": str(record.get("auth_mode") or "direct"),
        "methods": auth_methods(provider),
    }
    if failure is not None:
        status["needs_reauth"] = True
        status["auth_error"] = failure["error"]
        status["auth_failed_at"] = failure["failed_at"]
    account = (record.get("extras") or {}).get("account")
    if account:
        status["account"] = str(account)
    return status


def disconnect_connector(provider: ConnectorProvider, agent: str | None = None) -> bool:
    """Delete the token for the given scope. Returns True if a file was removed.

    Also drops the agent's connections-meta entry and any in-flight OAuth
    session for this scope, so a reconnect starts from a clean slate instead of
    inheriting the disconnected token's ``shared`` flag or resuming a half-done
    authorization against it.
    """
    token_path = _token_path_for(provider.id, agent)
    _RECORD_CACHE.pop(str(token_path), None)
    if agent:
        clear_connector_meta(provider.id, agent)
    from olano.core.connectors.device import drop_sessions_for as _drop_device_sessions

    _drop_device_sessions(provider.id, agent)
    _drop_sessions_for(provider.id, agent)
    if token_path.exists():
        token_path.unlink()
        return True
    return False


def _drop_sessions_for(service: str, agent: str | None) -> None:
    """Forget pending OAuth sessions targeting this service+scope."""
    for sid, sess in list(_SESSIONS.items()):
        if sess.service != service or (sess.agent or None) != agent:
            continue
        _SESSIONS.pop(sid, None)
        if sess.state:
            _BY_STATE.pop(sess.state, None)


# ── Live token access (powers the credential resolver) ─────────────────────


def _apply_refresh_payload(
    provider: ConnectorProvider, latest: dict[str, Any], payload: dict[str, Any], path: Path
) -> dict[str, Any]:
    """Fold a refresh response into the record and save it — both refresh paths.

    Shared verbatim by the direct provider POST and the relay refresh, so
    rotation handling (some providers rotate the refresh token), the
    Salesforce-style ``expires_in``-less re-arm, and token_extras capture can
    never diverge between the two.
    """
    latest = dict(latest)
    latest["access_token"] = payload.get("access_token", latest["access_token"])
    if payload.get("refresh_token"):  # some providers rotate it
        latest["refresh_token"] = payload["refresh_token"]
    expires_in = payload.get("expires_in")
    if expires_in is not None:
        latest["expires_at"] = time.time() + float(expires_in)
    elif provider.expires_default is not None:
        # Some providers (Salesforce) omit expires_in on refresh; re-arm
        # from the assumed lifetime or the record would refresh every call.
        latest["expires_at"] = time.time() + provider.expires_default
    latest["obtained_at"] = time.time()
    for key in provider.token_extras:
        if payload.get(key) is not None:
            latest.setdefault("extras", {})[key] = payload[key]
    # The provider honoured the grant, so any earlier refusal is history.
    latest.pop(_AUTH_FAILED_AT, None)
    latest.pop(_AUTH_ERROR, None)
    _save_record(latest, path)
    return latest


def _refresh_once(
    provider: ConnectorProvider, latest: dict[str, Any], path: Path
) -> tuple[dict[str, Any] | None, str]:
    """One refresh round-trip for *latest*: ``(fresh record, "")`` or ``(None, why)``.

    The single body behind :func:`_refresh_record` (near expiry) and
    :func:`refresh_record_now` (on a toolkit's refusal report), so the relay
    fork, client-auth style and the Salesforce re-arm can never diverge
    between the two. Caller holds the record's refresh lock.
    """
    if str(latest.get("auth_mode") or "") == "relay":
        from olano.core import connect_relay

        try:
            cfg = connect_relay.resolve_connect_relay_config()
            if cfg is None:
                raise RuntimeError("no auth relay configured")
            payload = connect_relay.refresh_via_relay(
                cfg,
                provider.id,
                str(latest.get("refresh_token") or ""),
                params=_relay_params(provider, dict(latest.get("params") or {})),
            )
        except Exception as exc:
            logger.warning("connector[%s]: relay token refresh failed: %s", provider.id, exc)
            return None, f"relay token refresh failed: {exc}"
        return _apply_refresh_payload(provider, latest, payload, path), ""
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": str(latest.get("refresh_token") or ""),
    }
    if provider.refresh_includes_scope and provider.scopes:
        data["scope"] = provider.scope_separator.join(provider.scopes)
    client_id = str(latest.get("client_id") or "")
    client_secret = str(latest.get("client_secret") or "")
    auth: tuple[str, str] | None = None
    if provider.client_auth == "basic":
        auth = (client_id, client_secret)
    else:
        data["client_id"] = client_id
        if client_secret:
            data["client_secret"] = client_secret
    url = provider.token_url.format(**latest.get("params", {}))
    try:
        payload = _post_token(url, data, headers=dict(provider.token_headers) or None, auth=auth)
    except Exception as exc:
        logger.warning("connector[%s]: token refresh failed: %s", provider.id, exc)
        return None, f"token refresh failed: {exc}"
    return _apply_refresh_payload(provider, latest, payload, path), ""


def _refresh_record(
    provider: ConnectorProvider, record: dict[str, Any], path: Path
) -> dict[str, Any] | None:
    """Refresh an expiring record in place on disk; returns the fresh record.

    On failure: returns the stale record while it is still technically valid,
    else ``None`` (credential resolution then falls through to vault/env).
    Relay-mode records (``auth_mode == "relay"``) refresh **through the
    relay** — this box holds no client secret for them — with identical
    stale-while-valid semantics; direct records are untouched by the fork.
    """
    refresh_token = record.get("refresh_token")
    if not refresh_token or not provider.supports_refresh:
        return record if not _is_expired(record) else None

    with _refresh_lock(path):
        # Another thread may have refreshed while we waited on the lock.
        latest = _load_record(path) or record
        if not _is_expired(latest, margin=_REFRESH_MARGIN_S):
            return latest
        if not latest.get("refresh_token"):
            latest = {**latest, "refresh_token": refresh_token}
        fresh, _why = _refresh_once(provider, latest, path)
        if fresh is None:
            return latest if not _is_expired(latest) else None
        return fresh


def refresh_record_now(
    provider: ConnectorProvider, agent: str | None = None
) -> tuple[dict[str, Any] | None, str]:
    """Refresh the record a toolkit just had refused, regardless of its expiry.

    The toolkit's first move on a 401: the file may say the token has an hour
    left while the service has already dropped the session, and when the grant
    itself is still alive one refresh repairs the connection with nobody
    involved. Returns ``(fresh record, "")`` on success and ``(None, why)``
    otherwise; the caller retries its request with the fresh token and reports
    through :func:`mark_auth_failed` when that is refused too.

    A record marked by :func:`mark_auth_failed` within
    :data:`_FORCED_REFRESH_COOLDOWN_S` is answered from the mark without a
    token-endpoint round-trip, so a retry loop costs the provider nothing.
    A refusal from the token endpoint itself (a 4xx: ``invalid_grant``, a
    revoked refresh token) is definitive and marks the record here, since
    nothing short of a new sign-in can follow it; a transport failure is not
    and leaves the record alone.
    """
    path = _resolve_token_path(provider.id, agent)
    record = _load_record(path)
    if record is None:
        return None, f"no {provider.name} connection is stored at this scope"
    with _refresh_lock(path):
        latest = _load_record(path) or record
        failed_at = latest.get(_AUTH_FAILED_AT)
        if failed_at is not None and time.time() - float(failed_at) < _FORCED_REFRESH_COOLDOWN_S:
            return None, str(latest.get(_AUTH_ERROR) or "the connection was refused a moment ago")
        if not latest.get("refresh_token") or not provider.supports_refresh:
            return None, (
                f"{provider.name} issued no refresh token for this connection, so it cannot "
                "be renewed without signing in again"
            )
        fresh, why = _refresh_once(provider, latest, path)
        if fresh is None:
            if _is_token_endpoint_refusal(why):
                _mark_locked(latest, path, why)
            return None, why
        return fresh, ""


def _is_token_endpoint_refusal(why: str) -> bool:
    """A 4xx from the token endpoint (RFC 6749 §5.2 spells a refusal that way)."""
    return "Token endpoint returned 4" in why or "invalid_grant" in why


def _mark_locked(latest: dict[str, Any], path: Path, error: str) -> dict[str, Any]:
    marked = dict(latest)
    marked[_AUTH_FAILED_AT] = time.time()
    marked[_AUTH_ERROR] = " ".join(str(error).split())[:_AUTH_ERROR_MAX_CHARS]
    _save_record(marked, path)
    return marked


def mark_auth_failed(
    provider: ConnectorProvider, agent: str | None = None, *, error: str
) -> dict[str, Any] | None:
    """Record that the service refuses this connection's token even after a refresh.

    Makes the Connections tab honest, ``connected: False`` + ``needs_reauth``
    with *error* as the reason, without deleting anything: the record still
    resolves (a spurious refusal must not strand a working token), and the next
    honoured refresh or a fresh sign-in clears the mark. Returns the marked
    record, or ``None`` when no record exists at the scope the agent resolves.
    """
    path = _resolve_token_path(provider.id, agent)
    with _refresh_lock(path):
        latest = _load_record(path)
        if latest is None:
            return None
        return _mark_locked(latest, path, error)


def auth_failure(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The mark on *record*, as ``{"failed_at", "error"}``, or ``None`` when unmarked."""
    if not record or record.get(_AUTH_FAILED_AT) is None:
        return None
    return {
        "failed_at": float(record[_AUTH_FAILED_AT]),
        "error": str(record.get(_AUTH_ERROR) or ""),
    }


def get_live_record(provider: ConnectorProvider, agent: str | None = None) -> dict[str, Any] | None:
    """Load the (own → shared → system) record, refreshing it when near expiry."""
    path = _resolve_token_path(provider.id, agent)
    record = _load_record(path)
    if record is None:
        return None
    if _is_expired(record, margin=_REFRESH_MARGIN_S):
        return _refresh_record(provider, record, path)
    return record


# Top-level record fields a provider may expose. client_id/client_secret and
# refresh_token exist for toolkits that drive their own OAuth client (praw
# needs all three to mint user-context tokens natively).
_EXPOSABLE_TOP_LEVEL = frozenset({"access_token", "refresh_token", "client_id", "client_secret"})


def extract_exposed_field(record: dict[str, Any], field_path: str) -> str | None:
    """Pull an exposed value out of a record.

    ``field_path`` is one of the top-level fields in ``_EXPOSABLE_TOP_LEVEL``,
    or ``extras.<key>`` / ``params.<key>``.
    """
    if field_path in _EXPOSABLE_TOP_LEVEL:
        value = record.get(field_path)
    elif field_path.startswith("extras."):
        value = record.get("extras", {}).get(field_path[len("extras.") :])
    elif field_path.startswith("params."):
        value = record.get("params", {}).get(field_path[len("params.") :])
    else:  # pragma: no cover — provider misconfiguration
        value = None
    return str(value) if value else None
