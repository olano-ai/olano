import {
  Activity,
  AtSign,
  BookOpen,
  Briefcase,
  CalendarClock,
  CreditCard,
  FileSignature,
  GitPullRequest,
  Globe,
  Hash,
  Mail,
  MessageCircle,
  NotebookText,
  Share2,
  ShoppingBag,
  Target,
  type LucideIcon,
} from "lucide-react";

// ── view-specific types for the Connections view ───────────────────────

/** One credential input rendered in a generic connector's connect panel. */
export interface ConnectionField {
  /** Key inside the POST /api/auth/{service}/start `creds` body. */
  key: string;
  /** strings.ts key for the input label (e.g. "creds.clientId"). */
  labelKey: string;
  /** Render as a password input (client secrets). */
  secret?: boolean;
  /** Untranslated placeholder — defaults / example values. */
  placeholder?: string;
  /** Marked with * in the form; the backend validates presence. */
  required?: boolean;
}

export interface ConnectionService {
  id: string;
  name: string;
  /** Flat lucide icon component representing the service. */
  icon: LucideIcon;
  caps: string[];
  /** Vault key that holds the OAuth client as a JSON blob ({"client_id", "client_secret"}). */
  credsEnv?: string;
  /** Identifier of the inline setup guide to surface, if any. */
  guideId?: string;
  /**
   * Credential form fields rendered in the connect panel. Every service —
   * google included — sends the filled fields as the {creds: {...}} start
   * body; empty fields fall through to the vault blob / env pair on the
   * runtime (google additionally falls back to the legacy
   * GOOGLE_OAUTH_CREDENTIALS client_secret.json path).
   */
  fields?: ConnectionField[];
}

/**
 * How a service can be signed in. Comes from the runtime per service
 * (`AuthStatus.methods`) — never hardcoded here — so a card offers exactly
 * the buttons its provider declares: "oauth" = the Connect panel (official
 * or bring-your-own OAuth app), "device" = the sign-in-with-a-code panel
 * (a one-time code typed on the provider's site; no app to register),
 * "key" = a pasted key in the vault under one of the names the connector
 * exposes (handled by the Vault tab / magic links, not by a card button).
 */
export type AuthMethod = "oauth" | "device" | "key";

// Translator type matching the `t` returned by useT(STR).
type T = (key: string, vars?: Record<string, string>) => string;

/** Mirrors `google_auth_status()` / `connector_auth_status()` on the runtime. */
export interface AuthStatus {
  connected: boolean;
  scope: "system" | "agent" | "shared" | "none";
  token_path: string | null;
  shared: boolean | null;
  /** Unix timestamp; null/absent = non-expiring or unknown. */
  expires_at?: number | null;
  /**
   * Sign-in methods the provider declares (see AuthMethod). Absent on the
   * legacy google payload from an older runtime — treat as ["oauth"].
   */
  methods?: AuthMethod[];
  /**
   * The signed-in login (e.g. a GitHub username) when the provider looks it
   * up after a sign-in — today that is the device-code method. Absent when
   * disconnected or when the provider has no account lookup.
   */
  account?: string | null;
  /**
   * Generic connectors: an official Olano OAuth app is available for this
   * service (connectors catalog + auth relay), so the connect panel can offer
   * one-click connect with no client fields. Absent on legacy runtimes and
   * on google (BYO-only).
   */
  official_available?: boolean;
  /**
   * Where the OAuth client came from: "official" = Olano's app (relay
   * refresh), "byo" = the user's own OAuth client, "public" = the provider's
   * public client used by the device-code method. Absent when disconnected
   * or on legacy runtimes (treat as "byo").
   */
  client_source?: "official" | "byo" | "public";
  /**
   * How the stored token was obtained: "direct" = the on-box OAuth callback,
   * "relay" = the official-app flow through the central callback, "device" =
   * a one-time code. Absent when disconnected or on legacy runtimes.
   */
  auth_mode?: "direct" | "relay" | "device";
  /**
   * The service refused the stored token and a refresh could not cure it (a
   * toolkit reported the 401). `connected` is false, the record is still on
   * disk, and a new sign-in replaces it. `auth_error` is the refusal, in the
   * service's own words, to print under the badge.
   */
  needs_reauth?: boolean;
  auth_error?: string | null;
  auth_failed_at?: number | null;
}

/** GET /api/auth/status — keyed by service id for all services. */
export type AuthStatusResponse = Record<string, AuthStatus | undefined>;

