import type { CSSProperties, ReactNode } from "react";
import {
  Activity,
  AtSign,
  BookOpen,
  Briefcase,
  CalendarClock,
  Check,
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
  X,
  type LucideIcon,
} from "lucide-react";
import { useT } from "../../core/i18n";
import { CONN_SERVICES } from "./types";
import { STR } from "./strings";

// Inline reproduction of the original dashboard's "Google OAuth Setup" guide.
// The legacy view linked out to a dedicated Guides section; 2.0 has no such
// route yet, so the guide is surfaced as a self-contained modal here.
// Generic connector guides share one builder (connectorGuide) parameterized by
// CONNECTOR_GUIDE_DATA below.

type T = (key: string, vars?: Record<string, string>) => string;

interface GuideStep {
  title: string;
  body: ReactNode;
}
interface GuidePart {
  num: number;
  title: ReactNode;
  steps: GuideStep[];
}
interface Guide {
  id: string;
  eyebrow: ReactNode;
  title: string;
  intro: string;
  badges: string[];
  parts: GuidePart[];
}

const chipStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: "2px 8px",
  marginRight: 6,
  marginTop: 6,
  borderRadius: "var(--r-pill)",
  fontSize: 11,
  fontWeight: 600,
  background: "var(--surface-3)",
  color: "var(--text-2)",
};

function Chips({ items }: { items: string[] }) {
  return (
    <div style={{ marginTop: 4 }}>
      {items.map((c) => (
        <span key={c} style={chipStyle}>
          {c}
        </span>
      ))}
    </div>
  );
}

function code(text: string): ReactNode {
  return (
    <code
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 12,
        background: "var(--code-bg)",
        border: "1px solid var(--code-border)",
        borderRadius: "var(--r-xs)",
        padding: "1px 5px",
        // A redirect URI has no break opportunities; on a 375px phone it
        // would otherwise run past the modal's edge.
        wordBreak: "break-all",
      }}
    >
      {text}
    </code>
  );
}

// Build the Google guide with the active translator. Service/product names
// (Google, Gmail, …) and code/env-var literals stay untranslated.
function googleGuide(t: T): Guide {
  return {
    id: "google-oauth",
    eyebrow: (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
        <Globe size={13} strokeWidth={1.75} />
        Google
      </span>
    ),
    title: t("guide.google.title"),
    intro: t("guide.google.intro"),
    badges: ["Gmail", "Calendar", "Drive", "Docs"],
    parts: [
      {
        num: 1,
        title: t("guide.google.p1.title"),
        steps: [
          {
            title: t("guide.google.p1.s1.title"),
            body: (
              <>
                {t("guide.google.p1.s1.pre")}
                <a href="https://console.cloud.google.com" target="_blank" rel="noopener noreferrer">
                  console.cloud.google.com
                </a>
                {t("guide.google.p1.s1.post")}
              </>
            ),
          },
          {
            title: t("guide.google.p1.s2.title"),
            body: (
              <>
                {t("guide.google.p1.s2.pre")}
                <strong>APIs &amp; Services → Library</strong>
                {t("guide.google.p1.s2.body")}
                <Chips
                  items={[
                    "Gmail API",
                    "Google Calendar API",
                    "Google Drive API",
                    "Google Docs API",
                    "Google Sheets API",
                  ]}
                />
              </>
            ),
          },
          {
            title: t("guide.google.p1.s3.title"),
            body: (
              <>
                {t("guide.google.p1.s3.pre")}
                <strong>APIs &amp; Services → OAuth consent screen</strong>
                {t("guide.google.p1.s3.post")}
                <Chips
                  items={[
                    "gmail.readonly",
                    "gmail.send",
                    "gmail.modify",
                    "calendar",
                    "drive",
                    "documents",
                    "spreadsheets",
                  ]}
                />
              </>
            ),
          },
          {
            title: t("guide.google.p1.s4.title"),
            body: (
              <>
                {t("guide.google.p1.s4.pre")}
                <strong>Credentials → Create Credentials → OAuth client ID</strong>
                {t("guide.google.p1.s4.mid")}
                <div style={{ marginTop: 6 }}>
                  {t("guide.google.p1.s4.redirect")}{" "}
                  {code(`${window.location.origin}/api/auth/google/callback`)}
                </div>
              </>
            ),
          },
        ],
      },
      {
        num: 2,
        title: t("guide.google.p2.title"),
        steps: [
          {
            title: t("guide.google.p2.s1.title"),
            body: (
              <>
                {t("guide.google.p2.s1.pre")}
                <strong>{t("guide.label.vault")}</strong>
                {t("guide.google.p2.s1.mid")}
                <strong>{t("guide.label.addSecret")}</strong>
                {t("guide.google.p2.s1.post")}
                {code("GOOGLE_OAUTH_CLIENT")}
                {t("guide.google.p2.s1.end")}
              </>
            ),
          },
          {
            title: t("guide.google.p2.s2.title"),
            body: (
              <>
                {t("guide.google.p2.s2.body")}
                <pre
                  style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: 12,
                    background: "var(--code-bg)",
                    border: "1px solid var(--code-border)",
                    borderRadius: "var(--r-sm)",
                    padding: "8px 10px",
                    marginTop: 6,
                    overflowX: "auto",
                  }}
                >
                  {`export GOOGLE_OAUTH_CLIENT='{"client_id":"…","client_secret":"…"}'`}
                </pre>
              </>
            ),
          },
        ],
      },
      {
        num: 3,
        title: t("guide.google.p3.title"),
        steps: [
          {
            title: t("guide.google.p3.s1.title"),
            body: (
              <>
                {t("guide.google.p3.s1.pre")}
                <strong>{t("guide.label.system")}</strong>
                {t("guide.google.p3.s1.mid")}
                <strong>{t("guide.label.perAgent")}</strong>
                {t("guide.google.p3.s1.post")}
              </>
            ),
          },
          {
            title: t("guide.google.p3.s2.title"),
            body: (
              <>
                {t("guide.google.p3.s2.pre")}
                <strong>{t("guide.label.connect")}</strong>
                {t("guide.google.p3.s2.mid")}
                <strong>{t("guide.label.fromVault")}</strong>
                {t("guide.google.p3.s2.mid2")}
                <strong>{t("guide.label.startAuth")}</strong>
                {t("guide.google.p3.s2.post")}
              </>
            ),
          },
          {
            title: t("guide.google.p3.s3.title"),
            body: (
              <>
                {t("guide.google.p3.s3.pre")}
                {code("gmail_read")}, {code("gmail_send")}
                {t("guide.google.p3.s3.mid")}
                {code("calendar_*")}
                {t("guide.google.p3.s3.post")}
                {code("drive_*")}
                {t("guide.google.p3.s3.post2")}
                {code("sheets_*")}
                {t("guide.google.p3.s3.end")}
              </>
            ),
          },
        ],
      },
    ],
  };
}

