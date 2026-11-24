"""HubSpot connector: OAuth in place of a pasted private-app token.

Setup: create an app in a HubSpot developer account (a public / multi-account
app, so any customer portal can install it), add the redirect URL
``<dashboard origin>/api/auth/hubspot/callback`` (and, for the official
client, ``https://auth.olano.ai/connect/callback``), and configure its scopes
to match the two lists below **exactly**: HubSpot refuses an install whose
``scope`` parameter does not carry every scope the app marks required, and one
whose ``optional_scope`` parameter names a scope the app does not list as
optional ("mismatch between the scopes in the install URL and the app's
configured scopes").

Why two lists: a required scope the customer's HubSpot plan does not include
fails the whole install, so only scopes every tier (Free included) has are
required, which is enough for the everyday CRM tools (contacts, companies,
deals, owners, notes and tasks). Everything else is optional: HubSpot grants
what the portal's plan and the installing user's permissions allow and drops
the rest silently, so a tool that needs a dropped scope answers 403
``MISSING_SCOPES``, which the toolkit explains instead of relaying.

Tokens: the v3 token endpoint (``/oauth/v3/token``; v1 is deprecated) takes a
form-encoded body with the client secret in it, has no PKCE, and returns a
30-minute access token plus a refresh token that does not expire and is not
rotated. ``hub_id`` (the portal id) is captured into extras for display. The
access token is served as ``HUBSPOT_ACCESS_TOKEN``, the name
``tools/hubspot_tools.py`` reads, so a hand-pasted private-app token and an
OAuth connection are interchangeable to every consumer.

A refresh does not ask for new scopes, so a connection made before a scope was
added here keeps working for the tools it had and gets ``MISSING_SCOPES`` on
the rest until the user reconnects.
"""

from __future__ import annotations

from olano.core.connectors.base import ConnectorProvider
from olano.core.connectors.registry import register

# Present on every HubSpot tier, Free included. Keep this list short: one scope
# a portal's plan lacks turns every connect from that portal into an error.
REQUIRED_SCOPES: tuple[str, ...] = (
    "oauth",
    "crm.objects.contacts.read",
    "crm.objects.contacts.write",
    "crm.objects.companies.read",
    "crm.objects.companies.write",
    "crm.objects.deals.read",
    "crm.objects.deals.write",
    "crm.objects.owners.read",
)

# Granted when the portal's plan and the installing user allow, dropped
# otherwise. Each line names the toolkit tools that need it.
OPTIONAL_SCOPES: tuple[str, ...] = (
    "tickets",  # hubspot_list_tickets, hubspot_create_ticket
    "e-commerce",  # products (hubspot_create_product)
    "crm.objects.line_items.read",  # line items
    "crm.objects.line_items.write",  # hubspot_create_line_item
    "crm.objects.quotes.read",  # hubspot_list_quotes
    "crm.objects.quotes.write",  # hubspot_create_quote
    "crm.objects.invoices.read",  # hubspot_list_invoices
    "crm.objects.custom.read",  # generic CRUD on custom objects
    "crm.objects.custom.write",
    "crm.schemas.contacts.read",  # hubspot_list_properties
    "crm.schemas.contacts.write",  # hubspot_create_property(_group)
    "crm.schemas.companies.read",
    "crm.schemas.companies.write",
    "crm.schemas.deals.read",
    "crm.schemas.deals.write",
    "crm.schemas.custom.read",  # hubspot_list_schemas
    "crm.lists.read",  # hubspot_get_list, hubspot_search_lists, members
    "crm.lists.write",  # hubspot_create_list, add/remove members
    "crm.import",  # hubspot_list_imports, hubspot_get_import_status
    "crm.export",  # hubspot_start_export, hubspot_get_export_status
    "sales-email-read",  # email activity bodies
    "timeline",  # hubspot_create_timeline_event
    "forms",  # hubspot_list_forms, hubspot_get_form_submissions
    "automation",  # workflows
    "content",  # blog posts, site/landing pages, URL redirects
    "hubdb",  # HubDB tables and rows
    "files",  # hubspot_upload_file
    "marketing-email",  # hubspot_list_marketing_emails
    "transactional-email",  # hubspot_send_transactional_email
)

register(
    ConnectorProvider(
        id="hubspot",
        name="HubSpot",
        authorize_url="https://app.hubspot.com/oauth/authorize",
        token_url="https://api.hubapi.com/oauth/v3/token",
        scopes=REQUIRED_SCOPES,
        creds_vault_key="HUBSPOT_OAUTH_CLIENT",
        client_env_pair=("HUBSPOT_CLIENT_ID", "HUBSPOT_CLIENT_SECRET"),
        exposes={"HUBSPOT_ACCESS_TOKEN": "access_token"},
        client_auth="post",
        extra_auth_params={"optional_scope": " ".join(OPTIONAL_SCOPES)},
        token_extras=("hub_id",),
    )
)