/** GET /api/vault/secrets/{name}/reveal */
export interface VaultSecret {
  name: string;
  value: string | null;
}

/**
 * POST /api/auth/{service}/start and GET /api/auth/{service}/poll/{id}.
 * Mirrors GoogleAuthSession.public() / OAuthSession.public() on the runtime.
 */
export interface OAuthSessionInfo {
  session_id: string;
  status: "awaiting_authorization" | "exchanging" | "authorized" | "error";
  authorization_url: string | null;
  redirect_uri: string | null;
  error: string | null;
  agent: string | null;
  shared: boolean;
  /** Present on generic connector sessions; absent on legacy google payloads. */
  service?: string;
  logs?: { ts?: number; level?: string; message: string }[];
  /**
   * "relay" = official-app flow (central callback + exchange; no redirect URI
   * to register); "direct"/absent = the on-box callback flow.
   */
  auth_mode?: "direct" | "relay";
}

/** Back-compat alias for the pre-connector-broker name. */
export type GoogleOAuthSession = OAuthSessionInfo;

/**
 * POST /api/auth/{service}/device/start (body {agent?, shared?}),
 * GET /api/auth/{service}/device/poll/{session_id} and
 * POST /api/auth/{service}/device/cancel/{session_id} — the generic
 * device-code (RFC 8628) sign-in for any service whose AuthStatus.methods
 * includes "device". Mirrors `connectors/device.public_session()` on the
 * runtime, which itself mirrors the OpenAI Codex device-flow shape. A
 * service without a device recipe answers 400 on all three routes.
 */
export interface DeviceSessionInfo {
  session_id: string;
  service: string;
  status: "starting" | "pending" | "approved" | "error" | "expired";
  user_code: string | null;
  verification_url: string | null;
  /** Seconds between dashboard polls of the poll route. */
  interval?: number;
  expires_at?: number | null;
  error_message: string | null;
  /** The signed-in login, once approved (when the provider looks it up). */
  account?: string | null;
  /** Scope the token lands at: the agent's own record, or system when null. */
  agent?: string | null;
  shared?: boolean;
  logs?: { ts?: number; level?: string; message: string }[];
}

export type Scope = "system" | "agent";

// ── magic links (olano/magic_links) ─────────────────────────────────
// Short-lived, PIN-protected pages the instance mints so a chat-only user can
// finish a sign-in on their own phone. Admin-gated server-side.

/** One entry of GET /api/magic-links/flows. */
export interface MagicLinkFlowInfo {
  id: string;
  label: string;
  /** Page kind — device_code | redirect | form | qr | connect | mcp. */
  kind: string;
  instructions: string;
  /** channel-connect only: the live connector registry for the platform picker. */
  platforms?: MagicLinkConnectPlatform[];
}

/** One messenger the channel-connect page can offer. */
export interface MagicLinkConnectPlatform {
  platform: string;
  label: string;
  /** "token" (credential form) or "qr" (pairing). */
  mode: string;
  channel_type: string;
}

/** Who asked for a link (chat channel + username, or the dashboard). */
export interface MagicLinkRequester {
  channel?: string;
  username?: string;
  agent?: string | null;
}

export type MagicLinkStatus = "pending" | "in_progress" | "done" | "error" | "expired" | "revoked";

/**
 * One row of GET /api/magic-links/links — `LinkRecord.public()` on the runtime.
 * Deliberately carries NO token, PIN, or secret value, so nothing here is
 * sensitive enough to hide.
 */
export interface MagicLink {
  id: string;
  flow: string;
  service: string;
  agent: string | null;
  status: MagicLinkStatus;
  result: string;
  error: string;
  requested_by: MagicLinkRequester;
  created_at: string;
  expires_at: string;
  expired: boolean;
  pin_required: boolean;
}

/** GET /api/magic-links/links — `available` folds in the feature switch and
 *  whether this deployment has a browser-reachable public origin at all. */
export interface MagicLinksResponse {
  available: boolean;
  reason: string;
  links: MagicLink[];
}

/**
 * POST /api/magic-links/links. The **only** time `url` and `pin` are ever
 * returned — they are hashed on the runtime and cannot be fetched again.
 */
export interface MintedMagicLink {
  id: string;
  url: string;
  /** "" when the deployment's PIN policy is off. */
  pin: string;
  flow: string;
  label: string;
  expires_at: string;
  ttl_minutes: number;
}

