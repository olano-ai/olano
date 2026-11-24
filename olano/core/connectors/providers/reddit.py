"""Reddit connector — permanent-duration user OAuth for praw.

Setup: create an app at reddit.com/prefs/apps (type "web app"), set the
redirect URI to ``<dashboard origin>/api/auth/reddit/callback``, and copy
the client id (under the app name) + secret.

``duration=permanent`` is mandatory — without it Reddit issues no refresh
token. Access tokens last 1 hour; the refresh token is long-lived and
NOT rotated, so both the broker and praw may refresh from it safely.

Exposure is praw-shaped: besides ``REDDIT_ACCESS_TOKEN``, the broker
serves ``REDDIT_REFRESH_TOKEN`` + ``REDDIT_CLIENT_ID`` +
``REDDIT_CLIENT_SECRET`` so the toolkit's ``_reddit()`` can construct a
full user-context ``praw.Reddit(refresh_token=…)`` client (reads AND
writes) — replacing both the app-only and username/password paths when
connected. Reddit requires a descriptive User-Agent on every request,
including token requests (``token_headers`` below).
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

register(
    ConnectorProvider(
        id="reddit",
        name="Reddit",
        authorize_url="https://www.reddit.com/api/v1/authorize",
        token_url="https://www.reddit.com/api/v1/access_token",
        scopes=("identity", "read", "submit"),
        creds_vault_key="REDDIT_OAUTH_CLIENT",
        client_env_pair=("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
        exposes={
            "REDDIT_ACCESS_TOKEN": "access_token",
            "REDDIT_REFRESH_TOKEN": "refresh_token",
            "REDDIT_CLIENT_ID": "client_id",
            "REDDIT_CLIENT_SECRET": "client_secret",
        },
        client_auth="basic",
        extra_auth_params={"duration": "permanent"},
        token_headers={"User-Agent": "olano:connector-broker:v1 (dashboard OAuth)"},
    )
)
