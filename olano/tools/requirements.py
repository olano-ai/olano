"""Tool requirements metadata.

Each tool can declare a small set of operational requirements:

  • `env_vars`         — environment variables / vault secrets the tool reads
  • `packages`         — Python packages (auto-runnable: `uv add <pkg>`)
  • `npm_packages`     — Node packages (auto-runnable: `npm install <pkg>`)
  • `setup_commands`   — Post-install commands (e.g. `playwright install`),
                         runnable via the dashboard's allowlisted shell-exec.
  • `cli`              — System binaries the host needs on PATH (`ffmpeg`,
                         `gcloud`, …). NOT auto-installable: the dashboard
                         shows per-platform install suggestions (brew /
                         apt-get / winget / etc.) and Run is gated to whatever
                         prefixes the shell-exec allowlist covers.

Most tools share the same requirements as the rest of their bundle (e.g. every
`github_*` tool needs `GITHUB_TOKEN`), so the source of truth is keyed on
bundle name. A small `TOOL_OVERRIDES` dict lets individual tools opt out or
add extras without bloating the bundle entry.

The dashboard reads this metadata via /api/catalog/tools to:

  • Show "ready / missing API key / missing package" badges on each tool
  • Open an inline "Set secret" dialog when an env var is unset
  • Provide Copy + Run buttons (with confirmation) for runnable installs
  • Surface platform-specific install hints for system CLIs

To add a new bundle of tools, register the toolkit in `_TOOLKITS` (or a
composite bundle in `_COMPOSITE_BUNDLES`) inside registry.py AND add an
entry here. Per-tool overrides should be rare.
"""

from __future__ import annotations

from typing import TypedDict


class EnvVarSpec(TypedDict, total=False):
    name: str
    label: str
    doc_url: str
    guidance: str
    optional: bool


class Requirements(TypedDict, total=False):
    env_vars: list[EnvVarSpec]
    packages: list[str]
    npm_packages: list[str]
    setup_commands: list[str]
    cli: list[str]
    doc_url: str
    guidance: str


def _ev(name: str, **kw: object) -> EnvVarSpec:
    """EnvVarSpec shorthand."""
    spec: EnvVarSpec = {"name": name}
    spec.update(kw)  # type: ignore[arg-type, typeddict-item]
    return spec


# `image_describe` accepts ANY vision-capable provider and falls back across
# every configured one, so each key is individually optional — and on a managed
# deployment none of them is needed at all. Marking one required would report a
# box with a working OpenAI key (or a box on credits) as broken.
_VISION_GUIDANCE = (
    "Not needed on a managed deployment: image-to-text runs on the Olano "
    "service, billed in credits. Elsewhere, any ONE of these enables it — the "
    "tool tries each configured provider in turn and falls through when one is "
    "out of credits or unauthorized. Set LIONCLAW_VISION_MODEL to pin a single "
    "model instead (that provider's key is then the one that matters)."
)
_VISION_REQUIREMENTS: Requirements = {
    "env_vars": [
        _ev(
            "OPENAI_API_KEY",
            optional=True,
            doc_url="https://platform.openai.com/api-keys",
            guidance=_VISION_GUIDANCE,
        ),
        _ev(
            "ANTHROPIC_API_KEY",
            optional=True,
            doc_url="https://console.anthropic.com/settings/keys",
            guidance=_VISION_GUIDANCE + " This key is the unmanaged default.",
        ),
        _ev(
            "GOOGLE_API_KEY",
            optional=True,
            doc_url="https://aistudio.google.com/apikey",
            guidance=_VISION_GUIDANCE + " GEMINI_API_KEY is accepted too.",
        ),
    ],
}


