// Loader for the MCP server directory shown in the "Add MCP server" modal:
// the "Connect a service" presets and the copy-paste example configs. Backed by
// the backend proxy GET /api/mcp/directory, which fetches the cloud registry
// and caches it, so the catalog can be maintained centrally
// without shipping a dashboard build. Module-cached like archetypes.ts/catalog.ts
// so re-opening the modal is instant, and it falls back to the bundled snapshot
// below if the backend/cloud is unreachable — the modal always has a list.

import { useEffect, useState } from "react";
import { api } from "../../core/api";
import type { McpServer } from "./types";
import {
  MCP_PRESETS,
  MCP_PRESET_CATEGORIES,
  toPreset,
  type DirectoryEntry,
  type McpPreset,
} from "./mcpPresets";
import { BUNDLED_SERVERS } from "./mcpBundled.generated";

// A copy-paste starter (usually a local stdio server). Rendered in the same
// unified catalog as the presets, distinguished by its badge.
//
// `description` is a TRANSLATION KEY for the bundled fallback list below
// (resolved via `t()` in SubModals.tsx, same pattern as connectorCatalog.ts),
// but a literal string for cloud-sourced entries (`toExample()`) — the cloud
// directory has no i18n of its own, so its text is shown as-is. `t()` falls
// back to returning an unknown key verbatim, so a literal cloud description
// renders correctly either way.
export type McpExample = {
  id: string;
  title: string;
  badge: string;
  description: string;
  config: McpServer;
  runCommand?: string;
  json?: string;
  logoUrl?: string;
};

export interface McpDirectory {
  presets: McpPreset[];
  examples: McpExample[];
  categories: string[];
  source: string; // "cloud" | "cache" | "bundled"
}

// ── Bundled offline fallback (mirrors the cloud seed) ───────────────────────
// Both halves of the offline fallback now derive from the SAME generated rows
// (source of truth: olano/core/bundled_mcp.json, which the Python runtime reads
// as its own baseline) — presets via mcpPresets.ts, examples here. They used to
// be two hand-written lists, which is how `github-docker-stdio` came to exist in
// both fallbacks and in no cloud row at all.
//
// `description` carries a TRANSLATION KEY where ./strings.ts has one, and the
// snapshot's literal English otherwise; `t()` returns an unknown key verbatim,
// so both render correctly. Every bundled example is translated today.
const TRANSLATED_EXAMPLE_DESCRIPTIONS = new Set([
  "everything-http",
  "everything-sse",
  "everything-stdio",
  "github-docker-stdio",
  "stripe-example",
  "time-python-stdio",
]);

const BUNDLED_EXAMPLES: McpExample[] = BUNDLED_SERVERS.filter(
  (r) => r.kind === "example",
).map((row) => {
  const example = toExample(row);
  return TRANSLATED_EXAMPLE_DESCRIPTIONS.has(row.slug)
    ? { ...example, description: `mcp.directory.${row.slug}.description` }
    : example;
});

const BUNDLED: McpDirectory = {
  presets: MCP_PRESETS,
  examples: BUNDLED_EXAMPLES,
  categories: MCP_PRESET_CATEGORIES,
  source: "bundled",
};

// Re-export so SubModals (and tests) can reach the offline examples directly.
export { BUNDLED_EXAMPLES as MCP_EXAMPLES };

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function toExample(e: DirectoryEntry): McpExample {
  // env_keys in the registry are variable NAMES the server expects — prefill
  // them as empty KEY= lines so the guided Environment field shows what to set.
  const env = Object.fromEntries((e.envKeys || []).map((k) => [k, ""]));
  const config: McpServer = {
    url: e.url || e.slug,
    transport: e.transport || "sse",
    enabled: true,
    ...(e.command ? { command: e.command } : {}),
    ...(e.args && e.args.length ? { args: e.args } : {}),
    ...(Object.keys(env).length ? { env } : {}),
    ...(e.auth && e.auth !== "none" ? { auth: e.auth } : {}),
  };
  return {
    id: e.slug,
    title: e.name,
    badge: e.badge || e.transport || "",
    description: e.description || "",
    config,
    ...(e.runCommand ? { runCommand: e.runCommand } : {}),
    ...(e.exampleJson ? { json: prettyJson(e.exampleJson) } : {}),
    ...(e.logoUrl ? { logoUrl: e.logoUrl } : {}),
  };
}

let cached: McpDirectory | null = null;
let inflight: Promise<McpDirectory> | null = null;

async function fetchDirectory(): Promise<McpDirectory> {
  if (cached) return cached;
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const raw = await api.get<{
        presets?: DirectoryEntry[];
        examples?: DirectoryEntry[];
        categories?: string[];
        source?: string;
      }>("/api/mcp/directory");
      const presets = (raw.presets || []).map(toPreset);
      const examples = (raw.examples || []).map(toExample);
      // Empty payload → keep the bundled list rather than showing nothing.
      cached = {
        presets: presets.length ? presets : MCP_PRESETS,
        examples: examples.length ? examples : BUNDLED_EXAMPLES,
        categories:
          raw.categories && raw.categories.length
            ? raw.categories
            : MCP_PRESET_CATEGORIES,
        source: raw.source || "cloud",
      };
    } catch {
      cached = BUNDLED;
    } finally {
      inflight = null;
    }
    return cached!;
  })();
  return inflight;
}

/**
 * The MCP directory, module-cached. Returns the bundled snapshot immediately
 * (so the modal renders at once) and swaps in the cloud list when it arrives.
 */
export function useMcpDirectory(): McpDirectory & { loading: boolean } {
  const [dir, setDir] = useState<McpDirectory>(cached || BUNDLED);
  const [loading, setLoading] = useState(!cached);
  useEffect(() => {
    if (cached) {
      setDir(cached);
      setLoading(false);
      return;
    }
    let alive = true;
    void fetchDirectory().then((d) => {
      if (alive) {
        setDir(d);
        setLoading(false);
      }
    });
    return () => {
      alive = false;
    };
  }, []);
  return { ...dir, loading };
}
