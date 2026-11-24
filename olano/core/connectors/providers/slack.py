"""Slack connector — a bot token (and optionally a user token) over OAuth v2.

Setup: create a Slack app at api.slack.com/apps, add the bot scopes below
under *OAuth & Permissions*, and register the redirect URL
``<dashboard origin>/api/auth/slack/callback``. Slack answers the token
endpoint with HTTP 200 even on failure (``{"ok": false, "error": …}``) —
the broker's "no access_token" check catches that, and ``post_exchange``
turns it into Slack's own error text.

Two tokens come back from one consent:

- the **bot token** (``access_token``) — exposed as ``SLACK_BOT_TOKEN``,
  what ``tools/slack_tools.py`` reads for posting, reading channels and
  listing users;
- the **user token** (``authed_user.access_token``, granted because the
  authorize URL also asks for ``user_scope=search:read``) — exposed as
  ``SLACK_USER_TOKEN``. ``search.messages`` is a user-scope method a bot
  token cannot call, so ``slack_search`` prefers this one when present.

Token rotation is opt-in per Slack app; when enabled the response carries a
``refresh_token`` and the standard refresh grant applies, otherwise the bot
token never expires. Note the *channel* connector (``platform:slack``, a bot
that listens in a workspace) stores its own ``SLACK_BOT_TOKEN`` in the vault
through the channel config — a different consumer, no collision: the broker
resolves above the vault for agents connected here.
"""

from __future__ import annotations

from typing import Any

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

# Bot scopes the toolkit's tools need (mirrors tools/requirements.py guidance).
_BOT_SCOPES = (
    "chat:write",
    "channels:read",
    "channels:history",
    "groups:read",
    "im:read",
    "users:read",
)


def _capture_user_token(provider: ConnectorProvider, record: dict[str, Any]) -> None:
    """Lift ``authed_user.access_token`` into ``extras.user_token``.

    ``token_extras`` captured the whole ``authed_user`` object; keep only the
    token (as its own exposable field) and the user id.
    """
    extras = record.setdefault("extras", {})
    authed = extras.pop("authed_user", None)
    if isinstance(authed, dict):
        if authed.get("access_token"):
            extras["user_token"] = str(authed["access_token"])
        if authed.get("id"):
            extras["user_id"] = str(authed["id"])
    team = extras.get("team")
    if isinstance(team, dict):
        extras["team"] = {k: team[k] for k in ("id", "name") if team.get(k)}


register(
    ConnectorProvider(
        id="slack",
        name="Slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        scopes=_BOT_SCOPES,
        creds_vault_key="SLACK_OAUTH_CLIENT",
        client_env_pair=("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"),
        exposes={
            "SLACK_BOT_TOKEN": "access_token",
            "SLACK_USER_TOKEN": "extras.user_token",
        },
        client_auth="post",
        # Slack separates bot scopes (`scope`) from user scopes (`user_scope`).
        extra_auth_params={"user_scope": "search:read"},
        scope_separator=",",
        token_extras=("bot_user_id", "team", "authed_user"),
        post_exchange=_capture_user_token,
        # Rotation is opt-in per app; without it the bot token never expires.
        supports_refresh=True,
    )
)
