"""Token verification for the inbound MCP server.

Implements the ``mcp`` SDK's :class:`TokenVerifier` protocol — one async
method, token in, :class:`AccessToken` out — so the SDK's own bearer
middleware, 401/403 handling and protected-resource metadata do the rest.

Two credential shapes are accepted:

* **Olano-minted bearer tokens** (:mod:`olano.mcp_server.tokens`): the default,
  zero-infrastructure path. The token's agent list becomes its scopes.
* **JWT access tokens from an external authorization server** (opt-in via
  ``config.yaml → mcp_server.jwt``): verified against the issuer's JWKS with
  PyJWT (already a dependency of the ``mcp`` SDK), with the issuer, expiry and
  — per RFC 8707 / the MCP spec's audience-binding requirement — the ``aud``
  claim all enforced. A token minted for a different API is refused even when
  the same issuer signed it. Scopes come from the ``scope``/``scp`` claim.

Anything else — the dashboard's own bearer token, a session cookie — is not a
credential here: the MCP endpoint is a separate trust boundary.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.server.auth.provider import AccessToken

from olano.config.system_settings import ExternalAccessJwtSettings
from olano.mcp_server.tokens import McpTokenStore, get_token_store, parse_token_id

logger = logging.getLogger(__name__)

_JWT_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]


class OlanoTokenVerifier:
    """``TokenVerifier`` over the Olano token store + optional external JWTs."""

    def __init__(
        self,
        *,
        resource_url: str,
        jwt_settings: ExternalAccessJwtSettings | None = None,
        store: McpTokenStore | None = None,
        extra_audiences: tuple[str, ...] = (),
    ) -> None:
        self.resource_url = resource_url
        # Also accepted as a JWT `aud` when no audience is configured: a
        # resource identifier an earlier release bound tokens to.
        self.extra_audiences = extra_audiences
        self.jwt_settings = jwt_settings or ExternalAccessJwtSettings()
        self._store = store
        self._jwks_client: Any = None

    @property
    def store(self) -> McpTokenStore:
        return self._store or get_token_store()

    async def verify_token(self, token: str) -> AccessToken | None:
        token = (token or "").strip()
        if not token:
            return None
        if parse_token_id(token) is not None:
            return self._verify_static(token)
        if self.jwt_settings.configured and token.count(".") == 2:
            return self._verify_jwt(token)
        return None

    # ── Olano-minted tokens ────────────────────────────────────────────────
    def _verify_static(self, token: str) -> AccessToken | None:
        record = self.store.verify(token)
        if record is None:
            return None
        self.store.touch(record.id)
        expires_at: int | None = None
        if record.expires_at:
            from datetime import datetime

            try:
                expires_at = int(datetime.fromisoformat(record.expires_at).timestamp())
            except ValueError:  # pragma: no cover - store validated it
                expires_at = None
        return AccessToken(
            token=token,
            client_id=record.label,
            scopes=record.scope_list(),
            expires_at=expires_at,
            resource=self.resource_url,
            subject=f"mcp-token:{record.id}",
            claims={"token_id": record.id, "kind": "olano"},
        )

    # ── External JWTs ──────────────────────────────────────────────────────
    def _jwks(self) -> Any:
        if self._jwks_client is None:
            from jwt import PyJWKClient

            self._jwks_client = PyJWKClient(
                self.jwt_settings.jwks_url.strip(), cache_keys=True, lifespan=300
            )
        return self._jwks_client

    def _verify_jwt(self, token: str) -> AccessToken | None:
        import jwt

        audience: str | list[str] = (self.jwt_settings.audience or "").strip() or [
            self.resource_url,
            *self.extra_audiences,
        ]
        try:
            signing_key = self._jwks().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=_JWT_ALGORITHMS,
                audience=audience,
                issuer=self.jwt_settings.issuer.strip(),
                options={"require": ["exp", "iss", "aud"]},
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            logger.info("MCP server: JWT refused: %s", exc)
            return None
        except Exception as exc:  # pragma: no cover - JWKS fetch failures etc.
            logger.warning("MCP server: JWT verification failed: %s", exc)
            return None
        scopes = _scopes_from_claims(claims)
        required = [s for s in self.jwt_settings.required_scopes if s]
        if any(s not in scopes for s in required):
            logger.info("MCP server: JWT lacks required scopes %s", required)
            return None
        client_id = str(
            claims.get("azp") or claims.get("client_id") or claims.get("sub") or "jwt-client"
        )
        exp = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(exp) if isinstance(exp, int | float) else None,
            resource=self.resource_url,
            subject=str(claims.get("sub") or "") or None,
            claims={"iss": claims.get("iss"), "kind": "jwt"},
        )


def _scopes_from_claims(claims: dict[str, Any]) -> list[str]:
    raw = claims.get("scope")
    if isinstance(raw, str):
        return [s for s in raw.split() if s]
    scp = claims.get("scp")
    if isinstance(scp, list):
        return [str(s) for s in scp if s]
    if isinstance(scp, str):
        return [s for s in scp.split() if s]
    return []


def token_allows_agent(access: AccessToken, agent: str) -> bool:
    """Whether a verified token's scopes cover ``agent``."""
    wanted = (agent or "").casefold()
    for scope in access.scopes:
        if scope == "agents:*":
            return True
        if scope.startswith("agent:") and scope[len("agent:") :].casefold() == wanted:
            return True
    return False


def now_ts() -> int:
    return int(time.time())
