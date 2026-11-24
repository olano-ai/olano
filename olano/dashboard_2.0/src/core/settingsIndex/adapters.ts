// Entries derived from catalogs that are already declarative.
//
// These cost nothing to maintain: a new connector field in connectorCatalog.ts
// (which already carries its label, help text and group as i18n keys) becomes
// searchable without anyone editing core/settingsIndex/catalog.ts. That is the
// whole reason the two catalogs are read here rather than transcribed.
//
// Phase 1 ships this file empty of adapters — the connector and connection
// adapters land with the agent-settings and connections phases, once those
// views render the matching data-setting-id anchors. Deriving entries for
// anchors that do not exist yet would fail the drift guard.

import type { SettingsEntry } from "./types";

export function derivedEntries(): SettingsEntry[] {
  return [];
}
