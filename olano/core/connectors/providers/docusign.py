"""DocuSign connector — signature scope with userinfo account discovery.

Setup: create an app in the DocuSign developer console (Apps & Keys),
add the redirect URI ``<dashboard origin>/api/auth/docusign/callback``,
and copy the Integration Key (client id) + a Secret Key. Demo/sandbox
accounts authenticate against ``account-d.docusign.com`` — enter
``account-d`` in the connect panel's environment field (default
``account`` = production).

After the code exchange the broker calls ``/oauth/userinfo`` to discover
the user's accounts, picks the default one, and captures
``account_id`` → ``DOCUSIGN_ACCOUNT_ID`` and ``base_uri + "/restapi"`` →
``DOCUSIGN_BASE_URL`` — exactly the values the toolkit reads alongside
the live ``DOCUSIGN_ACCESS_TOKEN``. Access tokens last ~8 hours; refresh
tokens are issued (and may rotate — the broker persists replacements).
"""

from __future__ import annotations

from typing import Any

from olano.core.connectors import base
from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register


def _capture_account(provider: ConnectorProvider, record: dict[str, Any]) -> None:
    env = record.get("params", {}).get("environment", "account")
    info = base._get_json(
        f"https://{env}.docusign.com/oauth/userinfo",
        headers={"Authorization": f"Bearer {record['access_token']}"},
    )
    accounts = info.get("accounts") or []
    if not accounts:
        return
    chosen = next((a for a in accounts if a.get("is_default")), accounts[0])
    if chosen.get("account_id"):
        record["extras"]["account_id"] = chosen["account_id"]
    if chosen.get("base_uri"):
        record["extras"]["base_url"] = f"{chosen['base_uri'].rstrip('/')}/restapi"


register(
    ConnectorProvider(
        id="docusign",
        name="DocuSign",
        authorize_url="https://{environment}.docusign.com/oauth/auth",
        token_url="https://{environment}.docusign.com/oauth/token",
        # "extended" (authorize-time only) keeps refresh tokens renewable
        # ~indefinitely — without it refresh dies after ~30 days.
        scopes=("signature", "extended"),
        creds_vault_key="DOCUSIGN_OAUTH_CLIENT",
        client_env_pair=("DOCUSIGN_CLIENT_ID", "DOCUSIGN_CLIENT_SECRET"),
        exposes={
            "DOCUSIGN_ACCESS_TOKEN": "access_token",
            "DOCUSIGN_ACCOUNT_ID": "extras.account_id",
            "DOCUSIGN_BASE_URL": "extras.base_url",
        },
        pkce=True,
        client_auth="basic",
        optional_params={"environment": "account"},
        post_exchange=_capture_account,
    )
)
