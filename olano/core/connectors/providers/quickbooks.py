"""QuickBooks (Intuit) connector — accounting scope with realmId capture.

Setup: create an app at developer.intuit.com, add the redirect URI
``<dashboard origin>/api/auth/quickbooks/callback``, and use the app's
Client ID/Secret (dev keys → sandbox companies, prod keys → production;
set the environment field accordingly so the toolkit targets the right
API host via ``QUICKBOOKS_ENVIRONMENT``).

Intuit's company id (``realmId``) is NOT in the token response — it
arrives as a query parameter on the authorization redirect, so the broker
captures it from the callback and serves it as ``QUICKBOOKS_REALM_ID``.
Access tokens last ~60 minutes; refresh tokens ~100 days and Intuit
rotates them — the broker persists each replacement automatically.
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

register(
    ConnectorProvider(
        id="quickbooks",
        name="QuickBooks",
        authorize_url="https://appcenter.intuit.com/connect/oauth2",
        token_url="https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        scopes=("com.intuit.quickbooks.accounting",),
        creds_vault_key="QUICKBOOKS_OAUTH_CLIENT",
        client_env_pair=("QUICKBOOKS_CLIENT_ID", "QUICKBOOKS_CLIENT_SECRET"),
        exposes={
            "QUICKBOOKS_ACCESS_TOKEN": "access_token",
            "QUICKBOOKS_REALM_ID": "extras.realmId",
            "QUICKBOOKS_ENVIRONMENT": "params.environment",
        },
        client_auth="basic",
        optional_params={"environment": "production"},
        callback_extras=("realmId",),
    )
)