// ── generic connector guides ─────────────────────────────────────────────
// Per-service data slotted into the shared guide.connector.* skeleton. The
// prose fragments below (create / fieldNotes / quirk) are dense with product
// terminology mined from olano/core/connectors/providers/*.py docstrings
// and stay untranslated, like service names and env vars.
interface ConnectorGuideData {
  /** Service id — used for the callback path, vault-key lookup, and svc.<id>.desc. */
  id: string;
  name: string;
  icon: LucideIcon;
  consoleUrl: string;
  consoleLabel: string;
  /** What to create in the developer console (app type, products, where to click). */
  create: string;
  /** Badge row — product areas the connector unlocks. */
  badges: string[];
  /** OAuth scopes requested (empty = provider has no scope system). */
  chips: string[];
  /** Notes for the connect panel's extra fields (tenant, shop, environment…). */
  fieldNotes?: string[];
  /** Token-lifetime / re-auth quirk line. */
  quirk: string;
}

const CONNECTOR_GUIDE_DATA: Record<string, ConnectorGuideData> = {
  "microsoft365-oauth": {
    id: "microsoft365",
    name: "Microsoft 365",
    icon: Mail,
    consoleUrl: "https://portal.azure.com",
    consoleLabel: "portal.azure.com",
    create:
      "Create an app registration (Entra ID → App registrations) with a Web platform redirect URI and a client secret, then grant the delegated Graph permissions below.",
    badges: ["Outlook Mail", "Calendar", "Contacts", "To Do", "OneDrive", "SharePoint", "Excel", "Teams"],
    chips: [
      "offline_access",
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
    ],
    fieldNotes: [
      "Tenant: leave empty for common (multi-tenant + personal accounts); orgs with conditional-access policies can enter their tenant GUID instead.",
    ],
    quirk:
      "Access tokens last ~60-90 minutes; offline_access grants a rolling refresh token, so the connection renews itself automatically. A connection made before a permission was added keeps working for what it had; sign in again (Connect) to grant the new ones.",
  },
  "slack-oauth": {
    id: "slack",
    name: "Slack",
    icon: Hash,
    consoleUrl: "https://api.slack.com/apps",
    consoleLabel: "api.slack.com/apps",
    create:
      "Create an app (From scratch) in your workspace. Under OAuth & Permissions add the bot token scopes below plus the user token scope search:read, register the redirect URL below, then copy the Client ID + Client Secret from Basic Information.",
    badges: ["Messages", "Channels", "Threads", "Search"],
    chips: ["chat:write", "channels:read", "channels:history", "groups:read", "im:read", "users:read", "search:read (user)"],
    quirk:
      "One consent grants two tokens: the bot token every tool posts and reads with, and a user token only search needs (search is a user-scope method a bot cannot call). Bot tokens never expire unless you enable token rotation on the app, in which case they refresh automatically. This is separate from a Slack channel (an agent listening in a workspace), which keeps its own bot token in the channel config.",
  },
  "notion-oauth": {
    id: "notion",
    name: "Notion",
    icon: NotebookText,
    consoleUrl: "https://www.notion.so/my-integrations",
    consoleLabel: "notion.so/my-integrations",
    create:
      "Create an integration and switch it to Public (an Internal integration only issues a pasted token — that one goes in the vault as NOTION_API_KEY instead). Register the redirect URI below, then copy the OAuth client ID + OAuth client secret. Notion has no scope list: the person picks which pages the integration may see on the consent page itself.",
    badges: ["Pages", "Databases", "Search"],
    chips: [],
    quirk:
      "Access tokens never expire and there is no refresh grant, so the connection stays up until you disconnect here or revoke it in Notion. The token is exposed as NOTION_API_KEY (and NOTION_TOKEN, its legacy name), so a pasted key and an OAuth connection are interchangeable to every tool.",
  },
  "salesforce-oauth": {
    id: "salesforce",
    name: "Salesforce",
    icon: Briefcase,
    consoleUrl: "https://login.salesforce.com",
    consoleLabel: "login.salesforce.com",
    create:
      "Log in to your org and create a Connected App (Setup → App Manager → New Connected App): enable OAuth, add the callback URL below, and select the api + refresh_token scopes.",
    badges: ["CRM", "SOQL", "Opportunities"],
    chips: ["api", "refresh_token"],
    fieldNotes: ["Domain: leave empty for login (production); enter test for sandboxes."],
    quirk:
      "Access tokens follow your org's session policy (~2h) and refresh automatically; the org's instance URL is captured for you at connect time.",
  },
  "hubspot-oauth": {
    id: "hubspot",
    name: "HubSpot",
    icon: Target,
    consoleUrl: "https://developers.hubspot.com",
    consoleLabel: "developers.hubspot.com",
    create:
      "In a HubSpot developer account, create an app that other HubSpot accounts can install, add the redirect URL below, and copy the Client ID + Client Secret from its Auth settings. Configure its scopes to match the list below exactly: the plain ones as required, the ones marked (optional) as optional. HubSpot refuses the sign-in when the app's scopes and the ones Olano asks for differ, in either direction.",
    badges: ["Contacts", "Companies", "Deals", "Tickets", "Activities", "Lists", "Workflows", "CMS"],
    chips: [
      "oauth",
      "crm.objects.contacts.read",
      "crm.objects.contacts.write",
      "crm.objects.companies.read",
      "crm.objects.companies.write",
      "crm.objects.deals.read",
      "crm.objects.deals.write",
      "crm.objects.owners.read",
      "tickets (optional)",
      "e-commerce (optional)",
      "crm.objects.line_items.read (optional)",
      "crm.objects.line_items.write (optional)",
      "crm.objects.quotes.read (optional)",
      "crm.objects.quotes.write (optional)",
      "crm.objects.invoices.read (optional)",
      "crm.objects.custom.read (optional)",
      "crm.objects.custom.write (optional)",
      "crm.schemas.contacts.read (optional)",
      "crm.schemas.contacts.write (optional)",
      "crm.schemas.companies.read (optional)",
      "crm.schemas.companies.write (optional)",
      "crm.schemas.deals.read (optional)",
      "crm.schemas.deals.write (optional)",
      "crm.schemas.custom.read (optional)",
      "crm.lists.read (optional)",
      "crm.lists.write (optional)",
      "crm.import (optional)",
      "crm.export (optional)",
      "sales-email-read (optional)",
      "timeline (optional)",
      "forms (optional)",
      "automation (optional)",
      "content (optional)",
      "hubdb (optional)",
      "files (optional)",
      "marketing-email (optional)",
      "transactional-email (optional)",
    ],
    quirk:
      "Access tokens last 30 minutes and refresh automatically; the refresh token does not expire. Optional scopes are granted only when the HubSpot plan and the person signing in have them, so a tool for a feature the account lacks reports a missing scope rather than failing the sign-in. The token is exposed as HUBSPOT_ACCESS_TOKEN, so a private-app token pasted into the vault under that name works the same way.",
  },
  "quickbooks-oauth": {
    id: "quickbooks",
    name: "QuickBooks",
    icon: CreditCard,
    consoleUrl: "https://developer.intuit.com",
    consoleLabel: "developer.intuit.com",
    create:
      "Create an app with the QuickBooks Online Accounting scope and add the redirect URI below. Development keys target sandbox companies; production keys target live ones.",
    badges: ["Invoices", "Customers", "Reports"],
    chips: ["com.intuit.quickbooks.accounting"],
    fieldNotes: ["Environment: leave empty for production; enter sandbox when using development keys."],
    quirk:
      "Your company id (realmId) is captured automatically from the callback. Access tokens last ~1 hour; refresh tokens last ~100 days and are rotated + persisted automatically.",
  },
  "x_twitter-oauth": {
    id: "x_twitter",
    name: "X (Twitter)",
    icon: AtSign,
    consoleUrl: "https://developer.x.com",
    consoleLabel: "developer.x.com",
    create:
      "Create a project + app, enable OAuth 2.0 under User authentication settings (type Web App), add the callback URI below, and copy the OAuth 2.0 Client ID + Client Secret.",
    badges: ["Posts", "Search", "Timelines"],
    chips: ["tweet.read", "tweet.write", "users.read", "like.write", "offline.access"],
    quirk:
      "Access tokens last 2 hours and refresh automatically. The free API tier still limits which endpoints a token may call — that is a plan restriction, not an auth failure.",
  },
  "meta-oauth": {
    id: "meta",
    name: "Meta",
    icon: BookOpen,
    consoleUrl: "https://developers.facebook.com",
    consoleLabel: "developers.facebook.com",
    create:
      "Create an app of type Business, add the Facebook Login for Business product, and register the redirect URI below. The Client ID / Secret are the app's App ID / App Secret. Publishing permissions (pages_manage_posts, instagram_content_publish) require App Review before non-testers can grant them.",
    badges: ["Facebook Pages", "Instagram", "Insights"],
    chips: ["pages_manage_posts", "pages_read_engagement", "read_insights", "instagram_basic", "instagram_content_publish"],
    quirk:
      "The login token is upgraded to a ~60-day token automatically (a selected Page token never expires). There is no refresh grant — reconnect from this tab when it lapses.",
  },
  "linkedin-oauth": {
    id: "linkedin",
    name: "LinkedIn",
    icon: Share2,
    consoleUrl: "https://developer.linkedin.com",
    consoleLabel: "developer.linkedin.com",
    create:
      "Create an app, add the products Sign In with LinkedIn using OpenID Connect and Share on LinkedIn, and register the redirect URL below.",
    badges: ["Posts", "Profile"],
    chips: ["openid", "profile", "email", "w_member_social"],
    quirk:
      "Access tokens last ~60 days and programmatic refresh is partner-gated — reconnect from this tab when the token lapses.",
  },
  "reddit-oauth": {
    id: "reddit",
    name: "Reddit",
    icon: MessageCircle,
    consoleUrl: "https://www.reddit.com/prefs/apps",
    consoleLabel: "reddit.com/prefs/apps",
    create:
      "Create an app of type web app and set the redirect URI below. The client id is the short string under the app name; the secret is labeled secret.",
    badges: ["Search", "Subreddits", "Submit"],
    chips: ["identity", "read", "submit"],
    quirk:
      "Authorization is requested with duration=permanent, so a long-lived refresh token keeps the connection alive while hourly access tokens rotate on their own.",
  },
  "fitbit-oauth": {
    id: "fitbit",
    name: "Fitbit",
    icon: Activity,
    consoleUrl: "https://dev.fitbit.com/apps",
    consoleLabel: "dev.fitbit.com/apps",
    create:
      "Register an app (application type Personal reads your own intraday data), set the redirect URL below, and copy the OAuth 2.0 Client ID + Client Secret.",
    badges: ["Activity", "Sleep", "Heart rate"],
    chips: ["activity", "heartrate", "sleep", "weight"],
    quirk:
      "Access tokens last 8 hours; the single-use refresh token is rotated and persisted automatically on every refresh.",
  },
  "buffer-oauth": {
    id: "buffer",
    name: "Buffer",
    icon: CalendarClock,
    consoleUrl: "https://buffer.com/developers/apps",
    consoleLabel: "buffer.com/developers/apps",
    create:
      "Buffer closed new developer-app registration, so a Client ID / Secret only exists for grandfathered apps — register the redirect URI below on yours. No app? Create a personal access token instead (Buffer → Settings → Apps) and store it in the vault as BUFFER_ACCESS_TOKEN; the toolkit reads either source.",
    badges: ["Scheduling", "Queues"],
    chips: [],
    quirk:
      "The legacy v1 API has no scopes (authorization grants full account access) and issues a non-expiring token — no refresh needed.",
  },
  "shopify-oauth": {
    id: "shopify",
    name: "Shopify",
    icon: ShoppingBag,
    consoleUrl: "https://partners.shopify.com",
    consoleLabel: "partners.shopify.com",
    create:
      "Create an app in the Partner dashboard — or, for a single store, in the shop admin under Apps → Develop apps. Set the allowed redirection URL below and copy the API key (client id) + API secret.",
    badges: ["Products", "Orders", "Customers"],
    chips: ["read_products", "write_products", "read_orders", "read_customers"],
    fieldNotes: ["Shop: the store's myshopify.com subdomain — mystore or mystore.myshopify.com both work."],
    quirk: "The offline access token is permanent until the app is uninstalled — no refresh and no reconnects needed.",
  },
  "github-oauth": {
    id: "github",
    name: "GitHub",
    icon: GitPullRequest,
    consoleUrl: "https://github.com/settings/developers",
    consoleLabel: "github.com/settings/developers",
    create:
      "Create an OAuth App (Developer settings → OAuth Apps → New OAuth App) with the callback URL below, then generate a client secret. A GitHub App works too and additionally rotates its user tokens automatically. No app at all? Close this guide and press Sign in with a code on the card instead — a one-time code typed on GitHub's device page, with nothing to register.",
    badges: ["Repos", "Issues", "Pull requests", "GitHub MCP"],
    chips: ["repo", "workflow", "read:org", "read:user"],
    quirk:
      "Classic OAuth App tokens never expire — no reconnects needed. GitHub App user tokens last 8 hours and refresh automatically. The token is also exposed as GITHUB_PERSONAL_ACCESS_TOKEN for the official GitHub MCP server; prefer the remote GitHub MCP preset for MCP-only use.",
  },
  "docusign-oauth": {
    id: "docusign",
    name: "DocuSign",
    icon: FileSignature,
    consoleUrl: "https://developers.docusign.com",
    consoleLabel: "developers.docusign.com",
    create:
      "Create an app under Apps & Keys, add the redirect URI below, and copy the Integration Key (client id) plus a Secret Key.",
    badges: ["Envelopes", "Signatures"],
    chips: ["signature"],
    fieldNotes: ["Environment: leave empty for account (production); enter account-d for demo/sandbox accounts."],
    quirk:
      "Your default account id and API base URL are discovered automatically after connect. Access tokens last ~8 hours and refresh automatically.",
  },
};

