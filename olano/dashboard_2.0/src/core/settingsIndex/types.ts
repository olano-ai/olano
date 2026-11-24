// The shape of one searchable setting.
//
// This module is deliberately tiny and dependency-free: it is imported by
// Layout (the always-loaded shell chunk), while catalog.ts and adapters.ts —
// which pull in every view's strings.ts — sit behind the dynamic import in
// ./index.ts. Keep it that way; a static import of the catalog from Layout puts
// the whole ~18k-line string corpus in the shell bundle with no visible symptom.

import type { LocalDict } from "../i18n";

export interface SettingsEntry {
  /** Stable dot-namespaced id. Doubles as the data-setting-id anchor. */
  id: string;
  /** A card/section heading, or one control inside it. Sections rank higher. */
  kind: "section" | "field";
  /** Route path. ":agent" is substituted with the current agent at select time. */
  route: string;
  /** Extra search params the target needs, e.g. { tab: "connectors" }. */
  params?: Record<string, string>;
  /** The owning view's strings.ts, for resolving labelKey / hintKey. */
  dict?: LocalDict;
  labelKey: string;
  /** Help text key. Searched at low weight; not rendered in the row. */
  hintKey?: string;
  /** Trail of i18n keys, outermost first: ["nav.config", "section.speech"]. */
  breadcrumbKeys: string[];
  /** English synonyms, always searched regardless of the UI language. */
  keywords?: string[];
  /** Hidden from non-admin members (the whole /config + /vault surface). */
  adminOnly?: boolean;
  /** Hidden when this capability is explicitly false (tab_* deployment gates). */
  capability?: string;
  /**
   * The target only mounts under a live condition (a gateway is attached, usage
   * budgets are configured). The drift guard tolerates its absence; everything
   * else must be present or CI fails.
   */
  conditional?: boolean;
}

/** The route + params + focus param a selected entry navigates to. */
export function entryHref(entry: SettingsEntry, agent?: string): string {
  const path = entry.route.includes(":agent")
    ? agent
      ? entry.route.replace(":agent", encodeURIComponent(agent))
      : "/agents"
    : entry.route;
  // With no agent to substitute we land on the roster, where an anchor for a
  // per-agent field would never resolve — so drop the focus param too.
  if (entry.route.includes(":agent") && !agent) return path;
  const params = new URLSearchParams(entry.params);
  params.set("focus", entry.id);
  return `${path}?${params.toString()}`;
}
