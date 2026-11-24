"""Olano's own OAuth 2.1 authorization server for the inbound MCP and A2A endpoints.

Why it exists: an OAuth-only client (a Claude.ai custom connector, an A2A
client following the card's ``oauth2`` scheme) cannot paste a bearer token. It
expects the standard dance — discover the authorization server from the
resource metadata, register itself (RFC 7591), send the person to an
``/authorize`` page, and trade the code for tokens at ``/token``. With no
external identity provider on the box, Olano has to *be* that server.

What the ``mcp`` SDK supplies (reused, not rewritten): the whole HTTP surface —
RFC 8414 metadata, ``/authorize`` request validation (response_type, PKCE
challenge, registered redirect URI, registered scopes), ``/register`` (RFC 7591
DCR), ``/token`` (client authentication, code + PKCE S256 verification, refresh
grant, scope narrowing) and ``/revoke`` (RFC 7009) — driven through the
:class:`OAuthAuthorizationServerProvider` protocol this module implements.

What Olano decides:

* **Who consents.** ``authorize`` never grants anything itself: it parks the
  request and sends the browser to the dashboard's consent page, where an
  **admin** (owner on a single-user install) picks which agents the client may
  reach. Only a dashboard session can answer that page — the MCP client's
  bearer token is not a credential here.
* **What is issued.** Access tokens are ordinary records in the shared token
  store (``kind: "oauth"``, short-lived, bound to the client and the consenting
  admin), so they show up in the same list as operator-minted tokens and are
  revoked the same way; refresh tokens rotate on every use.
* **Where it lives.** Under ``/api/oauth`` so every deployment shape's
  ``/api/*`` proxy rule reaches it, with the metadata document served at the
  RFC 8414 path-insertion location *and* the path-appending fallbacks MCP
  clients try.

Persisted: registered clients (``<home>/mcp_server/oauth_clients.json``, 0600 —
client secrets are compared in plaintext by the SDK, and a secret alone grants
nothing without a consented code) and tokens. In memory only: pending consents
(10 min) and unredeemed codes (5 min) — a runtime restart simply asks the client
to start its sign-in again.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

from olano.config.home import get_olano_paths
from olano.config.system_settings import ExternalAccessSettings
from olano.mcp_server.tokens import ALL_AGENTS, McpTokenStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.routing import Route

logger = logging.getLogger(__name__)

OAUTH_PREFIX = "/api/oauth"
CONSENT_ROUTE = "/oauth/consent"  # dashboard SPA route the browser is sent to
PENDING_TTL_SECONDS = 600
CODE_TTL_SECONDS = 300
# Every path ``build_routes`` serves under the prefix, spelled once. The app
# routes exactly these to the authorization server (server.py), never a
# ``/api/oauth/{rest:path}`` catch-all: the dashboard's consent API shares the
# prefix (``/api/oauth/consent/{id}``, routes.py, registered later), and a
# catch-all ahead of it handed the consent lookup and the Allow click to this
# router, which has no such route. Its bare 404 read as "unknown or expired"
# on the consent page, so no OAuth sign-in could ever be approved.
AUTHORIZE_PATH = f"{OAUTH_PREFIX}/authorize"
TOKEN_PATH = f"{OAUTH_PREFIX}/token"
REGISTER_PATH = f"{OAUTH_PREFIX}/register"
REVOKE_PATH = f"{OAUTH_PREFIX}/revoke"
METADATA_PATHS = (
    # Path appending: reachable through every /api/* proxy rule, and the MCP
    # spec has clients fall back to these.
    f"{OAUTH_PREFIX}/.well-known/oauth-authorization-server",
    f"{OAUTH_PREFIX}/.well-known/openid-configuration",
)
ENDPOINT_PATHS = (AUTHORIZE_PATH, TOKEN_PATH, REGISTER_PATH, REVOKE_PATH, *METADATA_PATHS)
# How a client may authenticate at /token and /revoke, as advertised in the
# metadata: every method `_presented_client_secret` accepts, `none` included.
CLIENT_AUTH_METHODS = ("client_secret_post", "client_secret_basic", "none")
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_SCOPE_RE = re.compile(r"^(agents:\*|agent:[A-Za-z0-9][A-Za-z0-9_.-]{0,63})$")


def agents_from_scopes(scopes: list[str] | None) -> list[str]:
    """``agent:<name>`` / ``agents:*`` scopes → agent names (``*`` for all)."""
    out: list[str] = []
    for scope in scopes or []:
        if scope == "agents:*":
            out.append(ALL_AGENTS)
        elif scope.startswith("agent:"):
            out.append(scope[len("agent:") :])
    return sorted(set(out))


def scopes_for_agents(agents: list[str]) -> list[str]:
    return [f"agent:{a}" if a != ALL_AGENTS else "agents:*" for a in agents]


# ── Registered clients ─────────────────────────────────────────────────────────


class OAuthClientStore:
    """RFC 7591 client registrations, one JSON file, mtime-cached like the token store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._stat: tuple[int, int] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._cache, self._stat = {}, None
            return self._cache
        sig = (st.st_mtime_ns, st.st_size)
        if self._stat == sig:
            return self._cache
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            logger.warning("OAuth client store unreadable at %s — treating as empty", self.path)
            raw = {}
        rows = raw.get("clients", []) if isinstance(raw, dict) else []
        self._cache = {
            str(r.get("client_id")): r for r in rows if isinstance(r, dict) and r.get("client_id")
        }
        self._stat = sig
        # A file written before secrets were hashed holds them as issued: digest
        # them now and rewrite, so the plaintext stops existing on first read.
        legacy = [
            r
            for r in self._cache.values()
            if r.get("client_secret") and not is_hashed_client_secret(r["client_secret"])
        ]
        if legacy:
            for row in legacy:
                row["client_secret"] = hash_client_secret(str(row["client_secret"]))
            logger.info(
                "OAuth client store: hashed %d legacy plaintext client secret(s)", len(legacy)
            )
            self._save()
        return self._cache

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from olano.security.private_files import write_private_text

        # Owner-only from the first byte: hashed client secrets.
        write_private_text(
            self.path,
            json.dumps({"version": 1, "clients": list(self._cache.values())}, indent=2),
        )
        self._stat = None

    def save(self, info: OAuthClientInformationFull, *, registered_by: str = "dcr") -> None:
        with self._lock:
            self._load()
            row = info.model_dump(mode="json", exclude_none=True)
            secret = row.get("client_secret")
            if secret and not is_hashed_client_secret(secret):
                row["client_secret"] = hash_client_secret(str(secret))
            row["registered_at"] = int(time.time())
            row["registered_by"] = registered_by
            self._cache[str(info.client_id)] = row
            self._save()

    def get(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            row = self._load().get(client_id)
        if row is None:
            return None
        data = {k: v for k, v in row.items() if k not in ("registered_at", "registered_by")}
        try:
            return OAuthClientInformationFull.model_validate(data)
        except Exception:  # pragma: no cover - a hand-edited file
            logger.warning("OAuth client %s has an unreadable record", client_id)
            return None

    def list(self) -> list[dict[str, Any]]:
        """Client rows for listings — never the secret."""
        with self._lock:
            rows = list(self._load().values())
        out = []
        for row in sorted(rows, key=lambda r: r.get("registered_at", 0), reverse=True):
            public = {k: v for k, v in row.items() if k != "client_secret"}
            public["has_secret"] = bool(row.get("client_secret"))
            out.append(public)
        return out

    def delete(self, client_id: str) -> bool:
        with self._lock:
            self._load()
            if client_id not in self._cache:
                return False
            self._cache.pop(client_id)
            self._save()
            return True


def client_store_path() -> Path:
    return get_olano_paths().root / "mcp_server" / "oauth_clients.json"


# ── Client secrets at rest ────────────────────────────────────────────────────
#
# The authorization server only ever *verifies* a client secret — RFC 7591 hands
# it to the client once, at registration, and nothing shows it again — so the
# store keeps a digest, the way the token store keeps token digests. A fast hash
# is the right one here: these are 32-byte random strings the server generated,
# not passwords, so there is nothing for a slow KDF to protect against.

SECRET_HASH_PREFIX = "sha256:"


def hash_client_secret(raw: str) -> str:
    return SECRET_HASH_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_hashed_client_secret(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(SECRET_HASH_PREFIX)
        and len(value) == len(SECRET_HASH_PREFIX) + 64
    )


def client_secret_matches(stored: str | None, presented: str | None) -> bool:
    """Constant-time check of a presented secret against the stored digest.

    A legacy plaintext record (a file written before secrets were hashed) still
    verifies, so an upgrade never locks a client out — the store rewrites such a
    record as its digest on first load anyway.
    """
    if not stored or presented is None:
        return False
    expected = stored if is_hashed_client_secret(stored) else hash_client_secret(stored)
    return hmac.compare_digest(
        expected.encode("utf-8"), hash_client_secret(presented).encode("utf-8")
    )


def _presented_client_secret(
    auth_header: str, form_data: Any, client: OAuthClientInformationFull, client_id: str
) -> str | None:
    """Pull the secret out of the request the way the client registered to send
    it (RFC 6749 §2.3.1 HTTP Basic, ``client_secret_post``, or none) — a mirror of
    the SDK's ``ClientAuthenticator`` extraction, kept branch for branch."""
    method = client.token_endpoint_auth_method
    if method == "client_secret_basic":
        if not auth_header.startswith("Basic "):
            raise AuthenticationError(
                "Missing or invalid Basic authentication in Authorization header"
            )
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            if ":" not in decoded:
                raise ValueError("Invalid Basic auth format")
            basic_id, secret = decoded.split(":", 1)
            basic_id, secret = unquote(basic_id), unquote(secret)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise AuthenticationError("Invalid Basic authentication header") from None
        if basic_id != client_id:
            raise AuthenticationError("Client ID mismatch in Basic auth")
        return secret
    if method == "client_secret_post":
        raw = form_data.get("client_secret")
        return raw if isinstance(raw, str) else None
    if method == "none":
        return None
    raise AuthenticationError(f"Unsupported auth method: {method}")  # pragma: no cover


class HashedSecretClientAuthenticator(ClientAuthenticator):
    """The SDK authenticator compares the stored secret with the presented one
    in constant time but assumes the store holds plaintext. Ours holds digests,
    so the same extraction ends in a digest comparison. Wired into the token
    handler and the revocation endpoint in place of the SDK's default."""

    async def authenticate_request(self, request: Any) -> OAuthClientInformationFull:
        form_data = await request.form()
        client_id = form_data.get("client_id")
        if not client_id:
            raise AuthenticationError("Missing client_id")
        client = await self.provider.get_client(str(client_id))
        if not client:
            raise AuthenticationError("Invalid client_id")
        presented = _presented_client_secret(
            request.headers.get("Authorization", ""), form_data, client, str(client_id)
        )
        return self._check(client, presented)

    async def authenticate(
        self, client_id: str, client_secret: str | None
    ) -> OAuthClientInformationFull:
        client = await self.provider.get_client(client_id)
        if not client:
            raise AuthenticationError("Invalid client_id")
        return self._check(client, client_secret)

    @staticmethod
    def _check(
        client: OAuthClientInformationFull, presented: str | None
    ) -> OAuthClientInformationFull:
        if client.client_secret:
            if not presented:
                raise AuthenticationError("Client secret is required")
            if not client_secret_matches(client.client_secret, presented):
                raise AuthenticationError("Invalid client_secret")
            if client.client_secret_expires_at and client.client_secret_expires_at < int(
                time.time()
            ):
                raise AuthenticationError("Client secret has expired")
        return client


# ── In-memory flow state ───────────────────────────────────────────────────────


@dataclass
class PendingAuthorization:
    id: str
    client: OAuthClientInformationFull
    params: AuthorizationParams
    created_at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > PENDING_TTL_SECONDS

    @property
    def client_name(self) -> str:
        return str(self.client.client_name or "").strip() or str(self.client.client_id)

    @property
    def requested_agents(self) -> list[str]:
        return agents_from_scopes(self.params.scopes)


class OlanoAuthorizationCode(AuthorizationCode):
    agents: list[str] = []
    client_name: str = ""


# ── The provider ───────────────────────────────────────────────────────────────


class OlanoAuthorizationServer:
    """``OAuthAuthorizationServerProvider`` over the shared token store + a consent step."""

    def __init__(
        self,
        *,
        origin: str,
        token_store: McpTokenStore,
        client_store: OAuthClientStore,
        settings_loader: Callable[[], ExternalAccessSettings],
        exposed_names: Callable[[], list[str]],
    ) -> None:
        self.origin = origin.rstrip("/")
        self.token_store = token_store
        self.client_store = client_store
        self._settings = settings_loader
        self._exposed_names = exposed_names
        self._pending: dict[str, PendingAuthorization] = {}
        self._codes: dict[str, OlanoAuthorizationCode] = {}
        self._lock = threading.Lock()

    # ── helpers ────────────────────────────────────────────────────────────
    @property
    def issuer(self) -> str:
        return f"{self.origin}{OAUTH_PREFIX}"

    def set_origin(self, origin: str) -> None:
        """Follow the deployment's public origin; pending consents survive."""
        self.origin = origin.rstrip("/")

    def valid_scopes(self) -> list[str]:
        return ["agents:*", *(f"agent:{n}" for n in sorted(self._exposed_names()))]

    def _sweep(self) -> None:
        now = time.monotonic()
        for rid, pend in list(self._pending.items()):
            if pend.expired:
                self._pending.pop(rid, None)
        for code, rec in list(self._codes.items()):
            if rec.expires_at < time.time() or now < 0:  # pragma: no branch
                self._codes.pop(code, None)

    # ── SDK provider protocol ──────────────────────────────────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.client_store.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        settings = self._settings()
        if not settings.oauth.dynamic_registration:
            raise RegistrationError(
                "invalid_client_metadata",
                "Dynamic client registration is off on this deployment; ask the operator to "
                "enable it under Config → External access.",
            )
        for uri in client_info.redirect_uris or []:
            parsed = urlparse(str(uri))
            if parsed.scheme == "http" and (parsed.hostname or "").lower() not in _LOOPBACK:
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"redirect_uri {uri} must use https:// (plain http is only allowed for "
                    "loopback addresses)",
                )
            if parsed.scheme in ("javascript", "data", "file"):
                raise RegistrationError("invalid_redirect_uri", f"redirect_uri {uri} is refused")
        self.client_store.save(client_info)
        logger.info(
            "OAuth: registered client %s (%s)", client_info.client_id, client_info.client_name
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        settings = self._settings()
        if not settings.oauth.enabled:
            raise AuthorizeError("temporarily_unavailable", "The authorization server is off.")
        for scope in params.scopes or []:
            if not _SCOPE_RE.match(scope):
                raise AuthorizeError("invalid_scope", f"Unknown scope {scope}")
        if params.resource:
            # RFC 8707: the client says which resource the token is for. Anything
            # that is not this deployment's MCP or A2A endpoint is refused now
            # rather than minting a token nobody can use.
            res = params.resource.rstrip("/")
            if not (res == f"{self.origin}/api/mcp" or res.startswith(f"{self.origin}/api/a2a/")):
                raise AuthorizeError(
                    "invalid_request",
                    f"resource {params.resource} is not served by this deployment",
                )
        with self._lock:
            self._sweep()
            request_id = secrets.token_urlsafe(24)
            self._pending[request_id] = PendingAuthorization(
                id=request_id, client=client, params=params
            )
        return f"{self.origin}{CONSENT_ROUTE}?request={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> OlanoAuthorizationCode | None:
        with self._lock:
            self._sweep()
            rec = self._codes.get(authorization_code)
        if rec is None or rec.client_id != client.client_id:
            return None
        return rec

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: OlanoAuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            # Single use: a replayed code must fail even inside its lifetime.
            if self._codes.pop(authorization_code.code, None) is None:
                raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(
            client_id=str(client.client_id),
            client_name=authorization_code.client_name,
            agents=list(authorization_code.agents),
            subject=authorization_code.subject or "",
            resource=authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        rec = self.token_store.verify_refresh(refresh_token)
        if rec is None or rec.client_id != client.client_id:
            return None
        expires_at = None
        if rec.expires_at:
            from datetime import datetime

            try:
                expires_at = int(datetime.fromisoformat(rec.expires_at).timestamp())
            except ValueError:  # pragma: no cover
                expires_at = None
        return RefreshToken(
            token=refresh_token,
            client_id=rec.client_id,
            scopes=rec.scope_list(),
            expires_at=expires_at,
            subject=rec.subject or None,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        rec = self.token_store.verify_refresh(refresh_token.token)
        if rec is None:
            raise TokenError("invalid_grant", "refresh token is no longer valid")
        # Rotation: the old refresh token and the access token it came with die now.
        self.token_store.revoke_refresh(rec.id)
        if rec.access_token_id:
            self.token_store.revoke(rec.access_token_id)
        agents = agents_from_scopes(scopes) or list(rec.agents)
        return self._issue(
            client_id=str(client.client_id),
            client_name=str(client.client_name or "").strip() or str(client.client_id),
            agents=agents,
            subject=rec.subject,
            resource=None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        rec = self.token_store.verify(token)
        if rec is None:
            return None
        return AccessToken(
            token=token,
            client_id=rec.label,
            scopes=rec.scope_list(),
            resource=f"{self.origin}/api/mcp",
            subject=rec.subject or None,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.token_store.revoke_by_hash(token.token)

    # ── issuing ────────────────────────────────────────────────────────────
    def _issue(
        self,
        *,
        client_id: str,
        client_name: str,
        agents: list[str],
        subject: str,
        resource: str | None,
    ) -> OAuthToken:
        settings = self._settings()
        if not agents:
            raise TokenError("invalid_scope", "no agents were granted")
        ttl = settings.oauth.access_token_minutes * 60
        raw, rec = self.token_store.mint(
            label=client_name or client_id,
            agents=agents,
            ttl_days=None,
            ttl_seconds=ttl,
            created_by=subject or "oauth",
            kind="oauth",
            client_id=client_id,
            subject=subject,
        )
        refresh_raw, _ = self.token_store.mint_refresh(
            client_id=client_id,
            agents=agents,
            ttl_days=settings.oauth.refresh_token_days,
            subject=subject,
            access_token_id=rec.id,
        )
        return OAuthToken(
            access_token=raw,
            token_type="Bearer",
            expires_in=ttl,
            scope=" ".join(rec.scope_list()),
            refresh_token=refresh_raw,
        )

    # ── consent API (dashboard session only) ──────────────────────────────
    def pending(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._sweep()
            pend = self._pending.get(request_id)
        if pend is None:
            return None
        exposed = sorted(self._exposed_names())
        requested = pend.requested_agents
        if ALL_AGENTS in requested:
            offered = exposed
        elif requested:
            offered = [a for a in exposed if a in requested]
        else:
            offered = exposed
        return {
            "request_id": pend.id,
            "client_id": str(pend.client.client_id),
            "client_name": pend.client_name,
            "client_uri": str(pend.client.client_uri) if pend.client.client_uri else None,
            "logo_uri": str(pend.client.logo_uri) if pend.client.logo_uri else None,
            "redirect_host": urlparse(str(pend.params.redirect_uri)).netloc,
            "requested_agents": requested,
            "requested_all": ALL_AGENTS in requested,
            "offered_agents": offered,
            "exposed_agents": exposed,
            "resource": pend.params.resource,
            "expires_in": max(0, int(PENDING_TTL_SECONDS - (time.monotonic() - pend.created_at))),
        }

    def approve(self, request_id: str, *, agents: list[str], subject: str) -> str:
        """Mint the code and return the redirect URL. ``agents`` is what the admin
        granted — a subset of the exposed agents, or ``["*"]``."""
        with self._lock:
            self._sweep()
            pend = self._pending.pop(request_id, None)
        if pend is None:
            raise KeyError(request_id)
        exposed = set(self._exposed_names())
        picked = sorted({a.strip() for a in agents if a.strip()})
        granted = [ALL_AGENTS] if ALL_AGENTS in picked else [a for a in picked if a in exposed]
        if not granted:
            raise ValueError("pick at least one exposed agent to grant")
        code = secrets.token_urlsafe(32)
        rec = OlanoAuthorizationCode(
            code=code,
            scopes=scopes_for_agents(granted),
            expires_at=time.time() + CODE_TTL_SECONDS,
            client_id=str(pend.client.client_id),
            code_challenge=pend.params.code_challenge,
            redirect_uri=pend.params.redirect_uri,
            redirect_uri_provided_explicitly=pend.params.redirect_uri_provided_explicitly,
            resource=pend.params.resource,
            subject=subject,
            agents=granted,
            client_name=pend.client_name,
        )
        with self._lock:
            self._codes[code] = rec
        logger.info(
            "OAuth: %s granted client %s access to %s",
            subject or "admin",
            pend.client_name,
            granted,
        )
        extra = {"code": code}
        if pend.params.state:
            extra["state"] = pend.params.state
        return construct_redirect_uri(str(pend.params.redirect_uri), **extra)

    def deny(self, request_id: str) -> str:
        with self._lock:
            pend = self._pending.pop(request_id, None)
        if pend is None:
            raise KeyError(request_id)
        extra = {"error": "access_denied", "error_description": "The administrator declined."}
        if pend.params.state:
            extra["state"] = pend.params.state
        return construct_redirect_uri(str(pend.params.redirect_uri), **extra)

    async def _revoke_endpoint(self, request: Any) -> Any:
        """RFC 7009 token revocation: authenticate the client the way the SDK
        does (secret required only when one was issued), then revoke whatever
        ``token`` is — access or refresh. Always 200 for an unknown token, per
        the RFC, so a caller learns nothing about tokens it does not hold."""
        from starlette.responses import JSONResponse, Response

        try:
            client = await HashedSecretClientAuthenticator(self).authenticate_request(request)
        except AuthenticationError as exc:
            return JSONResponse(
                {"error": "unauthorized_client", "error_description": exc.message}, status_code=401
            )
        form = await request.form()
        raw = str(form.get("token") or "")
        if not raw:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "token is required"},
                status_code=400,
            )
        # A client may only revoke its own tokens.
        access = self.token_store.verify(raw)
        refresh = self.token_store.verify_refresh(raw)
        owner = (access.client_id if access else None) or (refresh.client_id if refresh else None)
        if owner is not None and owner == client.client_id:
            self.token_store.revoke_by_hash(raw)
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    # ── routes ─────────────────────────────────────────────────────────────
    def build_routes(self) -> list[Route]:
        """The SDK's AS handlers re-rooted under ``/api/oauth`` plus the metadata
        document at every location a client may look for it.

        Assembled by hand rather than through ``create_auth_routes`` for one
        reason: that helper builds its own ``ClientAuthenticator``, which
        compares plaintext secrets, and the client store holds digests — so the
        token handler gets :class:`HashedSecretClientAuthenticator` instead.
        Everything else is the SDK's own handler behind the SDK's own CORS
        wrapper, exactly as the helper would mount it."""
        from mcp.server.auth.handlers.authorize import AuthorizationHandler
        from mcp.server.auth.handlers.metadata import MetadataHandler
        from mcp.server.auth.handlers.register import RegistrationHandler
        from mcp.server.auth.handlers.token import TokenHandler
        from mcp.server.auth.routes import build_metadata, cors_middleware, validate_issuer_url
        from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
        from starlette.routing import Route

        settings = self._settings()
        issuer = AnyHttpUrl(self.issuer)
        validate_issuer_url(issuer)
        registration = ClientRegistrationOptions(
            enabled=settings.oauth.dynamic_registration,
            valid_scopes=self.valid_scopes(),
            default_scopes=["agents:*"],
        )
        revocation = RevocationOptions(enabled=True)
        authenticator = HashedSecretClientAuthenticator(self)
        routes: list[Route] = [
            # No CORS on /authorize: clients redirect the browser to it.
            Route(
                AUTHORIZE_PATH,
                endpoint=AuthorizationHandler(self).handle,
                methods=["GET", "POST"],
            ),
            Route(
                TOKEN_PATH,
                endpoint=cors_middleware(
                    TokenHandler(self, authenticator).handle, ["POST", "OPTIONS"]
                ),
                methods=["POST", "OPTIONS"],
            ),
        ]
        if registration.enabled:
            routes.append(
                Route(
                    REGISTER_PATH,
                    endpoint=cors_middleware(
                        RegistrationHandler(self, options=registration).handle,
                        ["POST", "OPTIONS"],
                    ),
                    methods=["POST", "OPTIONS"],
                )
            )
        # RFC 7009 — ours, not the SDK's: its handler requires a `client_secret`
        # field even for public clients (RFC 7591 `token_endpoint_auth_method:
        # none`), which is what browser-less MCP clients register as, so it 400s
        # the very clients this server exists for.
        routes.append(
            Route(
                REVOKE_PATH,
                endpoint=cors_middleware(self._revoke_endpoint, ["POST", "OPTIONS"]),
                methods=["POST", "OPTIONS"],
            )
        )
        metadata = build_metadata(issuer, None, registration, revocation)
        # The SDK advertises only the two secret-based methods, but this server
        # also serves public clients (RFC 7591 `token_endpoint_auth_method:
        # none`, PKCE only: CLIs, desktop apps, browser-less MCP clients). DCR
        # issues them no secret and /token and /revoke accept them, so leaving
        # `none` out told exactly those clients they could not sign in here.
        metadata.token_endpoint_auth_methods_supported = list(CLIENT_AUTH_METHODS)
        metadata.revocation_endpoint_auth_methods_supported = list(CLIENT_AUTH_METHODS)
        handler = cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"])
        for path in (
            # RFC 8414 path insertion + OIDC discovery twin — need /.well-known/*
            # forwarded to the runtime (deploy/Caddyfile* do).
            f"/.well-known/oauth-authorization-server{OAUTH_PREFIX}",
            f"/.well-known/openid-configuration{OAUTH_PREFIX}",
            *METADATA_PATHS,
        ):
            routes.append(Route(path, endpoint=handler, methods=["GET", "OPTIONS"]))
        return routes


__all__ = [
    "CONSENT_ROUTE",
    "ENDPOINT_PATHS",
    "OAUTH_PREFIX",
    "HashedSecretClientAuthenticator",
    "OAuthClientStore",
    "OlanoAuthorizationServer",
    "PendingAuthorization",
    "agents_from_scopes",
    "client_store_path",
    "scopes_for_agents",
]