# Bundle-level requirements. Resolved as the default for every tool that
# belongs to that bundle.
BUNDLE_REQUIREMENTS: dict[str, Requirements] = {
    # ── Web / scraping ──
    "web": {
        "env_vars": [
            _ev(
                "TAVILY_API_KEY",
                doc_url="https://tavily.com/",
                guidance="Sign up free → Settings → API Keys → copy.",
            ),
        ],
        "packages": ["tavily-python", "requests"],
    },
    "browser": {
        "env_vars": [
            _ev(
                "BROWSER_REMOTE_CDP_URL",
                optional=True,
                guidance=(
                    "Only for Config → Browser → Remote browser service: the hosted "
                    "browser's DevTools address (ws:// or wss://, usually with its key). "
                    "Not needed for the browser on this machine."
                ),
            ),
            _ev(
                "BROWSER_PROXY_URL",
                optional=True,
                guidance=(
                    "Only to send the browser on this machine through a proxy, so sites "
                    "see the proxy's address instead of this server's: "
                    "http://user:password@host:port (or https://, socks5://, socks4:// "
                    "without a password). Prefer a proxy that keeps the same address."
                ),
            ),
        ],
        "packages": ["playwright"],
        "setup_commands": ["playwright install chromium"],
        "guidance": "Run `playwright install chromium` once after installing the package.",
    },
    # ── Search & extraction ──
    "knowledge": {"packages": ["chromadb", "tiktoken"]},
    # ── GitHub ──
    "github": {
        "env_vars": [
            _ev(
                "GITHUB_TOKEN",
                doc_url="https://github.com/settings/tokens",
                guidance="Settings → Developer settings → Personal access tokens (classic). Scopes: repo, read:user.",
            ),
            _ev("GITHUB_PAT", optional=True, guidance="Alias accepted by the GitHub toolkit."),
        ],
        "packages": ["PyGithub"],
    },
    # ── Git (clone/fetch/pull/push as a credentialed job container) ──
    "git": {
        "env_vars": [
            _ev(
                "GITHUB_TOKEN",
                optional=True,
                doc_url="https://github.com/settings/tokens",
                guidance=(
                    "Connect GitHub in Connections or ask the agent for a sign-in "
                    "link; public repos need nothing."
                ),
            )
        ],
    },
    # ── Slack ──
    "slack": {
        "env_vars": [
            _ev(
                "SLACK_BOT_TOKEN",
                doc_url="https://api.slack.com/apps",
                guidance="Create a Slack app → OAuth & Permissions → Bot Token Scopes (chat:write, channels:read, channels:history, search:read) → Install.",
            ),
            _ev("SLACK_TOKEN", optional=True, guidance="Alias accepted by the Slack toolkit."),
        ],
        "packages": ["slack-sdk"],
    },
    # ── Notion ──
    "notion": {
        "env_vars": [
            _ev(
                "NOTION_API_KEY",
                doc_url="https://www.notion.so/my-integrations",
                guidance="Create a new integration → copy the Internal Integration Token. Then share each Notion page/database with the integration.",
            ),
            _ev("NOTION_TOKEN", optional=True, guidance="Alias accepted by the Notion toolkit."),
        ],
        "packages": ["notion-client"],
    },
    # ── Linear ──
    "linear": {
        "env_vars": [
            _ev(
                "LINEAR_API_KEY",
                doc_url="https://linear.app/settings/api",
                guidance="Settings → API → Personal API keys → Create new key.",
            )
        ],
    },
    # ── Jira ──
    "jira": {
        "env_vars": [
            _ev(
                "JIRA_API_TOKEN",
                doc_url="https://id.atlassian.com/manage-profile/security/api-tokens",
                guidance="Atlassian account → Security → Create API token. Also set JIRA_EMAIL + JIRA_SERVER.",
            ),
            _ev("JIRA_EMAIL", optional=False),
            _ev("JIRA_SERVER", guidance="e.g. https://yourcompany.atlassian.net"),
            _ev(
                "JIRA_USER",
                optional=True,
                guidance="Alias of JIRA_EMAIL accepted by the Jira toolkit.",
            ),
        ],
    },
    # ── Finance: Yahoo Finance ──
    "yahoo": {
        "packages": ["yfinance"],
        "guidance": (
            "Yahoo Finance tools (yahoo_*) read public data via the yfinance "
            "library — no API key required."
        ),
    },
    # ── Finance: CoinGecko ──
    "coingecko": {
        "packages": ["requests"],
        "guidance": "CoinGecko tools (coingecko_*) use the free public API — no key needed.",
    },
    # ── Finance: Alpaca brokerage ──
    "alpaca": {
        "env_vars": [
            _ev(
                "ALPACA_PAPER_API_KEY",
                optional=True,
                doc_url="https://app.alpaca.markets/paper/dashboard/overview",
                guidance=(
                    "API key from the Alpaca PAPER dashboard. Used when an alpaca_* tool "
                    "is called with mode='paper' (the default)."
                ),
            ),
            _ev(
                "ALPACA_PAPER_SECRET_KEY",
                optional=True,
                guidance="Paper-trading secret key paired with ALPACA_PAPER_API_KEY.",
            ),
            _ev(
                "ALPACA_LIVE_API_KEY",
                optional=True,
                doc_url="https://app.alpaca.markets/brokerage/dashboard/overview",
                guidance=(
                    "API key from the Alpaca LIVE dashboard. Used only when an alpaca_* tool "
                    "is called with mode='live'. Paper and live keys are not interchangeable."
                ),
            ),
            _ev(
                "ALPACA_LIVE_SECRET_KEY",
                optional=True,
                guidance="Live-trading secret key paired with ALPACA_LIVE_API_KEY.",
            ),
            _ev(
                "ALPACA_API_KEY",
                optional=True,
                doc_url="https://app.alpaca.markets/paper/dashboard/overview",
                guidance=(
                    "Legacy fallback used when ALPACA_PAPER_API_KEY / ALPACA_LIVE_API_KEY are "
                    "unset. Combined with ALPACA_BASE_URL it determines paper vs. live."
                ),
            ),
            _ev(
                "ALPACA_SECRET_KEY",
                optional=True,
                guidance="Legacy fallback secret paired with ALPACA_API_KEY.",
            ),
            _ev(
                "ALPACA_BASE_URL",
                optional=True,
                guidance=(
                    "Only consulted when an alpaca_* tool is called WITHOUT an explicit "
                    "mode= argument and only legacy ALPACA_API_KEY/SECRET_KEY are set. "
                    "Defaults to https://paper-api.alpaca.markets. "
                    "Set to https://api.alpaca.markets for live trading."
                ),
            ),
        ],
        "packages": ["alpaca-trade-api"],
        "guidance": (
            "Alpaca tools default to PAPER trading and accept a `mode` argument per call:\n"
            "  • mode='paper' (default) → uses ALPACA_PAPER_* keys, paper-api.alpaca.markets\n"
            "  • mode='live'            → uses ALPACA_LIVE_*  keys, api.alpaca.markets\n"
            "  • mode=''                → follows legacy ALPACA_BASE_URL with ALPACA_* keys\n"
            "\n"
            "alpaca_place_order ALWAYS defaults to paper for safety. Switching to live "
            "requires the agent to explicitly pass mode='live' AND have ALPACA_LIVE_* "
            "credentials configured."
        ),
    },
    # ── Apollo.io ──
    "apollo": {
        "env_vars": [
            _ev(
                "APOLLO_API_KEY",
                doc_url="https://docs.apollo.io/docs/create-api-key",
                guidance=(
                    "In Apollo: Settings, Integrations, API keys, Create new key. A "
                    "master key unlocks every endpoint (listing users and API usage "
                    "need one); a limited key works only for the endpoints ticked "
                    "when it was created. Requests act as the workspace's "
                    "longest-standing admin."
                ),
            )
        ],
        "doc_url": "https://docs.apollo.io/reference/apollo-api",
        "guidance": (
            "ApolloAgent calls Apollo's REST API directly (Apollo ships no Python "
            "SDK). People search is free; enrichment, organization search, job "
            "postings and news spend the team's Apollo credits."
        ),
    },
    # ── Lusha ──
    "lusha": {
        "env_vars": [
            _ev(
                "LUSHA_API_KEY",
                doc_url="https://dashboard.lusha.com/api/manage-api-keys",
                guidance=(
                    "In the Lusha dashboard: Enrich, API, Manage API keys. The "
                    "account's plan must include API access, and the newer "
                    "endpoints (prospecting, lookalikes, signals, tables) need v3 "
                    "API access, which Lusha's account team enables."
                ),
            )
        ],
        "doc_url": "https://docs.lusha.com/api-reference/overview",
        "guidance": (
            "LushaAgent calls Lusha's REST API directly (Lusha ships no Python "
            "SDK). Search, enrichment, prospecting, lookalikes, signals, scores "
            "and table exports spend the account's Lusha credits."
        ),
    },
    # ── HubSpot ──
    "hubspot": {
        "env_vars": [
            _ev(
                "HUBSPOT_ACCESS_TOKEN",
                doc_url="https://developers.hubspot.com/docs/api/private-apps",
                guidance=(
                    "Easiest: connect HubSpot from the dashboard Connections tab (OAuth; "
                    "tokens refresh automatically). Or a private-app token: Settings → "
                    "Integrations → Private Apps → Create, with at least "
                    "crm.objects.contacts/companies/deals .read/.write, "
                    "crm.objects.owners.read, tickets, e-commerce and "
                    "crm.objects.line_items.read/write."
                ),
            )
        ],
        "packages": ["hubspot-api-client"],
        "doc_url": "https://github.com/HubSpot/hubspot-api-python",
        "guidance": (
            "HubSpotAgent uses the official HubSpot Python SDK with a single "
            "access token, no MCP server required: the HubSpot connector's OAuth "
            "token (Connections tab; refreshed automatically) or a pasted "
            "private-app token. All actions respect the token's scopes and the "
            "user's HubSpot permissions; a feature the account's plan lacks "
            "answers with a missing-scope message rather than a failed sign-in."
        ),
    },
    # ── Intuit QuickBooks / PayPal / Canva / DocuSign / Microsoft 365 ──
    "quickbooks": {
        "env_vars": [
            _ev(
                "QUICKBOOKS_ACCESS_TOKEN",
                doc_url="https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization",
            ),
            _ev(
                "QUICKBOOKS_REALM_ID",
                guidance="QuickBooks Online company/realm ID selected during OAuth.",
            ),
            _ev(
                "QUICKBOOKS_ENVIRONMENT",
                optional=True,
                guidance="Use 'sandbox' for Intuit sandbox; defaults to production.",
            ),
        ],
        "packages": ["httpx"],
        "doc_url": "https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/account",
        "guidance": (
            "QuickBooks tools use an OAuth access token and realm ID. Prefer sandbox "
            "credentials while testing financial writes. Or connect via the dashboard "
            "Connections tab — tokens are minted and refreshed automatically, and the "
            "realm ID is captured for you."
        ),
    },
    "paypal": {
        "env_vars": [
            _ev(
                "PAYPAL_CLIENT_ID",
                doc_url="https://developer.paypal.com/dashboard/applications/sandbox",
            ),
            _ev("PAYPAL_CLIENT_SECRET"),
            _ev(
                "PAYPAL_ACCESS_TOKEN",
                optional=True,
                guidance="Optional pre-minted bearer token; otherwise client credentials are used.",
            ),
            _ev("PAYPAL_MODE", optional=True, guidance="sandbox (default) or live."),
        ],
        "packages": ["httpx"],
        "doc_url": "https://developer.paypal.com/api/rest/",
        "guidance": "PayPal tools default to sandbox mode. Set PAYPAL_MODE=live only for intentional production activity.",
    },
    "canva": {
        "env_vars": [
            _ev("CANVA_ACCESS_TOKEN", doc_url="https://www.canva.dev/docs/connect/authentication/"),
        ],
        "packages": ["httpx"],
        "doc_url": "https://www.canva.dev/docs/connect/api-reference/",
    },
    "docusign": {
        "env_vars": [
            _ev("DOCUSIGN_ACCESS_TOKEN", doc_url="https://developers.docusign.com/platform/auth/"),
            _ev("DOCUSIGN_ACCOUNT_ID"),
            _ev(
                "DOCUSIGN_BASE_URL",
                optional=True,
                guidance="Defaults to https://demo.docusign.net/restapi. Use account base_uri + /restapi for production.",
            ),
        ],
        "packages": ["httpx"],
        "doc_url": "https://developers.docusign.com/docs/esign-rest-api/",
        "guidance": (
            "DocuSign tools default to demo base URL. Verify recipients and documents "
            "before sending envelopes. Or connect via the dashboard Connections tab — "
            "tokens are minted and refreshed automatically, and the account ID + base "
            "URL are captured for you."
        ),
    },
    "microsoft365": {
        "env_vars": [
            _ev("MS365_ACCESS_TOKEN", doc_url="https://learn.microsoft.com/graph/auth/"),
            _ev(
                "MICROSOFT_GRAPH_ACCESS_TOKEN",
                optional=True,
                guidance="Alias accepted by the Microsoft 365 toolkit.",
            ),
        ],
        "packages": ["httpx"],
        "doc_url": "https://learn.microsoft.com/graph/overview",
        "guidance": (
            "Microsoft 365 tools call Microsoft Graph. Token scopes depend on use: "
            "Mail.ReadWrite/Send, MailboxSettings.ReadWrite, Calendars.ReadWrite (+ "
            "Calendars.Read.Shared for meeting times), People.Read, User.ReadBasic.All, "
            "Contacts.ReadWrite, Tasks.ReadWrite, Files.ReadWrite.All, Sites.Read.All, "
            "Team.ReadBasic.All, Channel.ReadBasic.All, ChannelMessage.Send, Chat.ReadWrite. "
            "Or connect via the dashboard Connections tab: tokens are minted and refreshed "
            "automatically, and a connection made before a scope was added needs one fresh "
            "sign-in to grant it."
        ),
    },
    # ── Airtable ──
    "airtable": {
        "env_vars": [
            _ev(
                "AIRTABLE_API_KEY",
                doc_url="https://airtable.com/create/tokens",
                guidance="Account → Developer hub → Personal access tokens → Create. Add scopes data.records:read/write and the bases you need.",
            ),
            _ev(
                "AIRTABLE_TOKEN", optional=True, guidance="Alias accepted by the Airtable toolkit."
            ),
        ],
        "guidance": "Base ids (appXXXXXXXXXXXXXX, from a base URL) are passed per call, not configured.",
    },
    # ── Wallet (Web3) ──
    "wallet_cold": {
        "env_vars": [
            _ev(
                "ETH_RPC_URL",
                doc_url="https://www.alchemy.com/",
                guidance="A free Alchemy or Infura JSON-RPC URL works.",
            ),
            _ev("ETHERSCAN_API_KEY", doc_url="https://etherscan.io/apis"),
        ],
        "packages": ["web3", "eth-account"],
    },
    "wallet_hot": {
        "env_vars": [
            _ev("ETH_RPC_URL"),
            _ev("ETHERSCAN_API_KEY"),
        ],
        "packages": ["web3", "eth-account"],
    },
    # ── Google ──
    "google": {
        "env_vars": [
            _ev(
                "GOOGLE_OAUTH_CLIENT",
                optional=True,
                doc_url="https://console.cloud.google.com/apis/credentials",
                guidance=(
                    'JSON with your OAuth client: {"client_id": …, "client_secret": …} '
                    "(pasting the downloaded client_secret.json verbatim also works). "
                    "Required for the first-time consent flow; not needed afterwards. "
                    "The GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET pair works too."
                ),
            ),
            _ev(
                "GOOGLE_OAUTH_CREDENTIALS",
                optional=True,
                doc_url="https://console.cloud.google.com/apis/credentials",
                guidance=(
                    "Legacy alternative to GOOGLE_OAUTH_CLIENT: path to your downloaded "
                    "OAuth client_secret.json."
                ),
            ),
        ],
        "packages": [
            "google-api-python-client",
            "google-auth-oauthlib",
            "google-auth-httplib2",
        ],
        "doc_url": ("https://developers.google.com/workspace/guides/create-credentials"),
        "guidance": (
            "Tools cover Gmail (read/send/search/labels/drafts/trash), Calendar "
            "(events, freebusy, quick-add, list calendars), Drive (list/upload/download/"
            "search/share/folders/delete), Docs (create/read/append/insert/replace), and "
            "Sheets (list/create/read/write/append/clear, tabs, rows & columns, "
            "find-replace, formatting, freeze panes, CSV export).\n"
            "\n"
            "Setup:\n"
            "  1. Create an OAuth 2.0 Client ID in Google Cloud Console — type Web "
            "application for the dashboard Connect flow (register the "
            "/api/auth/google/callback redirect URI), or Desktop app for the local "
            "`olano auth google` browser flow.\n"
            "  2. Enable: Gmail API, Google Calendar API, Google Drive API, Google Docs "
            "API, Google Sheets API.\n"
            "  3. Store the client: `olano vault set GOOGLE_OAUTH_CLIENT` with "
            '{"client_id": …, "client_secret": …} (pasting the downloaded '
            "client_secret.json verbatim also works) — or the legacy "
            "GOOGLE_OAUTH_CREDENTIALS=/path/to/client_secret.json.\n"
            "  4. Connect from the dashboard Connections tab, or run `olano auth google` "
            "once — the cached token is stored under the active Olano home.\n"
            "\n"
            "Scopes: gmail.readonly, gmail.send, gmail.modify, calendar, drive, documents, "
            "spreadsheets.\n"
            "If you upgraded from an older Olano version, re-run `olano auth google` "
            "so the new gmail.modify + documents scopes are granted. The Sheets tools need "
            "no re-auth — the Sheets API also accepts the drive scope existing tokens "
            "already carry — but the Google Sheets API must be enabled on the project.\n"
            "Or connect via the dashboard Connections tab — tokens are minted and "
            "refreshed automatically."
        ),
    },
    # ── Scheduler & ops ── no external deps; uses local APScheduler.
    "scheduler": {"packages": ["apscheduler"]},
    "ops": {"packages": ["apscheduler"]},
    # ── Trello ──
    "trello": {
        "env_vars": [
            _ev("TRELLO_API_KEY", doc_url="https://trello.com/app-key"),
            _ev("TRELLO_TOKEN", guidance="Generate a Token from the same page as your API key."),
        ],
    },
    # ── Zapier ──
    "zapier": {
        "env_vars": [
            _ev(
                "ZAPIER_NLA_API_KEY",
                doc_url="https://nla.zapier.com/credentials/",
                guidance="Sign up for Zapier NLA (Natural Language Actions) → API key.",
            ),
            _ev("ZAPIER_API_KEY", optional=True, guidance="Alias accepted by the Zapier toolkit."),
        ],
    },
    # ── Notifications (Telegram / channel sender uses runtime channel config; no key here) ──
    "notification": {},
    # ── GCloud ──
    "gcloud": {
        "cli": ["gcloud"],
        "guidance": "Install Google Cloud SDK and run `gcloud auth application-default login` once.",
    },
    # ── X (Twitter) ──
    "x_twitter": {
        "env_vars": [
            _ev("X_BEARER_TOKEN", optional=True),
            _ev(
                "X_API_KEY",
                doc_url="https://developer.twitter.com/en/portal/dashboard",
                guidance=(
                    "X Developer Portal → Project & Apps → Keys and tokens. Or connect "
                    "via the dashboard Connections tab — a user-context OAuth token is "
                    "minted and refreshed automatically."
                ),
            ),
            _ev("X_API_SECRET"),
            _ev("X_ACCESS_TOKEN", optional=True),
            _ev("X_ACCESS_SECRET", optional=True),
            _ev(
                "X_USER_ACCESS_TOKEN",
                optional=True,
                guidance="User-context OAuth token minted by the dashboard Connections tab.",
            ),
        ],
    },
    "tiktok": {
        "env_vars": [
            _ev("TIKTOK_ACCESS_TOKEN", doc_url="https://developers.tiktok.com/"),
            _ev("TIKTOK_API_BASE_URL", guidance="Approved TikTok API or partner gateway base URL."),
        ],
        "packages": ["requests"],
    },
    "douyin": {
        "env_vars": [
            _ev("DOUYIN_ACCESS_TOKEN", doc_url="https://developer.open-douyin.com/"),
            _ev("DOUYIN_API_BASE_URL", guidance="Approved Douyin API or partner gateway base URL."),
        ],
        "packages": ["requests"],
    },
    "rednote": {
        "env_vars": [
            _ev("REDNOTE_ACCESS_TOKEN", doc_url="https://open.xiaohongshu.com/"),
            _ev(
                "REDNOTE_API_BASE_URL",
                guidance="Approved Xiaohongshu API or partner gateway base URL.",
            ),
        ],
        "packages": ["requests"],
    },
    # ── LinkedIn / Buffer / Mailchimp / YouTube ──
    "linkedin": {
        "env_vars": [
            _ev(
                "LINKEDIN_ACCESS_TOKEN",
                doc_url="https://www.linkedin.com/developers/apps",
                guidance=(
                    "Create a LinkedIn app → request Marketing Developer Platform access "
                    "→ 3-legged OAuth. Or connect via the dashboard Connections tab — "
                    "the token is minted automatically (LinkedIn has no programmatic "
                    "refresh; reconnect when it expires, ~60 days)."
                ),
            )
        ],
    },
    "buffer": {
        "env_vars": [
            _ev(
                "BUFFER_ACCESS_TOKEN",
                doc_url="https://buffer.com/developers/api",
                guidance=(
                    "Settings → Apps → Create access token. Or connect via the dashboard "
                    "Connections tab (grandfathered OAuth apps only — new Buffer app "
                    "registration is closed)."
                ),
            ),
            _ev(
                "BUFFER_ORGANIZATION_ID",
                optional=True,
                guidance="Organization to post as when the account has several.",
            ),
        ],
    },
    "mailchimp": {
        "env_vars": [
            _ev(
                "MAILCHIMP_API_KEY",
                doc_url="https://us1.admin.mailchimp.com/account/api/",
                guidance="Account → Extras → API keys → Create. The trailing `-us1` part is the data center.",
            ),
            _ev(
                "MAILCHIMP_SERVER",
                optional=True,
                guidance="Data-center prefix, e.g. us1 (default us1).",
            ),
        ],
    },
    "youtube": {
        "env_vars": [
            _ev(
                "YOUTUBE_API_KEY",
                doc_url="https://console.cloud.google.com/apis/credentials",
                guidance=(
                    "Google Cloud Console → APIs & Services → Credentials → Create API "
                    "key. Enable YouTube Data API v3. Or connect Google via the "
                    "dashboard Connections tab — YouTube tools fall back to the Google "
                    "OAuth token when no API key is set."
                ),
            )
        ],
    },
    # ── Image / media ──
    "image": {
        "env_vars": [
            _ev(
                "OPENAI_API_KEY",
                optional=True,
                doc_url="https://platform.openai.com/api-keys",
                guidance=(
                    "Not needed on a managed deployment — image generation runs on "
                    "the Olano image service, billed in credits, and stays there "
                    "even when a key is present. Set a key (this or "
                    "GOOGLE_API_KEY / GEMINI_API_KEY) to run on your own account "
                    "on an unmanaged install, or together with "
                    "LIONCLAW_IMAGE_MODEL=openai:gpt-image-2.5-flare to switch "
                    "a managed one over."
                ),
            ),
            _ev(
                "GOOGLE_API_KEY",
                optional=True,
                doc_url="https://aistudio.google.com/apikey",
                guidance="Gemini image backend (nanobanana tools). GEMINI_API_KEY is accepted too.",
            ),
            _ev("GEMINI_API_KEY", optional=True, guidance="Alias of GOOGLE_API_KEY."),
        ],
    },
    "media": {
        "env_vars": [
            _ev("OPENAI_API_KEY", doc_url="https://platform.openai.com/api-keys"),
        ],
        "cli": ["ffmpeg"],
    },
    # ── Shopify / Stripe / Ecommerce ──
    "shopify": {
        "env_vars": [
            _ev(
                "SHOPIFY_ACCESS_TOKEN",
                doc_url="https://shopify.dev/docs/apps/auth/admin-app-access-tokens",
                guidance=(
                    "Custom app → API credentials → Admin API access token. Or connect "
                    "via the dashboard Connections tab — a permanent per-shop token is "
                    "minted automatically."
                ),
            ),
            _ev("SHOPIFY_STORE", guidance="e.g. yourshop.myshopify.com"),
        ],
    },
    "stripe": {
        "env_vars": [
            _ev(
                "STRIPE_API_KEY",
                doc_url="https://dashboard.stripe.com/apikeys",
                guidance="Use a restricted key in production. Test mode keys start with sk_test_…",
            )
        ],
        "packages": ["stripe"],
    },
    # ── FRED ──
    "fred": {
        "env_vars": [
            _ev(
                "FRED_API_KEY",
                doc_url="https://fredaccount.stlouisfed.org/apikeys",
                guidance="Free St. Louis Fed account → request an API key.",
            )
        ],
    },
    # ── Asana / Salesforce / Coinbase ──
    "asana": {
        "env_vars": [
            _ev(
                "ASANA_ACCESS_TOKEN",
                doc_url="https://app.asana.com/0/my-apps",
                guidance="My profile → Apps → Personal access tokens.",
            ),
            _ev(
                "ASANA_DEFAULT_WORKSPACE",
                optional=True,
                guidance="Workspace gid used when a call names none.",
            ),
        ],
    },
    "salesforce": {
        "env_vars": [
            _ev(
                "SALESFORCE_ACCESS_TOKEN",
                doc_url="https://help.salesforce.com/s/articleView?id=sf.connected_app_create_api_integration.htm",
                guidance=(
                    "Create a Connected App → enable OAuth → use a session token or "
                    "refresh-token flow. Or connect via the dashboard Connections tab — "
                    "tokens are minted and refreshed automatically, and the instance URL "
                    "is captured for you."
                ),
            ),
            _ev("SALESFORCE_INSTANCE_URL", guidance="e.g. https://yourcompany.my.salesforce.com"),
        ],
        "packages": ["simple-salesforce"],
    },
    "coinbase": {
        "env_vars": [
            _ev(
                "COINBASE_API_KEY",
                doc_url="https://cloud.coinbase.com/access/api",
                guidance=(
                    "Coinbase Cloud → API → Create key for *Advanced Trade*. "
                    "Pair with COINBASE_API_SECRET."
                ),
            ),
            _ev("COINBASE_API_SECRET"),
        ],
        "packages": ["httpx"],
        "guidance": (
            "⚠ Coinbase Advanced Trade has NO public sandbox — all orders execute on "
            "production with real funds. Use small notional sizes for testing. "
            "For full paper-trading workflows, prefer Binance/Bybit/OKX/Alpaca."
        ),
    },
    # ── Binance ──
    "binance": {
        "env_vars": [
            _ev(
                "BINANCE_TESTNET_API_KEY",
                optional=True,
                doc_url="https://testnet.binance.vision/",
                guidance=(
                    "Testnet API key (paper trading). Used by default for ALL tools. "
                    "Sign in with GitHub at testnet.binance.vision to generate keys."
                ),
            ),
            _ev("BINANCE_TESTNET_API_SECRET", optional=True),
            _ev(
                "BINANCE_API_KEY",
                optional=True,
                doc_url="https://www.binance.com/en/my/settings/api-management",
                guidance=(
                    "Live API key. Only used when a tool is called with mode='live'. "
                    "Also serves as fallback when BINANCE_TESTNET_API_KEY is unset."
                ),
            ),
            _ev("BINANCE_API_SECRET", optional=True),
        ],
        "packages": ["httpx"],
        "guidance": (
            "All tools (including public market data) DEFAULT TO mode='testnet' for "
            "safety. Pass mode='live' explicitly to trade real funds or read live "
            "production prices. Testnet uses fake balances on testnet.binance.vision."
        ),
    },
    # ── Bybit ──
    "bybit": {
        "env_vars": [
            _ev(
                "BYBIT_TESTNET_API_KEY",
                optional=True,
                doc_url="https://testnet.bybit.com/app/user/api-management",
                guidance=("Testnet API key (paper trading). Used by default for ALL tools."),
            ),
            _ev("BYBIT_TESTNET_API_SECRET", optional=True),
            _ev(
                "BYBIT_API_KEY",
                optional=True,
                doc_url="https://www.bybit.com/app/user/api-management",
                guidance=("Live API key. Only used when a tool is called with mode='live'."),
            ),
            _ev("BYBIT_API_SECRET", optional=True),
        ],
        "packages": ["httpx"],
        "guidance": (
            "Bybit V5 unified API (spot, linear, inverse, options). All tools "
            "DEFAULT TO mode='testnet' for safety. Pass mode='live' explicitly to "
            "trade real funds. Testnet credentials are minted at testnet.bybit.com."
        ),
    },
    # ── OKX ──
    "okx": {
        "env_vars": [
            _ev(
                "OKX_DEMO_API_KEY",
                optional=True,
                doc_url="https://www.okx.com/account/my-api",
                guidance=(
                    "Demo-trading API key (paper trading). Used by default for ALL "
                    "tools. In OKX, switch to *Demo Trading* and create a fresh API "
                    "key from there. Falls back to OKX_API_KEY if unset."
                ),
            ),
            _ev("OKX_DEMO_API_SECRET", optional=True),
            _ev("OKX_DEMO_API_PASSPHRASE", optional=True),
            _ev(
                "OKX_API_KEY",
                optional=True,
                doc_url="https://www.okx.com/account/my-api",
                guidance="Live OKX API key. Only used when a tool is called with mode='live'.",
            ),
            _ev("OKX_API_SECRET", optional=True),
            _ev(
                "OKX_API_PASSPHRASE",
                optional=True,
                guidance="Passphrase set when creating the live API key — not your account password.",
            ),
        ],
        "packages": ["httpx"],
        "guidance": (
            "OKX V5 API. All tools DEFAULT TO mode='demo' for safety. Pass "
            "mode='live' explicitly to trade real funds. Demo mode adds the "
            "x-simulated-trading header and uses demo-trading API keys."
        ),
    },
    # ── Kraken ──
    "kraken": {
        "env_vars": [
            _ev(
                "KRAKEN_API_KEY",
                doc_url="https://www.kraken.com/u/security/api",
                guidance="Account → Security → API. Required permissions depend on tool usage.",
            ),
            _ev(
                "KRAKEN_API_SECRET",
                guidance="Kraken returns a base64-encoded private key — paste it as-is.",
            ),
        ],
        "packages": ["httpx"],
        "guidance": (
            "Kraken Spot REST API. Kraken has no sandbox — kraken_place_order "
            "DEFAULTS TO dry_run=True (validate-only) as the paper-equivalent. "
            "Pass dry_run=False to submit a real order against the live exchange."
        ),
    },
    # ── dYdX (v4 Cosmos chain) ──
    "dydx": {
        "packages": ["httpx"],
        "guidance": (
            "dYdX v4 read-only tools via the public indexer. Trading on v4 requires "
            "signing Cosmos transactions with a wallet mnemonic and is not exposed "
            "by this toolkit. Each tool accepts mode='mainnet' (default) or "
            "mode='testnet'. To inspect your own positions/orders, pass your "
            "Cosmos address (dydx1...)."
        ),
    },
    # ── Polymarket ──
    "polymarket": {
        "packages": ["httpx"],
        "guidance": (
            "Polymarket read-only tools using the public Gamma (events) and CLOB "
            "(prices, orderbook) APIs. Trading via the CLOB requires EVM wallet "
            "signing on Polygon and is not exposed by this toolkit."
        ),
    },
    # ── Interactive Brokers ──
    "ibkr": {
        "env_vars": [
            _ev(
                "IBKR_HOST",
                optional=True,
                guidance="Host where TWS/Gateway is running. Defaults to 127.0.0.1.",
            ),
            _ev(
                "IBKR_PAPER_PORT",
                optional=True,
                guidance="Port for paper trading. Defaults to 7497 (TWS) — IB Gateway uses 4002.",
            ),
            _ev(
                "IBKR_LIVE_PORT",
                optional=True,
                guidance="Port for live trading. Defaults to 7496 (TWS) — IB Gateway uses 4001.",
            ),
            _ev(
                "IBKR_CLIENT_ID",
                optional=True,
                guidance="Unique client ID for the API connection. Defaults to 42.",
            ),
        ],
        "packages": ["ib_insync"],
        "doc_url": "https://www.interactivebrokers.com/en/trading/tws.php",
        "guidance": (
            "Requires Trader Workstation (TWS) or IB Gateway running locally with the "
            "API enabled (Edit → Global Configuration → API → Settings).\n"
            "  • mode='paper' (default) → port 7497 (TWS) / 4002 (Gateway)\n"
            "  • mode='live'            → port 7496 (TWS) / 4001 (Gateway)\n"
            "Real-time market data tools require an IBKR market data subscription."
        ),
    },
    # ── Feishu / DingTalk / WeCom (Chinese collaboration suites) ──
    "feishu": {
        "env_vars": [
            _ev("FEISHU_APP_ID"),
            _ev(
                "FEISHU_APP_SECRET",
                doc_url="https://open.feishu.cn/app",
                guidance="Lark/Feishu Developer Console → create app → App Credentials.",
            ),
        ],
    },
    "dingtalk": {
        "env_vars": [
            _ev("DINGTALK_APP_KEY"),
            _ev("DINGTALK_APP_SECRET", doc_url="https://open.dingtalk.com/"),
            _ev("DINGTALK_ROBOT_TOKEN", optional=True, guidance="Only for robot-message tools."),
            _ev("DINGTALK_ROBOT_SECRET", optional=True),
            _ev("DINGTALK_AGENT_ID", optional=True, guidance="Only for work-notification tools."),
        ],
    },
    # WhatsApp Business, on either business connector. The everyday tools run
    # on the agent's own connector (Twilio or Meta Cloud API) and need nothing
    # else; the admin tools read the account from the vault when the agent
    # that manages it has no connector of its own. Which pair it needs depends
    # on the provider, so every key is optional here and the guidance says which.
    "whatsapp_business": {
        "env_vars": [
            _ev(
                "TWILIO_ACCOUNT_SID",
                optional=True,
                doc_url="https://console.twilio.com/",
                guidance=(
                    "For a Twilio account: Twilio Console home page, Account Info panel "
                    "(starts with AC). Not needed when this agent has a WhatsApp connector."
                ),
            ),
            _ev(
                "TWILIO_AUTH_TOKEN",
                optional=True,
                doc_url="https://console.twilio.com/",
                guidance="For a Twilio account: Twilio Console home page, Account Info panel.",
            ),
            _ev(
                "WHATSAPP_META_ACCESS_TOKEN",
                optional=True,
                doc_url="https://business.facebook.com/settings/system-users",
                guidance=(
                    "For Meta's Cloud API: a system user token with whatsapp_business_messaging "
                    "and whatsapp_business_management (Business settings, System users). Not "
                    "needed when this agent has a WhatsApp connector."
                ),
            ),
            _ev(
                "WHATSAPP_META_WABA_ID",
                optional=True,
                doc_url="https://developers.facebook.com/apps/",
                guidance="For Meta's Cloud API: Meta App Dashboard, WhatsApp, API Setup.",
            ),
        ],
        "guidance": (
            "WhatsApp Business, through Twilio or Meta's Cloud API. The everyday tools (list and "
            "send approved templates, message and window status) come with either WhatsApp "
            "business connector and use its account. The admin tools (create, submit, check and "
            "delete templates; the account's numbers and business profile; on Twilio, balance "
            "and usage) act on the whole account and are switched on by an administrator only, "
            "in the connector panel or here. An agent without a connector needs its account's "
            "keys in the vault: the two Twilio keys, or the Meta token and Business Account ID."
        ),
    },
    "wecom": {
        "env_vars": [
            _ev(
                "WECOM_CORP_ID",
                doc_url="https://developer.work.weixin.qq.com/",
                guidance="WeCom (Enterprise WeChat) → Manage app → Corp ID.",
            ),
            _ev("WECOM_AGENT_ID"),
            _ev("WECOM_AGENT_SECRET"),
            _ev("WECOM_ROBOT_KEY", optional=True),
        ],
    },
    # ── Health: Fitbit ──
    "fitbit": {
        "env_vars": [
            _ev("FITBIT_ACCESS_TOKEN", doc_url="https://dev.fitbit.com/apps"),
        ],
        "guidance": (
            "Fitbit needs OAuth; run `olano oauth fitbit`. Or connect via the "
            "dashboard Connections tab — tokens are minted and refreshed automatically."
        ),
    },
    # ── Health: Garmin Connect ──
    "garmin": {
        "env_vars": [
            _ev("GARMIN_EMAIL"),
            _ev("GARMIN_PASSWORD"),
        ],
        "packages": ["garminconnect"],
        "guidance": "Garmin reads via the garminconnect library using account email + password.",
    },
    # ── Health: cross-source aggregator ──
    "health": {
        "env_vars": [
            _ev("FITBIT_ACCESS_TOKEN", optional=True, doc_url="https://dev.fitbit.com/apps"),
            _ev("GARMIN_EMAIL", optional=True),
            _ev("GARMIN_PASSWORD", optional=True),
        ],
        "guidance": "Aggregates whichever Fitbit / Garmin sources are configured.",
    },
    # ── Memory graph / notes / secrets — local; no requirements ──
    "graph": {},
    "notes": {},
    "secrets": {},
    # ── Toolkits that read a key the catalog used to say nothing about ──
    # (``tests/test_toolkit_requirements.py`` pins every get_credential name a
    # toolkit reads to its entry here — the integration finder and the catalog
    # badge both derive "what does this need" from this map.)
    "web_search": {
        "env_vars": [
            _ev(
                "TAVILY_API_KEY",
                optional=True,
                doc_url="https://tavily.com/",
                guidance=(
                    "Not needed on a managed deployment (search runs on the Olano "
                    "service, billed in credits). Elsewhere: sign up free → Settings → "
                    "API Keys."
                ),
            ),
        ],
    },
    "firecrawl": {
        "env_vars": [
            _ev(
                "FIRECRAWL_API_KEY",
                doc_url="https://www.firecrawl.dev/",
                guidance="Sign up → API keys.",
            ),
        ],
    },
    "sql": {
        "env_vars": [
            _ev(
                "LIONCLAW_SQL_DSN",
                guidance=(
                    "SQLAlchemy connection string, e.g. postgresql://user:pass@host/db "
                    "or sqlite:////path/to/file.db."
                ),
            ),
        ],
        "packages": ["sqlalchemy"],
    },
    "e2b": {
        "env_vars": [_ev("E2B_API_KEY", doc_url="https://e2b.dev/dashboard")],
        "packages": ["e2b-code-interpreter"],
    },
    "composio": {
        "env_vars": [_ev("COMPOSIO_API_KEY", doc_url="https://app.composio.dev/settings")],
    },
    "alpha_vantage": {
        "env_vars": [
            _ev(
                "ALPHA_VANTAGE_API_KEY",
                doc_url="https://www.alphavantage.co/support/#api-key",
                guidance="Free key from the Alpha Vantage site.",
            ),
            _ev("ALPHAVANTAGE_API_KEY", optional=True, guidance="Alias of ALPHA_VANTAGE_API_KEY."),
        ],
    },
    "polygon": {
        "env_vars": [_ev("POLYGON_API_KEY", doc_url="https://polygon.io/dashboard/api-keys")],
    },
    "weather": {
        "env_vars": [
            _ev(
                "OPENWEATHER_API_KEY",
                doc_url="https://home.openweathermap.org/api_keys",
                guidance="OpenWeatherMap → API keys.",
            ),
            _ev("OPENWEATHERMAP_API_KEY", optional=True, guidance="Alias of OPENWEATHER_API_KEY."),
        ],
    },
    "wolfram": {
        "env_vars": [
            _ev(
                "WOLFRAM_APP_ID",
                doc_url="https://developer.wolframalpha.com/access",
                guidance="Wolfram|Alpha developer portal → Get an AppID.",
            ),
            _ev("WOLFRAMALPHA_APP_ID", optional=True, guidance="Alias of WOLFRAM_APP_ID."),
        ],
    },
    "pubmed": {
        "env_vars": [
            _ev(
                "PUBMED_EMAIL",
                optional=True,
                guidance="Contact e-mail NCBI asks E-utilities callers to send (LIONCLAW_USER_EMAIL is used otherwise).",
            ),
            _ev(
                "PUBMED_API_KEY",
                optional=True,
                doc_url="https://www.ncbi.nlm.nih.gov/account/settings/",
                guidance="Optional NCBI API key — raises the rate limit; searches work without it.",
            ),
        ],
    },
    "reddit": {
        "env_vars": [
            _ev(
                "REDDIT_CLIENT_ID",
                doc_url="https://www.reddit.com/prefs/apps",
                guidance="Create a script-type app → client id (under the app name). Or connect via the dashboard Connections tab.",
            ),
            _ev("REDDIT_CLIENT_SECRET"),
            _ev(
                "REDDIT_REFRESH_TOKEN",
                optional=True,
                guidance="OAuth refresh token (minted by the Connections tab).",
            ),
            _ev(
                "REDDIT_USERNAME",
                optional=True,
                guidance="Password-grant fallback for a script app.",
            ),
            _ev("REDDIT_PASSWORD", optional=True),
            _ev("REDDIT_USER_AGENT", optional=True, guidance="Defaults to olano-bot/1.0."),
        ],
    },
    "wallet": {
        "env_vars": [
            _ev(
                "ETH_RPC_URL",
                optional=True,
                guidance="Ethereum JSON-RPC endpoint (default: a public Infura placeholder — set your own).",
            ),
            _ev(
                "BSC_RPC_URL",
                optional=True,
                guidance="BNB Smart Chain RPC (default: public dataseed).",
            ),
            _ev(
                "POLYGON_RPC_URL",
                optional=True,
                guidance="Polygon RPC (default: public polygon-rpc.com).",
            ),
            _ev(
                "ETHERSCAN_API_KEY",
                optional=True,
                doc_url="https://etherscan.io/myapikey",
                guidance="Only for transaction history lookups.",
            ),
        ],
    },
}


