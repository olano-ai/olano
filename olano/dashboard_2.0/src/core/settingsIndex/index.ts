// Loading and resolving the settings index.
//
// The catalog imports every indexed view's strings.ts (~18k lines across the
// dashboard), so it MUST stay behind a dynamic import: the palette lives in
// Layout, which is in the always-loaded shell chunk. Importing ./catalog
// statically from anywhere in that chunk moves the whole string corpus into the
// initial bundle, with no visible symptom other than a slower first paint.

import type { Lang } from "../i18n";
import { resolveLocal } from "../i18n";
import type { Searchable } from "../settingsSearch";
import type { SettingsEntry } from "./types";

export type { SettingsEntry } from "./types";
export { entryHref } from "./types";

/** An entry with its i18n keys resolved, ready for the scorer and the renderer. */
export interface ResolvedEntry extends Searchable {
  entry: SettingsEntry;
  label: string;
  breadcrumb: string;
  hint?: string;
  keywords?: string[];
}

let pending: Promise<SettingsEntry[]> | null = null;

/**
 * Load the index, once per page. Single-flighted: two palette opens in the same
 * session share one chunk fetch.
 *
 * A failed load resolves to an empty index rather than rejecting — the palette
 * still works as the nav/agent/task filter it was before, which is strictly
 * better than an error state over a search enhancement.
 */
export function loadSettingsIndex(): Promise<SettingsEntry[]> {
  if (!pending) {
    pending = Promise.all([import("./catalog"), import("./adapters")])
      .then(([catalog, adapters]) => {
        const entries = [...catalog.CONFIG_ENTRIES, ...adapters.derivedEntries()];
        // Published for tests/e2e/test_settings_index.py, the drift guard that
        // asserts every entry's anchor still exists in the DOM. The catalog is
        // TypeScript, so the alternative was generating a JSON artifact at
        // build time — one more thing that can itself go stale. This is UI
        // metadata (ids, i18n keys), nothing sensitive.
        (window as unknown as { __olanoSettingsIndex?: unknown }).__olanoSettingsIndex = entries.map(
          ({ id, route, params, conditional, adminOnly }) => ({ id, route, params, conditional, adminOnly }),
        );
        return entries;
      })
      .catch(() => {
        // Don't cache the failure: a chunk that failed on a flaky network must
        // be retryable on the next open (same rule as core/catalogResource.ts).
        pending = null;
        return [];
      });
  }
  return pending;
}

/**
 * Resolve labels/hints/breadcrumbs for the current language.
 *
 * `t` is the central dictionary's translator; each entry's own view dict wins
 * over it, matching what useT does inside the view that owns the field.
 */
export function resolveEntries(
  entries: SettingsEntry[],
  lang: Lang,
  t: (key: string) => string,
): ResolvedEntry[] {
  const tr = (dict: SettingsEntry["dict"], key: string) => resolveLocal(dict, lang, key) ?? t(key);
  return entries.map((entry) => ({
    entry,
    label: tr(entry.dict, entry.labelKey),
    breadcrumb: entry.breadcrumbKeys.map((k) => tr(entry.dict, k)).join(" › "),
    hint: entry.hintKey ? tr(entry.dict, entry.hintKey) : undefined,
    keywords: entry.keywords,
  }));
}
