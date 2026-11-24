"""LinkedIn connector — OIDC sign-in + member posting.

Setup: create an app at developer.linkedin.com, add the products
"Sign In with LinkedIn using OpenID Connect" and "Share on LinkedIn",
and register the redirect URL
``<dashboard origin>/api/auth/linkedin/callback``.

Scopes: ``openid profile email`` (resolves the member URN the toolkit's
posting needs via ``/v2/userinfo``) + ``w_member_social`` (create posts).
The restricted analytics scope (``r_member_social``) is deliberately NOT
requested — it is gated behind Community Management API approval and
including it would fail authorization for ordinary apps; the toolkit's
analytics tool reports its own permission error instead.

Access tokens last ~60 days and programmatic refresh is partner-gated,
so the broker treats the token as non-refreshable — reconnect from the
Connections tab when it lapses. Exposed as ``LINKEDIN_ACCESS_TOKEN``.
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

register(
    ConnectorProvider(
        id="linkedin",
        name="LinkedIn",
        authorize_url="https://www.linkedin.com/oauth/v2/authorization",
        token_url="https://www.linkedin.com/oauth/v2/accessToken",
        scopes=("openid", "profile", "email", "w_member_social"),
        creds_vault_key="LINKEDIN_OAUTH_CLIENT",
        client_env_pair=("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"),
        exposes={"LINKEDIN_ACCESS_TOKEN": "access_token"},
        client_auth="post",
        supports_refresh=False,
        # Responses include expires_in (~60d); this is belt-and-braces.
        expires_default=60 * 24 * 3600.0,
    )
)
