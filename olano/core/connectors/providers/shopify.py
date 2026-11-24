"""Shopify connector — per-shop OAuth with a permanent offline token.

Setup: create a custom/public app in the Shopify Partner dashboard (or
shop admin → Apps → Develop apps for a single store), set the allowed
redirection URL to ``<dashboard origin>/api/auth/shopify/callback``, and
copy the API key (client id) + API secret. The connect panel asks for the
shop's ``myshopify.com`` subdomain — entering either ``mystore`` or
``mystore.myshopify.com`` works.

Shopify's token exchange is non-standard (no ``grant_type`` /
``redirect_uri`` — just client id + secret + code, per shop), handled by
the :func:`_token_request` override. Offline access tokens are permanent
until the app is uninstalled, so there is no refresh. The broker serves
``SHOPIFY_ACCESS_TOKEN`` plus the full store domain under
``SHOPIFY_STORE`` (the name the toolkit reads) and ``SHOPIFY_SHOP``.
"""

from __future__ import annotations

from typing import Any

from olano.core.connectors.base import ConnectorProvider, OAuthSession
from olano.core.connectors.registry import register


def _normalize(params: dict[str, str]) -> dict[str, str]:
    """Accept 'mystore', 'mystore.myshopify.com', or a pasted URL."""
    shop = params.get("shop", "")
    shop = shop.replace("https://", "").replace("http://", "").strip().strip("/")
    if shop.endswith(".myshopify.com"):
        shop = shop[: -len(".myshopify.com")]
    return {**params, "shop": shop}


def _token_request(
    provider: ConnectorProvider, session: OAuthSession, code: str
) -> tuple[str, dict[str, str], dict[str, str] | None, tuple[str, str] | None]:
    url = f"https://{session.params['shop']}.myshopify.com/admin/oauth/access_token"
    data = {
        "client_id": session.client_id,
        "client_secret": session.client_secret,
        "code": code,
    }
    return url, data, None, None


def _store_domain(provider: ConnectorProvider, record: dict[str, Any]) -> None:
    record["extras"]["store_domain"] = f"{record['params']['shop']}.myshopify.com"


register(
    ConnectorProvider(
        id="shopify",
        name="Shopify",
        authorize_url="https://{shop}.myshopify.com/admin/oauth/authorize",
        token_url="https://{shop}.myshopify.com/admin/oauth/access_token",
        # Matches the toolkit: product list/create/update, order list/get,
        # customer search. Shopify wants comma-separated scopes.
        scopes=("read_products", "write_products", "read_orders", "read_customers"),
        creds_vault_key="SHOPIFY_OAUTH_CLIENT",
        client_env_pair=("SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET"),
        exposes={
            "SHOPIFY_ACCESS_TOKEN": "access_token",
            "SHOPIFY_STORE": "extras.store_domain",
            "SHOPIFY_SHOP": "extras.store_domain",
        },
        client_auth="post",
        scope_separator=",",
        required_params=("shop",),
        supports_refresh=False,
        normalize_params=_normalize,
        token_request=_token_request,
        post_exchange=_store_domain,
    )
)
