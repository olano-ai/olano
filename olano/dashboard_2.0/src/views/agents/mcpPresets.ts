// The "Connect a service" presets shown in the Add MCP server modal.
//
// The ROWS are no longer written here: they are derived from
// ./mcpBundled.generated.ts, whose source of truth is olano/core/bundled_mcp.json
// — the same file the Python runtime serves as its offline baseline. Adding a
// provider is a cloud-side data change plus a regeneration
// (`uv run python scripts/gen_bundled_mcp.py`), not an edit here.
//
// This file previously held a hand-maintained copy, and the two drifted: the
// same server appeared as `parallel-search` here and `parallel` on the Python
// side, with a different title, description, oauthClient and a `Web & Search`
// category that existed nowhere else. Onboarding looked one of those ids up by
// hand, so the wizard and the modal could offer different servers.

import { BUNDLED_SERVERS, type BundledServer } from "./mcpBundled.generated";

export type McpPreset = {
  id: string;
  title: string;
  category: string;
  url: string;
  transport: "http" | "sse" | "auto";
  auth: "oauth" | "none";
  // "dcr" = the server auto-registers a client; "preregistered" = the user must
  // create an OAuth app and supply Client ID/secret.
  oauthClient: "dcr" | "preregistered";
  appConsoleUrl?: string;
  appConsoleLabel?: string;
  // TRANSLATION KEY (`mcp.preset.<id>.notes` in ./strings.ts) for the presets
  // below, resolved via `t()` in SubModals.tsx — same pattern as
  // connectorCatalog.ts. Cloud-sourced presets from mcpDirectory.ts's
  // `toPreset()` put literal cloud text here instead; `t()` falls back to
  // returning an unknown key verbatim, so that still renders correctly.
  notes?: string;
  // Optional metadata from the cloud directory (absent in the bundled fallback):
  // a small company logo and a one-line description for richer directory cards.
  logoUrl?: string;
  description?: string;
};

// Raw entry shape from the cloud directory API (camelCase, both kinds). The
// generated bundled rows are the same shape, which is what lets one converter
// serve both sources.
export interface DirectoryEntry {
  slug: string;
  name: string;
  description?: string | null;
  kind?: string;
  category?: string | null;
  transport?: string | null;
  auth?: string | null;
  oauthClient?: string | null;
  url?: string | null;
  command?: string | null;
  args?: string[] | null;
  envKeys?: string[] | null;
  logoUrl?: string | null;
  homepageUrl?: string | null;
  appConsoleUrl?: string | null;
  appConsoleLabel?: string | null;
  badge?: string | null;
  runCommand?: string | null;
  exampleJson?: string | null;
  notes?: string | null;
}

// The one directory-entry → preset converter, used for both the cloud payload
// (mcpDirectory.ts) and the bundled rows below.
export function toPreset(e: DirectoryEntry): McpPreset {
  const transport = (e.transport as McpPreset["transport"]) || "auto";
  const auth = (e.auth as McpPreset["auth"]) || "none";
  const oauthClient = (e.oauthClient as McpPreset["oauthClient"]) || "dcr";
  return {
    id: e.slug,
    title: e.name,
    category: e.category || "Other",
    url: e.url || "",
    transport,
    auth,
    oauthClient,
    ...(e.appConsoleUrl ? { appConsoleUrl: e.appConsoleUrl } : {}),
    ...(e.appConsoleLabel ? { appConsoleLabel: e.appConsoleLabel } : {}),
    ...(e.notes ? { notes: e.notes } : {}),
    ...(e.logoUrl ? { logoUrl: e.logoUrl } : {}),
    ...(e.description ? { description: e.description } : {}),
  };
}

// Slugs whose notes are translated in ./strings.ts as `mcp.preset.<slug>.notes`.
// A slug listed here gets the KEY (so `t()` resolves it in en/zh/es); anything
// else keeps the snapshot's literal English, which `t()` passes through
// unchanged. Add a slug here only once its three translations exist — listing
// one without them renders the raw key to the user.
const TRANSLATED_PRESET_NOTES = new Set([
  "asana",
  "atlassian",
  "canva",
  "github",
  "hubspot",
  "intercom",
  "linear",
  "notion",
  "parallel",
  "paypal",
  "sentry",
  "square",
  "stripe",
  "vercel",
  "webflow",
  "wix",
  "zapier",
]);

function withTranslatedNotes(row: BundledServer): McpPreset {
  const preset = toPreset(row);
  return TRANSLATED_PRESET_NOTES.has(row.slug)
    ? { ...preset, notes: `mcp.preset.${row.slug}.notes` }
    : preset;
}

export const MCP_PRESETS: McpPreset[] = BUNDLED_SERVERS.filter(
  (r) => r.kind !== "example",
).map(withTranslatedNotes);

// Sorted, so the category chips render in the same order as the Python side's
// `_categories_of` rather than in array-insertion order.
export const MCP_PRESET_CATEGORIES: string[] = Array.from(
  new Set(MCP_PRESETS.map((p) => p.category)),
).sort();
