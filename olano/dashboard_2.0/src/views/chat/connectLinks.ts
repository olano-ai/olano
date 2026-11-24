// Structured "connect" links mined from tool results, so the chat renders a
// proper Connect button card instead of the raw OAuth URL the model would
// otherwise paste as a wall of unclickable text.
//
// Two result shapes, both JSON produced by backend tools:
//   - MCP OAuth (`self_authorize_mcp_server` / `olano_authorize_mcp_server`,
//     and their status polls): `{authorization_url, url, status, …}` where
//     `url` is the MCP endpoint the sign-in belongs to.
//   - Magic links (`magic_link_*` tools): `{url, pin, flow, label, …}` — the
//     deployment's secure sign-in page.
//
// Detection is keyed on tool names + field names — never on prose — so it
// works identically for live streams and reloaded threads. Persisted
// transcripts truncate tool results (~1200 chars, after which the JSON no
// longer parses), so extraction falls back to per-field regexes; every field
// we need appears well before the cut.

// From turnText, where it is defined (sessionStore re-exports it): this module
// is shared with the mobile app, which must not pull the chat store.
import type { ToolCall } from "./turnText";

export interface ConnectLink {
  /** The sign-in URL the button opens. Also the dedupe key. */
  href: string;
  /** Decides the card copy: an MCP server OAuth vs a minted magic link. */
  kind: "mcp-oauth" | "magic-link";
  /** MCP endpoint URL the sign-in belongs to (kind "mcp-oauth"). */
  serverUrl?: string;
  /** Human label from the payload (magic links carry one, e.g. "Connect Google"). */
  label?: string;
  /** One-time PIN the user types on the magic-link page, when the policy issues one. */
  pin?: string;
  /** The link record's id (kind "magic-link"): what the inline form routes key on. */
  id?: string;
  /** The magic-link flow (kind "magic-link"), e.g. "secret-entry". */
  flow?: string;
}

/**
 * Magic-link flows the chat renders as a form card under the reply instead of
 * a link to open. Mirrors `service.INLINE_FORM_FLOWS` on the backend, which is
 * what `GET /api/magic-links/links/{id}/form` serves; a flow listed here and
 * not there would get a 400 and fall back to the link card.
 */
export const INLINE_FORM_FLOWS: ReadonlySet<string> = new Set(["secret-entry", "provider-key"]);

/** `https://host[:port]` of a URL, or "" when it does not parse. */
export function originOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}

/**
 * Whether a link is rendered inline, as a secret form, rather than as a link.
 *
 * `apiOrigin` is the origin this client talks to (the dashboard's own origin,
 * the deployment the app is signed in to). The link must point at it: a
 * result is detected by tool-name prefix, and an MCP tool could be named
 * `magic_link_*`, so a link to any other origin keeps the external button and
 * never puts a credential field in the chat for it.
 */
export function inlineFormLink(link: ConnectLink, apiOrigin: string): boolean {
  if (link.kind !== "magic-link" || !link.id || !link.flow) return false;
  if (!INLINE_FORM_FLOWS.has(link.flow)) return false;
  const origin = originOf(apiOrigin);
  return !!origin && originOf(link.href) === origin;
}

/** One input of an inline form, as `GET /api/magic-links/links/{id}/form` describes it. */
export interface InlineFormField {
  name: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
  help: string;
  options: { value: string; label: string }[];
  multiline: boolean;
  value: string;
}

/** The inline form's payload: the descriptor plus the link's live status. */
export interface InlineForm {
  id: string;
  flow: string;
  kind: string;
  status: string;
  title: string;
  instructions: string;
  fields: InlineFormField[];
  result: string;
  error: string;
  expires_at: string;
  agent: string;
  pin_required: boolean;
}

/** A link whose status says it can no longer take a submission. */
export function inlineFormSpent(status: string): boolean {
  return status === "done" || status === "error" || status === "expired" || status === "revoked";
}

/** Regex fallback for a string field in a truncated (unparseable) JSON result. */
function rawField(raw: string, name: string): string {
  const m = new RegExp(`"${name}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`).exec(raw);
  if (!m) return "";
  try {
    return JSON.parse(`"${m[1]}"`) as string;
  } catch {
    return m[1];
  }
}

function isHttp(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

export function extractConnectLinks(tools: ToolCall[]): ConnectLink[] {
  const out: ConnectLink[] = [];
  const seen = new Set<string>();
  for (const call of tools) {
    const raw = call.result;
    if (!call.hasResult || !raw) continue;
    const magicTool = call.name.startsWith("magic_link");
    // Cheap prefilter before any JSON.parse — most results are neither shape.
    if (!raw.includes("authorization_url") && !(magicTool && raw.includes('"url"'))) continue;

    let parsed: Record<string, unknown> | null = null;
    try {
      const p: unknown = JSON.parse(raw);
      parsed = p && typeof p === "object" && !Array.isArray(p) ? (p as Record<string, unknown>) : null;
    } catch {
      parsed = null; // truncated persisted result — fall back to field regexes
    }
    const get = (name: string): string => {
      if (parsed) {
        const v = parsed[name];
        return typeof v === "string" ? v : "";
      }
      return rawField(raw, name);
    };

    if (get("error")) continue;
    const status = get("status");
    // A finished/expired OAuth flow (status polls return the same shape) must
    // not offer a Connect button for a link that can no longer work.
    if (status === "authorized" || status === "expired") continue;

    const authUrl = get("authorization_url");
    if (isHttp(authUrl)) {
      if (!seen.has(authUrl)) {
        seen.add(authUrl);
        out.push({ href: authUrl, kind: "mcp-oauth", serverUrl: get("url") });
      }
      continue;
    }
    if (magicTool) {
      const url = get("url");
      if (isHttp(url) && !seen.has(url)) {
        seen.add(url);
        out.push({
          href: url,
          kind: "magic-link",
          label: get("label"),
          pin: get("pin"),
          id: get("id"),
          flow: get("flow"),
        });
      }
    }
  }
  return out;
}

/**
 * Human service name for an MCP endpoint with no directory match:
 * `https://connect.composio.dev/mcp` → "Composio". Best-effort — the
 * registrable label beats subdomain noise like `mcp.` / `connect.` / `api.`.
 */
export function serviceNameFromUrl(url: string): string {
  try {
    const host = new URL(url).hostname.toLowerCase();
    const parts = host.split(".").filter(Boolean);
    // Drop the TLD (and common second-level suffixes like co.uk) to land on
    // the registrable label; a bare host keeps itself.
    let label = parts.length >= 2 ? parts[parts.length - 2] : parts[0] || "";
    const secondLevel = new Set(["co", "com", "net", "org", "gov", "edu", "ac"]);
    if (parts.length >= 3 && secondLevel.has(label)) label = parts[parts.length - 3];
    return label ? label.charAt(0).toUpperCase() + label.slice(1) : "";
  } catch {
    return "";
  }
}

/** Hostname of a URL, for the card's subtitle ("connect.composio.dev"). */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}
