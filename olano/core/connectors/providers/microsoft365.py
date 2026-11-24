"""Microsoft 365 connector — delegated Microsoft Graph token.

Setup: create an app registration in Entra ID (portal.azure.com → App
registrations), add a **Web** redirect URI of
``<dashboard origin>/api/auth/microsoft365/callback``, create a client
secret, and grant delegated Graph permissions matching the scopes below.
Multi-tenant + personal accounts work via the default ``common`` tenant;
orgs with conditional-access policies can enter their tenant GUID in the
connect panel instead.

Tokens: access tokens last ~60-90 minutes; ``offline_access`` yields a
rolling refresh token (rotated on every refresh — the broker persists the
replacement automatically). The delegated token powers every
``microsoft365`` toolkit tool through ``MS365_ACCESS_TOKEN`` — note that
sending Teams channel messages is delegated-only, which is exactly what
this flow mints.

Scopes: every one below is a delegated permission a user can consent to
without an admin in a default tenant. Never add one that requires admin
consent (``ChannelMessage.Read.All``, for one): a single such scope turns the
whole consent screen into "Need admin approval" for every non-admin user.
A refresh does not ask for new scopes (``refresh_includes_scope`` is off), so
a connection made before a scope was added keeps working for the tools it
had and gets a 403 on the rest until the user signs in again; the toolkit's
403 message says exactly that.
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

register(
    ConnectorProvider(
        id="microsoft365",
        name="Microsoft 365",
        authorize_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        # Matches the toolkit's Graph endpoints: mail (drafts, folders, flags,
        # attachments) + send, calendar CRUD + meeting times, people and
        # directory lookup, mailbox settings (time zone, automatic replies),
        # contacts, To Do, OneDrive/SharePoint files + Excel + search, Teams
        # channels and chats.
        scopes=(
            "offline_access",
            "User.Read",
            "User.ReadBasic.All",
            "People.Read",
            "Mail.ReadWrite",
            "Mail.Send",
            "MailboxSettings.ReadWrite",
            "Calendars.ReadWrite",
            "Calendars.Read.Shared",
            "Contacts.ReadWrite",
            "Tasks.ReadWrite",
            "Files.ReadWrite.All",
            "Sites.Read.All",
            "Team.ReadBasic.All",
            "Channel.ReadBasic.All",
            "ChannelMessage.Send",
            "Chat.ReadWrite",
        ),
        creds_vault_key="MS365_OAUTH_CLIENT",
        client_env_pair=("MS365_CLIENT_ID", "MS365_CLIENT_SECRET"),
        exposes={
            "MS365_ACCESS_TOKEN": "access_token",
            "MICROSOFT_GRAPH_ACCESS_TOKEN": "access_token",
        },
        pkce=True,
        client_auth="post",
        optional_params={"tenant": "common"},
    )
)
