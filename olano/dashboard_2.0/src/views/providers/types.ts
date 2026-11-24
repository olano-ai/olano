// View-specific types mirroring the backend catalog + codex auth endpoints.
// Shapes match olano/runtime/api.py:catalog_providers() and the codex
// device-flow session objects in _codex_auth_sessions.

import type { ModelMeta } from "../agents/types";

export interface ProviderAuth {
  // "device_code" (Codex OAuth) or "deployment_token" (Olano-managed tiers).
  type: string;
  // Capability: usable on this box at all. The model pickers gate on this, so
  // a provider that self-heals (Olano mid-enrollment) stays true here.
  connected: boolean;
  // Live health. "degraded" = connected but not currently working — e.g.
  // attached to cloud.olano.ai with no gateway key yet, where every olano tier
  // 401s. Must not render as a green tick.
  status: "connected" | "degraded" | "not_connected" | string;
  error: string | null;
  // Olano on a self-hosted (open) box only, absent on a managed deployment:
  // which key family serves the gateway ("env" = an operator's own
  // OLANO_GATEWAY_KEY, "deployment" = the enrolled key, "account" = a pasted
  // Olano API key, null = none), and whether the card may connect or remove an
  // account key here (/api/olano/credits-key).
  source?: "env" | "deployment" | "account" | null;
  manageable?: boolean;
  // Codex-only diagnostics; absent for other auth types.
  auth_store?: string | null;
  token_preview?: string | null;
  expires_soon?: boolean | null;
}

export interface Provider {
  key: string;
  name: string;
  package: string;
  env_var: string | null;
  available: boolean;
  env_set: boolean;
  models: string[];
  // Per-model metadata from the synced cloud catalog (bare model name → meta);
  // shared shape with the agents view — see agents/types.ts ModelMeta.
  model_meta?: Record<string, ModelMeta>;
  notes: string;
  docs_url?: string | null;
  console_url?: string | null;
  auth: ProviderAuth | null;
}

export interface CatalogResponse {
  providers: Provider[];
  // Deployment entitlement: the plan excludes BYOK (connecting your own
  // provider keys / Codex subscriptions). Providers render, key entry locks.
  byok_locked?: boolean;
  // Which shape came back. "basic" (requested with ?fields=basic) leaves
  // `models` empty and omits `model_meta` — it skips the per-model reasoning
  // probes that make the full payload cost seconds. Only ask for it from a
  // caller that reads neither.
  fields?: "basic" | "full";
  // Cloud-catalog provenance: bundled/disk/cloud + the synced revision.
  catalog?: { revision?: number; source?: string | null };
}

// POST /api/auth/openai-codex/start and GET .../poll/{id} both return this.
export interface CodexSession {
  session_id: string;
  status: "pending" | "approved" | "error" | "expired";
  user_code: string;
  verification_url: string;
  interval: number;
  expires_in: number;
  expires_at?: number;
  error_message: string | null;
  models?: string[];
}

export const CODEX_KEY = "openai-codex";
export const OLANO_KEY = "olano";

// Whether every requirement for actually calling this provider is met right
// now: the package is installed AND it is authenticated — an OAuth / deployment
// provider by a live connection, a key-based one by its key being present
// (`env_set` is true for a key in the process env OR in the vault, see
// runtime/api.py:_key_set), a keyless local one (Ollama) by nothing further.
//
// Mirrors agents/catalog.ts:providerUsable() — the predicate the model pickers
// gate on — plus the one exclusion the status badge above the chips already
// makes: `status: "degraded"` is a provider that is attached but not currently
// working (Olano mid-enrollment), and it must not paint its models as ready any
// more than it may render a green tick.
export function providerReady(p: Provider): boolean {
  if (!p.available) return false;
  if (p.auth) return p.auth.connected && p.auth.status !== "degraded";
  if (p.env_var) return p.env_set;
  return true;
}

// Filter result for a provider against the search query.
export interface ProviderMatch {
  provider: Provider;
  show: boolean;
  models: string[];
}

// Mirror of the original renderProviders() filter:
//   models = !query ? all : models matching (name + key + model)
//   provider is hidden only when query is set, no models match, AND the
//   provider name itself doesn't match.
export function matchProvider(p: Provider, query: string): ProviderMatch {
  if (!query) return { provider: p, show: true, models: p.models };
  const needle = query.toLowerCase();
  const nameHit = p.name.toLowerCase().includes(needle) || p.key.toLowerCase().includes(needle);
  const models = p.models.filter((m) =>
    `${p.name} ${p.key} ${m}`.toLowerCase().includes(needle),
  );
  const show = models.length > 0 || nameHit;
  // When the provider name matches but no individual model does, the original
  // still shows the provider — with all its models, not an empty chip list.
  return { provider: p, show, models: models.length ? models : nameHit ? p.models : [] };
}
