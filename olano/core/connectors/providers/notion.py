"""Notion connector — a public integration's OAuth, in place of a pasted token.

Setup: create a **public** integration at notion.so/my-integrations (the
internal-integration token is the ``key`` method; this is the browser one),
register the redirect URL ``<dashboard origin>/api/auth/notion/callback``,
and paste its OAuth client id + secret into the Connections panel. The
consent screen lets the person pick which pages the integration may see.

Notion's token endpoint is unusual on two counts, both handled by the
``token_request`` override: HTTP Basic client auth **and** a JSON body (a
form-encoded post is rejected). Access tokens do not expire and there is no
refresh grant, so the record is non-expiring. Exposed as ``NOTION_API_KEY``
(what ``tools/notion_tools.py`` reads) and ``NOTION_TOKEN`` (its legacy
name), so a hand-pasted key and an OAuth connection are interchangeable to
every consumer. The workspace and bot ids land in ``extras`` for display.
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider, OAuthSession
from olano.core.connectors.registry import register


def _token_request(
    provider: ConnectorProvider, session: OAuthSession, code: str
) -> tuple[str, dict[str, str], dict[str, str] | None, tuple[str, str] | None]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": session.redirect_uri,
    }
    return (
        provider.token_url,
        data,
        {"Content-Type": "application/json"},
        (session.client_id, session.client_secret),
    )


register(
    ConnectorProvider(
        id="notion",
        name="Notion",
        authorize_url="https://api.notion.com/v1/oauth/authorize",
        token_url="https://api.notion.com/v1/oauth/token",
        # Notion has no scope parameter; access is chosen on the consent page.
        scopes=(),
        creds_vault_key="NOTION_OAUTH_CLIENT",
        client_env_pair=("NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET"),
        exposes={
            "NOTION_API_KEY": "access_token",
            "NOTION_TOKEN": "access_token",
        },
        client_auth="basic",
        extra_auth_params={"owner": "user"},
        token_extras=("workspace_id", "workspace_name", "bot_id"),
        token_request=_token_request,
        supports_refresh=False,
        expires_default=None,
    )
)
