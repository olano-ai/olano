"""Fitbit connector — wearable data scopes with rotating refresh tokens.

Setup: register an app at dev.fitbit.com/apps (application type
"Personal" reads your own intraday data), set the redirect URL to
``<dashboard origin>/api/auth/fitbit/callback``, and copy the OAuth 2.0
Client ID + Client Secret.

Access tokens last 8 hours; the refresh token is single-use and rotated
on every refresh (the broker persists each replacement). The live token
serves ``FITBIT_ACCESS_TOKEN``, powering both the ``fitbit`` and
``health`` toolkits; scopes cover exactly the endpoints those tools call
(activity, sleep, heart rate, weight).
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

register(
    ConnectorProvider(
        id="fitbit",
        name="Fitbit",
        authorize_url="https://www.fitbit.com/oauth2/authorize",
        token_url="https://api.fitbit.com/oauth2/token",
        scopes=("activity", "heartrate", "sleep", "weight"),
        creds_vault_key="FITBIT_OAUTH_CLIENT",
        client_env_pair=("FITBIT_CLIENT_ID", "FITBIT_CLIENT_SECRET"),
        exposes={"FITBIT_ACCESS_TOKEN": "access_token"},
        pkce=True,
        client_auth="basic",
    )
)