/**
 * One row of GET /api/magic-links/bindings — a chat identity linked to a
 * dashboard account (the owner-login flow persists these). Admin-gated and
 * secret-free: no token, session, or password ever appears here.
 */
export interface MagicLinkBinding {
  /** Channel-scoped identity, e.g. "telegram:12345678". Contains a colon. */
  key: string;
  user_id: string;
  email: string;
  bound_at: string;
  /** How the link was established. password | auto | bootstrap | binding. */
  bound_via: string;
}

/** GET /api/magic-links/bindings. */
export interface MagicLinkBindingsResponse {
  bindings: MagicLinkBinding[];
}

/** The dashboard sections an owner-login link can open on. `prompt` needs an
 *  agent; the rest are self-contained. Order matches the modal dropdown. */
export const OWNER_LOGIN_SECTIONS = [
  "overview",
  "prompt",
  "vault",
  "connections",
  "providers",
  "config",
  "files",
] as const;

// Every generic connector needs an OAuth client id + secret; extra
// per-service params (tenant, shop, …) are appended per entry below.
const CLIENT_FIELDS: ConnectionField[] = [
  { key: "client_id", labelKey: "creds.clientId", required: true },
  { key: "client_secret", labelKey: "creds.clientSecret", secret: true, required: true },
];

