// Loader for the agent archetype catalog: builtin TEAMS (archetypes that
// supervise subagents), individual EXPERTS (leaf archetypes), and INTEGRATIONS
// (ToolkitAgents — pre-built expert subagents). Backed by /api/archetypes and
// /api/integrations.
//
// Built on the shared catalog-resource store (catalogResource.ts) rather than a
// private module singleton, which is what this used to be. That singleton wrote
// `{teams: [], specialists: [], integrations: []}` into its cache whenever a
// fetch failed — one 401/429/timeout and every picker in the SPA showed
// "Nothing available." until a full page reload, with no retry and no way to
// tell a failure from a genuinely empty catalog.

import {
  prefetchCatalog,
  useCatalogResource,
  type CatalogResource,
} from "../../core/catalogResource";

const KEY = "archetypes";

export interface ArchetypeEntry {
  name: string;
  label: string;
  description: string;
  emoji?: string;
  color?: string;
  category?: string;
  model?: string;
  subagent_count?: number;
}

export interface IntegrationEntry {
  name: string;
  toolkit: string;
  label: string;
  description: string;
  emoji?: string;
  category?: string;
  tags?: string[];
  credential_env_vars?: string[];
}

export interface ArchetypeCatalog {
  teams: ArchetypeEntry[];
  specialists: ArchetypeEntry[];
  integrations: IntegrationEntry[];
}

const EMPTY: ArchetypeCatalog = { teams: [], specialists: [], integrations: [] };

interface ArchetypesResponse {
  specialists?: ArchetypeEntry[];
  teams?: ArchetypeEntry[];
}
interface IntegrationsResponse {
  integrations?: IntegrationEntry[];
}

// Both halves are fetched together and either both land or the whole read is an
// error: a picker showing Teams and Experts but a silently empty Integrations
// tab is the failure mode this replaces, and it is indistinguishable from a box
// that genuinely has no integrations.
async function fetchArchetypes(get: <T>(path: string) => Promise<T>): Promise<ArchetypeCatalog> {
  const [arch, integ] = await Promise.all([
    get<ArchetypesResponse>("/api/archetypes"),
    get<IntegrationsResponse>("/api/integrations"),
  ]);
  return {
    teams: arch.teams || [],
    specialists: arch.specialists || [],
    integrations: integ.integrations || [],
  };
}

/**
 * The archetype + integration catalog, shared across every picker.
 *
 * Returns the last good value (or `EMPTY` before the first load) alongside a
 * status, so callers can render a skeleton while loading and a retry affordance
 * on error instead of an empty list that looks like a working, empty catalog.
 */
export function useArchetypes(): CatalogResource<ArchetypeCatalog> {
  return useCatalogResource(KEY, fetchArchetypes, EMPTY);
}

/**
 * Warm the catalog without mounting a picker. Called from the app shell on
 * idle, so the "New agent" / "Add subagent" modals open onto loaded lists
 * instead of starting their fetch at first mount.
 */
export function prefetchArchetypes(): void {
  prefetchCatalog(KEY, fetchArchetypes);
}
