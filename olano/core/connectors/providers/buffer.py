"""Buffer connector — legacy v1 OAuth with a non-expiring token.

Setup caveat: Buffer closed new developer-app registration years ago, so
a Client ID/Secret is only available to grandfathered apps. Without one,
skip this connector and store a personal access token (Buffer →
Settings → Apps → Create access token) in the vault as
``BUFFER_ACCESS_TOKEN`` — the toolkit reads either source identically.

For grandfathered OAuth apps: register the redirect URI
``<dashboard origin>/api/auth/buffer/callback``. The legacy v1 API has no
scope system (authorization grants full account access) and issues
long-lived tokens with no refresh grant. Exposed as
``BUFFER_ACCESS_TOKEN``.
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

register(
    ConnectorProvider(
        id="buffer",
        name="Buffer",
        authorize_url="https://bufferapp.com/oauth2/authorize",
        token_url="https://api.bufferapp.com/1/oauth2/token.json",
        scopes=(),
        creds_vault_key="BUFFER_OAUTH_CLIENT",
        client_env_pair=("BUFFER_CLIENT_ID", "BUFFER_CLIENT_SECRET"),
        exposes={"BUFFER_ACCESS_TOKEN": "access_token"},
        client_auth="post",
        supports_refresh=False,
    )
)