# Per-tool overrides. Use sparingly: only for tools that diverge from their
# bundle (e.g. firecrawl tools live in no bundle, vision tools need OpenAI).
TOOL_OVERRIDES: dict[str, Requirements] = {
    "firecrawl_scrape": {
        "env_vars": [
            _ev(
                "FIRECRAWL_API_KEY",
                doc_url="https://www.firecrawl.dev/",
                guidance="Sign up → API keys.",
            ),
        ],
    },
    "firecrawl_crawl": {
        "env_vars": [_ev("FIRECRAWL_API_KEY", doc_url="https://www.firecrawl.dev/")],
    },
    "firecrawl_map": {
        "env_vars": [_ev("FIRECRAWL_API_KEY", doc_url="https://www.firecrawl.dev/")],
    },
    "firecrawl_extract": {
        "env_vars": [_ev("FIRECRAWL_API_KEY", doc_url="https://www.firecrawl.dev/")],
    },
    # Any ONE of these unlocks vision on an unmanaged box, and none is needed
    # on a managed one — so every entry is individually optional. Marking one
    # required would report a box with a working OpenAI key as broken.
    "image_describe": _VISION_REQUIREMENTS,
    "screenshot_url": {
        "env_vars": [
            _ev(
                "SCREENSHOTONE_API_KEY",
                optional=True,
                doc_url="https://screenshotone.com/dashboard/access",
                guidance="Optional: use ScreenshotOne for high-quality URL → image. Falls back to playwright if unset.",
            )
        ],
    },
    "page_screenshot": {"packages": ["playwright"]},
    "browser_screenshot": {"packages": ["playwright"]},
    "browser_record": {"packages": ["playwright"]},
    # web tools beyond search
    "page_read": {},
    "page_extract": {"env_vars": [_ev("TAVILY_API_KEY", doc_url="https://tavily.com/")]},
    "web_extract": {"env_vars": [_ev("TAVILY_API_KEY", doc_url="https://tavily.com/")]},
    "web_extract_deep": {"env_vars": [_ev("TAVILY_API_KEY", doc_url="https://tavily.com/")]},
    # ── Finance per-tool overrides ──
    # CoinGecko tools — no API key; public rate limit is ~30 req/min on the free tier.
    "coingecko_get_price": {"packages": ["requests"]},
    "coingecko_trending": {"packages": ["requests"]},
    "coingecko_top_coins": {"packages": ["requests"]},
    "coingecko_get_coin_info": {"packages": ["requests"]},
    "coingecko_get_ohlc": {"packages": ["requests"]},
    "coingecko_search": {"packages": ["requests"]},
    # Yahoo Finance tools — no API key; yfinance talks to the Yahoo Finance endpoints.
    "yahoo_get_quote": {"packages": ["yfinance"]},
    "yahoo_get_history": {"packages": ["yfinance"]},
    "yahoo_get_financials": {"packages": ["yfinance"]},
    "yahoo_get_dividends": {"packages": ["yfinance"]},
    "yahoo_get_news": {"packages": ["yfinance"]},
    "yahoo_compare_symbols": {"packages": ["yfinance"]},
    "yahoo_get_options_summary": {"packages": ["yfinance"]},
    # Alpaca tools — paper-by-default. Each tool accepts mode='paper'|'live' at call time.
    # Paper credentials: ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY (recommended)
    # Live credentials:  ALPACA_LIVE_API_KEY  / ALPACA_LIVE_SECRET_KEY
    # Legacy fallback for both: ALPACA_API_KEY / ALPACA_SECRET_KEY (+ ALPACA_BASE_URL).
    "alpaca_place_order": {
        "packages": ["alpaca-trade-api"],
        "guidance": (
            "Submits a real order against the chosen Alpaca environment. "
            "Defaults to mode='paper' for safety — you must pass mode='live' AND have "
            "ALPACA_LIVE_API_KEY + ALPACA_LIVE_SECRET_KEY configured to trade real money. "
            "Double-check symbol, qty, and side before invoking."
        ),
    },
    "alpaca_cancel_order": {"packages": ["alpaca-trade-api"]},
    "alpaca_get_account": {"packages": ["alpaca-trade-api"]},
    "alpaca_list_positions": {"packages": ["alpaca-trade-api"]},
    "alpaca_list_orders": {"packages": ["alpaca-trade-api"]},
    "alpaca_get_bars": {"packages": ["alpaca-trade-api"]},
    "alpaca_get_portfolio_history": {"packages": ["alpaca-trade-api"]},
}