// Build a generic connector guide from the shared guide.connector.* skeleton
// plus one service's CONNECTOR_GUIDE_DATA entry.
function connectorGuide(t: T, data: ConnectorGuideData): Guide {
  const Icon = data.icon;
  const vars = { name: data.name };
  const callback = `${window.location.origin}/api/auth/${data.id}/callback`;
  const vaultKey = CONN_SERVICES.find((s) => s.id === data.id)?.credsEnv;
  return {
    id: `${data.id}-oauth`,
    eyebrow: (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
        <Icon size={13} strokeWidth={1.75} />
        {data.name}
      </span>
    ),
    title: t("guide.connector.title", vars),
    intro: `${t("guide.connector.intro", vars)} ${t(`svc.${data.id}.desc`)}`,
    badges: data.badges,
    parts: [
      {
        num: 1,
        title: t("guide.connector.p1.title"),
        steps: [
          {
            title: t("guide.connector.p1.s1.title"),
            body: (
              <>
                {t("guide.connector.p1.s1.pre")}
                <a href={data.consoleUrl} target="_blank" rel="noopener noreferrer">
                  {data.consoleLabel}
                </a>
                {t("guide.connector.p1.s1.post")}
                {data.create}
                {data.chips.length > 0 && (
                  <div style={{ marginTop: 6 }}>
                    {t("guide.connector.p1.s1.scopes")}
                    <Chips items={data.chips} />
                  </div>
                )}
              </>
            ),
          },
          {
            title: t("guide.connector.p1.s2.title"),
            body: (
              <>
                {t("guide.connector.p1.s2.body")}
                <div style={{ marginTop: 6 }}>{code(callback)}</div>
              </>
            ),
          },
        ],
      },
      {
        num: 2,
        title: t("guide.connector.p2.title"),
        steps: [
          {
            title: t("guide.connector.p2.s1.title"),
            body: (
              <>
                {t("guide.connector.p2.s1.pre", vars)}
                <strong>{t("guide.label.connect")}</strong>
                {t("guide.connector.p2.s1.mid", vars)}
                {data.fieldNotes?.map((note) => (
                  <div key={note} style={{ marginTop: 4 }}>
                    {note}
                  </div>
                ))}
                {vaultKey && (
                  <div style={{ marginTop: 6 }}>
                    {t("guide.connector.p2.s1.vaultPre")}
                    {code(vaultKey)}
                    {t("guide.connector.p2.s1.vaultMid")}
                    <strong>{t("guide.label.fromVault")}</strong>
                    {t("guide.connector.p2.s1.vaultPost")}
                  </div>
                )}
              </>
            ),
          },
          {
            title: t("guide.connector.p2.s2.title"),
            body: (
              <>
                {t("guide.connector.p2.s2.pre")}
                <strong>{t("guide.label.startAuth")}</strong>
                {t("guide.connector.p2.s2.post")}
                <div style={{ marginTop: 4 }}>{data.quirk}</div>
              </>
            ),
          },
        ],
      },
    ],
  };
}

