"""Meta connector — one login powering both Facebook Pages and Instagram.

Setup: create an app at developers.facebook.com (type "Business"), add
the **Facebook Login for Business** product, register the redirect URI
``<dashboard origin>/api/auth/meta/callback``, and copy the App ID / App
Secret. Publishing permissions (``pages_manage_posts``,
``instagram_content_publish``) require App Review before non-testers can
grant them.

Token lifecycle (no standard refresh grant):

1. the authorization code yields a short-lived (~1-2h) user token;
2. :func:`_exchange_long_lived` swaps it for a ~60-day user token
   (``fb_exchange_token``);
3. ``/me/accounts`` is queried for Page access tokens — a Page token
   minted from a long-lived user token does not expire, and it is what
   the ``facebook``/``instagram`` toolkits need for publishing. If
   ``META_PAGE_ID`` is configured we pick that Page; a single-Page
   account picks its only Page; otherwise the long-lived user token is
   kept (list/read works; select a Page to publish).

The stored token serves ``META_ACCESS_TOKEN``; ``META_PAGE_ID`` /
``META_IG_USER_ID`` stay user-configured in the vault. When the ~60-day
user token lapses, reconnect from the Connections tab.
"""

from __future__ import annotations

from typing import Any

from olano.core.connectors import base
from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

_GRAPH = "https://graph.facebook.com/v22.0"
# Fallback lifetime for the long-lived user token when the exchange
# response omits expires_in (~60 days).
_LONG_LIVED_S = 60 * 24 * 3600.0


def _exchange_long_lived(provider: ConnectorProvider, record: dict[str, Any]) -> None:
    """Swap short-lived → long-lived user token, then prefer a Page token."""
    import time

    payload = base._get_json(
        f"{_GRAPH}/oauth/access_token"
        f"?grant_type=fb_exchange_token&client_id={record['client_id']}"
        f"&client_secret={record['client_secret']}"
        f"&fb_exchange_token={record['access_token']}"
    )
    user_token = payload.get("access_token") or record["access_token"]
    expires_in = payload.get("expires_in")
    record["access_token"] = user_token
    record["expires_at"] = time.time() + (float(expires_in) if expires_in else _LONG_LIVED_S)
    record["extras"]["user_token"] = user_token

    try:
        accounts = base._get_json(f"{_GRAPH}/me/accounts?access_token={user_token}")
        pages = [p for p in accounts.get("data", []) if p.get("access_token")]
    except Exception:
        pages = []
    if not pages:
        return
    record["extras"]["pages"] = [{"id": p.get("id"), "name": p.get("name")} for p in pages]

    from olano.tools._credentials import get_credential

    wanted = get_credential("META_PAGE_ID")
    chosen = next((p for p in pages if wanted and p.get("id") == wanted), None)
    if chosen is None and len(pages) == 1:
        chosen = pages[0]
    if chosen is not None:
        # Page tokens derived from a long-lived user token do not expire.
        record["access_token"] = chosen["access_token"]
        record["expires_at"] = None
        record["extras"]["page_id"] = chosen.get("id")


register(
    ConnectorProvider(
        id="meta",
        name="Meta (Facebook & Instagram)",
        authorize_url="https://www.facebook.com/v22.0/dialog/oauth",
        token_url=f"{_GRAPH}/oauth/access_token",
        # Matches the facebook/instagram toolkits: page list/read/publish,
        # page + IG insights, IG publish/comments.
        scopes=(
            "pages_show_list",
            "pages_read_engagement",
            "pages_manage_posts",
            "pages_manage_engagement",
            "pages_read_user_content",
            "read_insights",
            "instagram_basic",
            "instagram_content_publish",
            "instagram_manage_comments",
            "instagram_manage_insights",
            "business_management",
        ),
        creds_vault_key="META_OAUTH_CLIENT",
        client_env_pair=("META_CLIENT_ID", "META_CLIENT_SECRET"),
        exposes={"META_ACCESS_TOKEN": "access_token"},
        client_auth="post",
        token_method="get",
        scope_separator=",",
        supports_refresh=False,
        post_exchange=_exchange_long_lived,
    )
)