def _merge(a: Requirements, b: Requirements) -> Requirements:
    """Merge two requirement dicts (b takes precedence on scalar fields)."""
    out: Requirements = {}
    # env_vars: union by name, preserve order
    seen_env: set[str] = set()
    env_vars: list[EnvVarSpec] = []
    for source in (a.get("env_vars") or [], b.get("env_vars") or []):
        for spec in source:
            n = spec.get("name", "")
            if n and n not in seen_env:
                seen_env.add(n)
                env_vars.append(spec)
    if env_vars:
        out["env_vars"] = env_vars
    # Other list fields: union, preserve order, dedup
    for field in ("packages", "npm_packages", "setup_commands", "cli"):
        a_items: list[str] = a.get(field) or []  # type: ignore[assignment]
        b_items: list[str] = b.get(field) or []  # type: ignore[assignment]
        merged_list = list(dict.fromkeys(a_items + b_items))
        if merged_list:
            out[field] = merged_list  # type: ignore[literal-required]
    if "doc_url" in b or "doc_url" in a:
        out["doc_url"] = b.get("doc_url") or a.get("doc_url", "")
    if "guidance" in b or "guidance" in a:
        out["guidance"] = b.get("guidance") or a.get("guidance", "")
    return out


def get_requirements(tool_name: str, bundles: list[str]) -> Requirements:
    """Resolve a tool's requirements.

    Combines (in order): every bundle the tool belongs to, then any per-tool
    override. Later sources win on scalar fields; list fields are unioned.
    """
    merged: Requirements = {}
    for b in bundles:
        if b in BUNDLE_REQUIREMENTS:
            merged = _merge(merged, BUNDLE_REQUIREMENTS[b])
    if tool_name in TOOL_OVERRIDES:
        merged = _merge(merged, TOOL_OVERRIDES[tool_name])
    return merged
