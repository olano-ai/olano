"""X (Twitter) connector — OAuth 2.0 user context with PKCE.

Setup: in the X Developer Portal, enable OAuth 2.0 (User authentication
settings → type "Web App"), add the callback
``<dashboard origin>/api/auth/x_twitter/callback``, and copy the OAuth 2.0
Client ID + Client Secret (confidential client).

The minted user-context token is exposed as ``X_USER_ACCESS_TOKEN`` — a
NEW credential name the toolkit prefers for every API v2 call (reads and
writes) before falling back to the legacy OAuth 1.0a keys / app-only
bearer. ``offline.access`` yields a refresh token that X rotates on every
refresh; access tokens last 2 hours. Note the free API tier still limits
which endpoints a token may call — that is a plan restriction, not an
auth failure.
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

register(
    ConnectorProvider(
        id="x_twitter",
        name="X (Twitter)",
        authorize_url="https://x.com/i/oauth2/authorize",
        token_url="https://api.x.com/2/oauth2/token",
        scopes=("tweet.read", "tweet.write", "users.read", "like.write", "offline.access"),
        creds_vault_key="X_OAUTH_CLIENT",
        client_env_pair=("X_CLIENT_ID", "X_CLIENT_SECRET"),
        exposes={"X_USER_ACCESS_TOKEN": "access_token"},
        pkce=True,
        client_auth="basic",
    )
)