export const CONN_SERVICES: ConnectionService[] = [
  {
    id: "google",
    name: "Google",
    icon: Globe,
    caps: ["gmail_read", "gmail_send", "calendar", "drive", "docs", "sheets"],
    // Client ID/Secret like every other connector. The runtime still honors
    // the legacy GOOGLE_OAUTH_CREDENTIALS client_secret.json *path* (vault or
    // env) as a fallback when the fields are left empty.
    credsEnv: "GOOGLE_OAUTH_CLIENT",
    guideId: "google-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "microsoft365",
    name: "Microsoft 365",
    icon: Mail,
    caps: [
      "ms365_send_mail",
      "ms365_create_draft",
      "ms365_create_event",
      "ms365_find_meeting_times",
      "ms365_search_files",
      "ms365_send_chat_message",
    ],
    credsEnv: "MS365_OAUTH_CLIENT",
    guideId: "microsoft365-oauth",
    fields: [...CLIENT_FIELDS, { key: "tenant", labelKey: "creds.tenant", placeholder: "common" }],
  },
  {
    id: "slack",
    name: "Slack",
    icon: Hash,
    // tools/slack_tools.py — the bot token (SLACK_BOT_TOKEN) drives all of
    // these; slack_search prefers the user token (SLACK_USER_TOKEN) the same
    // consent grants, since search.messages is a user-scope method.
    caps: ["slack_send_message", "slack_list_channels", "slack_get_messages", "slack_get_thread", "slack_search"],
    credsEnv: "SLACK_OAUTH_CLIENT",
    guideId: "slack-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "notion",
    name: "Notion",
    icon: NotebookText,
    // tools/notion_tools.py — reads NOTION_API_KEY (NOTION_TOKEN is its
    // legacy name; the connector exposes both).
    caps: ["notion_search", "notion_get_page", "notion_create_page", "notion_append_to_page", "notion_query_database"],
    credsEnv: "NOTION_OAUTH_CLIENT",
    guideId: "notion-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "salesforce",
    name: "Salesforce",
    icon: Briefcase,
    caps: ["sf_query", "sf_create_record", "sf_update_record", "sf_list_opportunities", "sf_pipeline_summary"],
    credsEnv: "SALESFORCE_OAUTH_CLIENT",
    guideId: "salesforce-oauth",
    fields: [
      ...CLIENT_FIELDS,
      { key: "domain", labelKey: "creds.domain", placeholder: "login (test for sandboxes)" },
    ],
  },
  {
    id: "hubspot",
    name: "HubSpot",
    icon: Target,
    // tools/hubspot_tools.py reads HUBSPOT_ACCESS_TOKEN, which the connector
    // exposes; a pasted private-app token under the same name also works.
    caps: [
      "hubspot_search_contacts",
      "hubspot_create_contact",
      "hubspot_create_deal",
      "hubspot_log_activity",
      "hubspot_get_pipeline",
    ],
    credsEnv: "HUBSPOT_OAUTH_CLIENT",
    guideId: "hubspot-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "quickbooks",
    name: "QuickBooks",
    icon: CreditCard,
    caps: [
      "quickbooks_create_invoice",
      "quickbooks_list_invoices",
      "quickbooks_list_customers",
      "quickbooks_profit_and_loss",
      "quickbooks_balance_sheet",
    ],
    credsEnv: "QUICKBOOKS_OAUTH_CLIENT",
    guideId: "quickbooks-oauth",
    fields: [
      ...CLIENT_FIELDS,
      { key: "environment", labelKey: "creds.environment", placeholder: "production" },
    ],
  },
  {
    id: "x_twitter",
    name: "X (Twitter)",
    icon: AtSign,
    caps: ["x_post_tweet", "x_search", "x_timeline", "x_get_user", "x_like_tweet"],
    credsEnv: "X_OAUTH_CLIENT",
    guideId: "x_twitter-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "meta",
    name: "Meta",
    icon: BookOpen,
    caps: [
      "facebook_publish_post",
      "facebook_get_page_insights",
      "instagram_publish_image",
      "instagram_publish_reel",
      "instagram_get_comments",
    ],
    credsEnv: "META_OAUTH_CLIENT",
    guideId: "meta-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "linkedin",
    name: "LinkedIn",
    icon: Share2,
    caps: ["linkedin_post", "linkedin_get_profile", "linkedin_get_analytics"],
    credsEnv: "LINKEDIN_OAUTH_CLIENT",
    guideId: "linkedin-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "reddit",
    name: "Reddit",
    icon: MessageCircle,
    caps: ["reddit_search", "reddit_get_subreddit_posts", "reddit_get_post_comments", "reddit_post_submit"],
    credsEnv: "REDDIT_OAUTH_CLIENT",
    guideId: "reddit-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "fitbit",
    name: "Fitbit",
    icon: Activity,
    caps: ["fitbit_get_activity", "fitbit_get_sleep", "fitbit_get_heart_rate", "fitbit_get_weight"],
    credsEnv: "FITBIT_OAUTH_CLIENT",
    guideId: "fitbit-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "buffer",
    name: "Buffer",
    icon: CalendarClock,
    caps: ["buffer_create_post", "buffer_list_channels", "buffer_list_scheduled"],
    credsEnv: "BUFFER_OAUTH_CLIENT",
    guideId: "buffer-oauth",
    fields: [...CLIENT_FIELDS],
  },
  {
    id: "shopify",
    name: "Shopify",
    icon: ShoppingBag,
    caps: [
      "shopify_list_products",
      "shopify_create_product",
      "shopify_list_orders",
      "shopify_get_analytics",
      "shopify_search_customers",
    ],
    credsEnv: "SHOPIFY_OAUTH_CLIENT",
    guideId: "shopify-oauth",
    fields: [
      ...CLIENT_FIELDS,
      {
        key: "shop",
        labelKey: "creds.shop",
        placeholder: "mystore or mystore.myshopify.com",
        required: true,
      },
    ],
  },
  {
    id: "docusign",
    name: "DocuSign",
    icon: FileSignature,
    caps: [
      "docusign_create_envelope",
      "docusign_send_envelope",
      "docusign_list_envelopes",
      "docusign_get_envelope",
      "docusign_void_envelope",
    ],
    credsEnv: "DOCUSIGN_OAUTH_CLIENT",
    guideId: "docusign-oauth",
    fields: [
      ...CLIENT_FIELDS,
      { key: "environment", labelKey: "creds.environment", placeholder: "account (account-d for demo)" },
    ],
  },
  {
    // One connector, two sign-in methods: the OAuth app (Connect) and a
    // one-time code (Sign in with a code) — the runtime's `methods` list
    // decides which buttons the card shows. Either way the token lands at
    // the chosen scope and is exposed as GITHUB_TOKEN / GITHUB_PERSONAL_ACCESS_TOKEN.
    id: "github",
    name: "GitHub",
    icon: GitPullRequest,
    caps: [
      "github_list_repos",
      "github_create_issue",
      "github_create_pr",
      "github_search_code",
      "github_merge_pr",
    ],
    credsEnv: "GITHUB_OAUTH_CLIENT",
    guideId: "github-oauth",
    fields: [...CLIENT_FIELDS],
  },
];

export function scopeLabel(status: AuthStatus, t: T): string {
  switch (status.scope) {
    case "system":
      return t("scopeLabel.system");
    case "agent":
      return t("scopeLabel.agent");
    case "shared":
      return t("scopeLabel.shared");
    default:
      return "";
  }
}