export function SetupGuideModal({ guideId, onClose }: { guideId: string; onClose: () => void }) {
  const t = useT(STR);
  const data = CONNECTOR_GUIDE_DATA[guideId];
  const guide = guideId === "google-oauth" ? googleGuide(t) : data ? connectorGuide(t, data) : null;
  if (!guide) return null;

  return (
    <div className="modal-overlay" onClick={onClose} style={{ zIndex: 60 }}>
      <div
        className="card modal-card"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(680px, 100%)",
          overflowY: "auto",
          overscrollBehavior: "contain",
          padding: 24,
        }}
      >
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <div className="muted" style={{ fontSize: 12, fontWeight: 600 }}>
              {guide.eyebrow}
            </div>
            <h1 style={{ fontSize: 19, margin: "4px 0 6px" }}>{guide.title}</h1>
          </div>
          <button className="btn" onClick={onClose} aria-label={t("guide.close")}>
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          {guide.intro}
        </p>
        <div className="row" style={{ flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
          {guide.badges.map((b) => (
            <span key={b} className="badge admin" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <Check size={12} strokeWidth={2} />
              {b}
            </span>
          ))}
        </div>

        {guide.parts.map((part) => (
          <div key={part.num} style={{ marginTop: 18 }}>
            <div className="row" style={{ gap: 8, marginBottom: 10 }}>
              <span
                style={{
                  display: "inline-grid",
                  placeItems: "center",
                  width: 22,
                  height: 22,
                  borderRadius: "var(--r-pill)",
                  background: "var(--accent-gradient)",
                  color: "var(--accent-fg)",
                  fontSize: 12,
                  fontWeight: 700,
                  flexShrink: 0,
                }}
              >
                {part.num}
              </span>
              <strong style={{ fontSize: 14 }}>{part.title}</strong>
            </div>
            <ol style={{ margin: 0, paddingLeft: 0, listStyle: "none" }}>
              {part.steps.map((step, i) => (
                <li key={i} style={{ display: "flex", gap: 10, paddingBottom: 12 }}>
                  <span
                    style={{
                      display: "inline-grid",
                      placeItems: "center",
                      width: 20,
                      height: 20,
                      borderRadius: "var(--r-pill)",
                      border: "1px solid var(--border-strong)",
                      color: "var(--text-3)",
                      fontSize: 11,
                      fontWeight: 700,
                      flexShrink: 0,
                    }}
                  >
                    {i + 1}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13.5, fontWeight: 600 }}>{step.title}</div>
                    <div className="muted" style={{ fontSize: 13, marginTop: 2, lineHeight: 1.5 }}>
                      {step.body}
                    </div>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        ))}

        <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
          <button className="btn primary" onClick={onClose}>
            {t("guide.gotIt")}
          </button>
        </div>
      </div>
    </div>
  );
}
