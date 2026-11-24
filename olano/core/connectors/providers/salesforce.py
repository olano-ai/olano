"""Salesforce connector — Connected App OAuth with instance-URL capture.

Setup: create a Connected App (Setup → App Manager → New Connected App),
enable OAuth, add the callback
``<dashboard origin>/api/auth/salesforce/callback``, and select the
``api`` + ``refresh_token`` scopes. Sandboxes: enter ``test`` in the
connect panel's domain field (→ ``test.salesforce.com``).

Salesforce's token response carries ``instance_url`` — the org host every
REST call must target — which the broker captures into extras and serves
as ``SALESFORCE_INSTANCE_URL`` alongside the live ``SALESFORCE_ACCESS_TOKEN``.
Access tokens expire on the org's session policy (typically ~2h) and the
token response omits ``expires_in``, so ``expires_default`` re-arms the
refresh clock; refresh tokens are long-lived and not rotated.
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

register(
    ConnectorProvider(
        id="salesforce",
        name="Salesforce",
        authorize_url="https://{domain}.salesforce.com/services/oauth2/authorize",
        token_url="https://{domain}.salesforce.com/services/oauth2/token",
        scopes=("api", "refresh_token"),
        creds_vault_key="SALESFORCE_OAUTH_CLIENT",
        client_env_pair=("SALESFORCE_CLIENT_ID", "SALESFORCE_CLIENT_SECRET"),
        exposes={
            "SALESFORCE_ACCESS_TOKEN": "access_token",
            "SALESFORCE_INSTANCE_URL": "extras.instance_url",
        },
        pkce=True,
        client_auth="post",
        optional_params={"domain": "login"},
        token_extras=("instance_url",),
        # Token responses omit expires_in; assume ~2h so refresh re-arms.
        expires_default=7200.0,
    )
)
