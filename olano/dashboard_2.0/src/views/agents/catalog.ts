// Catalog loader hook — fetches providers (model picker), tool names (tool
// picker), and skills (skills tab) once and shares them across the create /
// edit / detail surfaces. Mirrors the original ensureCatalog() memoization.

import { useEffect, useState } from "react";
import { subscribeStream } from "../../core/agentStream";
import { api } from "../../core/api";
import type {
  InternalToolRow,
  ModelAliasEntry,
  ProviderEntry,
  ProvidersResponse,
  SkillEntry,
  SkillsResponse,
  ToolBundle,
  ToolEntry,
  ToolsResponse,
} from "./types";

export interface Catalog {
  providers: ProviderEntry[];
  // Deployment-default aliases (default:turbo/pro/max) with their current
  // targets, straight from the providers payload.
  modelAliases: ModelAliasEntry[];
  toolNames: string[];
  // Full tool objects (name/description/category/bundles/requirements) powering
  // the categorized tool browser. `toolNames` is kept as a flat convenience.
  tools: ToolEntry[];
  bundles: ToolBundle[];
  skills: SkillEntry[];
  // Auto-injected universal/document tool names — the Tools tab routes unchecks
  // of these into the agent's `disabled_tools` so the removal actually sticks.
  agentDefaults: string[];
}

const EMPTY: Catalog = {
  providers: [],
  modelAliases: [],
  toolNames: [],
  tools: [],
  bundles: [],
  skills: [],
  agentDefaults: [],
};

// Module-level cache so re-opening modals doesn't re-fetch. Invalidated by the
// `catalog_updated` SSE event when the background cloud sync lands new data.
let cached: Catalog | null = null;
let inflight: Promise<Catalog> | null = null;

// "provider:model" → context window (tokens), from the synced catalog's
// model_meta. Consulted by chat's modelContextWindow() so the context bar uses
// real per-model windows instead of the provider-prefix ladder.
const contextWindows = new Map<string, number>();

// "default:<tier>" → the concrete "provider:model" it currently resolves to,
// from the providers payload's `model_aliases` (the backend owns resolution —
// config.yaml override or the box's built-in target). Module state like
// contextWindows above, refreshed on every catalog fetch.
const aliasTargets = new Map<string, string>();

/** True for a `default:<tier>` deployment-alias picker value. */
export function isAliasOption(value: string): boolean {
  return value.startsWith("default:");
}

/**
 * The concrete "provider:model" behind a `default:*` alias, per the last
 * catalog fetch — or the input unchanged for non-alias values (and for an
 * alias the catalog hasn't announced, so callers degrade gracefully).
 */
export function resolveModelAlias(value: string | undefined | null): string | null {
  if (!value) return null;
  if (!isAliasOption(value)) return value;
  return aliasTargets.get(value) ?? value;
}

function indexModelAliases(aliases: ModelAliasEntry[] | undefined): void {
  aliasTargets.clear();
  for (const a of aliases ?? []) {
    if (a?.value && a.target) aliasTargets.set(a.value, a.target);
  }
}

export function catalogContextWindow(modelId: string | undefined | null): number | null {
  if (!modelId) return null;
  return contextWindows.get(modelId) ?? null;
}

function indexContextWindows(providers: ProviderEntry[]): void {
  for (const p of providers) {
    for (const [model, meta] of Object.entries(p.model_meta ?? {})) {
      if (typeof meta?.context_window === "number" && meta.context_window > 0) {
        contextWindows.set(`${p.key}:${model}`, meta.context_window);
      }
    }
  }
}

export function invalidateCatalog(): void {
  cached = null;
}

async function fetchCatalog(): Promise<Catalog> {
  if (cached) return cached;
  if (inflight) return inflight;
  inflight = (async () => {
    const [tools, skills, providers] = await Promise.all([
      api.get<ToolsResponse>("/api/catalog/tools").catch(() => ({ tools: [] }) as ToolsResponse),
      api.get<SkillsResponse>("/api/catalog/skills").catch(() => ({ skills: [] }) as SkillsResponse),
      api.get<ProvidersResponse>("/api/catalog/providers").catch(() => ({ providers: [] }) as ProvidersResponse),
    ]);
    const toolList = tools.tools || [];
    indexContextWindows(providers.providers || []);
    indexModelAliases(providers.model_aliases);
    cached = {
      providers: providers.providers || [],
      modelAliases: providers.model_aliases || [],
      toolNames: toolList.map((t) => t.name),
      tools: toolList,
      bundles: tools.bundles || [],
      skills: skills.skills || [],
      agentDefaults: tools.agent_defaults || [],
    };
    return cached;
  })();
  try {
    return await inflight;
  } finally {
    inflight = null;
  }
}

export function useCatalog(): Catalog {
  const [cat, setCat] = useState<Catalog>(cached ?? EMPTY);
  useEffect(() => {
    let alive = true;
    void fetchCatalog().then((c) => {
      if (alive) setCat(c);
    });
    // Background cloud sync landed fresh catalog data → drop the module cache
    // and refetch so pickers/badges update without a page reload.
    const unsubscribe = subscribeStream((frame) => {
      if (frame.type !== "catalog_updated") return;
      invalidateCatalog();
      void fetchCatalog().then((c) => {
        if (alive) setCat(c);
      });
    });
    return () => {
      alive = false;
      unsubscribe();
    };
  }, []);
  return cat;
}

// The agent's "extra" tools (agent.json5 `tools`) merged with the universal
// defaults, minus anything disabled — the one computation for "how many extra
// tools does this agent have," shared by the Tools tab badge and the Tools
// tab body so they can no longer disagree the way they used to (badge read
// the raw `tools` array, body read this merge).
export function selectedExtraTools(
  cfg: { tools?: string[]; disabled_tools?: string[] },
  agentDefaults: string[],
): string[] {
  const disabledSet = new Set(cfg.disabled_tools || []);
  const set = new Set((cfg.tools || []).filter((n) => !disabledSet.has(n)));
  for (const d of agentDefaults) if (!disabledSet.has(d)) set.add(d);
  return [...set];
}

// Internal rows currently on, resolved against the LIVE `disabled_tools`
// rather than the server-computed `on` flag: a toggleable row is on iff not
// disabled — the same rule the server applies (internal_tools.py:
// `on=t.name not in disabled`) — so an optimistic `disabled_tools` edit
// takes effect immediately instead of flickering back until savePatch's
// reconcile GET lands. Locked rows (toggleable: false — condition-off or
// allow-list-excluded) keep the server verdict, which no client-side edit
// can change.
export function internalOnNames(cfg: {
  disabled_tools?: string[];
  internal_tools?: InternalToolRow[];
}): string[] {
  const disabled = new Set(cfg.disabled_tools || []);
  return (cfg.internal_tools || [])
    .filter((r) => (r.toggleable === false ? r.on : !disabled.has(r.name)))
    .map((r) => r.name);
}

// One picker option. `olano` marks the Olano-managed tiers so every picker can
// pin them at the top with their own styling — they are the product's default
// choice (billed in credits, no provider key), not just another provider row.
// `tier` carries the bare tier id (`auto`/`turbo`/`pro`/`max`) so the
// renderers can look up its translated one-line description (ModelOptions.tsx
// `olanoTierHintKey`) without re-parsing the value. `alias` marks the
// deployment-default options (`default:*`), which sort above even the Olano
// tiers; their `target` is the concrete model the alias currently resolves to,
// so renderers can say "currently Olano Turbo" without re-asking the map.
export interface ModelOption {
  value: string;
  label: string;
  olano?: boolean;
  alias?: boolean;
  target?: string;
  tier?: string;
}

// Product names for the deployment aliases — mirrors OLANO_TIER_LABELS below.
const ALIAS_LABELS: Record<string, string> = {
  turbo: "Default · Turbo",
  pro: "Default · Pro",
  max: "Default · Max",
};

// The deployment-default options, from the last catalog fetch. Empty until a
// providers payload with `model_aliases` has been indexed (an old backend), so
// pickers on a stale runtime simply don't offer aliases rather than offering
// ones the runtime can't resolve.
export function aliasModelOptions(): ModelOption[] {
  const out: ModelOption[] = [];
  for (const [value, target] of aliasTargets) {
    const tier = value.slice("default:".length);
    out.push({
      value,
      label: ALIAS_LABELS[tier] ?? `Default · ${tier.charAt(0).toUpperCase()}${tier.slice(1)}`,
      alias: true,
      tier,
      target,
    });
  }
  return out;
}

// Product names for the tiers. `auto` stays listed even though the runtime no
// longer offers it (olano_gateway.HIDDEN_TIERS — it doesn't route per request
// yet): an agent still configured with `olano:auto` has to render as the name
// its owner picked, via modelValueLabel() below.
const OLANO_TIER_LABELS: Record<string, string> = {
  auto: "Olano Auto",
  turbo: "Olano Turbo",
  pro: "Olano Pro",
  max: "Olano Max",
};

// Reading order of the managed tiers: the default first, then cheapest →
// most capable, so the list itself teaches the ladder. Deliberately NOT the
// catalog's order — the cloud catalog sorts models by its own rules (it once
// listed Auto, Pro, Max, Turbo) and a price ladder that jumps around reads as
// an accident. Tiers missing from this list keep catalog order, after these.
const OLANO_TIER_ORDER: string[] = ["auto", "turbo", "pro", "max"];

function olanoTierRank(tier: string | undefined): number {
  const i = tier ? OLANO_TIER_ORDER.indexOf(tier) : -1;
  return i === -1 ? OLANO_TIER_ORDER.length : i;
}

/** True for an `olano:<tier>` picker value. */
export function isOlanoOption(value: string): boolean {
  return value.startsWith("olano:");
}

// Label for a saved `provider:model` the catalog no longer offers — every model
// <select> keeps such a value as its own option so an agent's configured model
// is never silently swapped. Retired Olano tiers (e.g. `olano:auto`, withdrawn
// until the gateway actually routes) go through here so they read as the
// product name the user picked, not a raw id.
export function modelValueLabel(value: string): string {
  if (isAliasOption(value)) {
    const tier = value.slice("default:".length);
    return ALIAS_LABELS[tier] ?? value;
  }
  if (!isOlanoOption(value)) return value;
  const tier = value.slice("olano:".length);
  return OLANO_TIER_LABELS[tier] ?? value;
}

// Flatten providers into "provider:model" options for a <select>. The
// deployment-default aliases (default:*) lead, then the Olano tiers with
// product names ("Olano Pro", never "olano · pro") — the API already lists the
// olano provider first, this makes it a guarantee rather than an ordering
// accident.
export function modelOptions(providers: ProviderEntry[]): ModelOption[] {
  const out: ModelOption[] = [];
  for (const p of providers) {
    for (const m of p.models) {
      if (p.key === "olano") {
        out.push({
          value: `olano:${m}`,
          label: OLANO_TIER_LABELS[m] ?? `Olano ${m.charAt(0).toUpperCase()}${m.slice(1)}`,
          olano: true,
          tier: m,
        });
      } else {
        out.push({ value: `${p.key}:${m}`, label: `${p.name} · ${m}` });
      }
    }
  }
  const olano = out
    .filter((o) => o.olano)
    .sort((a, b) => olanoTierRank(a.tier) - olanoTierRank(b.tier));
  return [...aliasModelOptions(), ...olano, ...out.filter((o) => !o.olano)];
}

// Reasoning-effort capability for a "provider:model" value, from the catalog's
// per-model metadata. null → the model has no effort control (hide the picker).
// A `default:*` alias answers as its current target — the effort control the
// user actually gets is the target's.
export function modelReasoningMeta(
  providers: ProviderEntry[],
  model: string,
): { efforts: string[]; defaultEffort: string | null } | null {
  model = resolveModelAlias(model) ?? model;
  const i = model.indexOf(":");
  if (i <= 0) return null;
  const meta = providers.find((p) => p.key === model.slice(0, i))?.model_meta?.[model.slice(i + 1)];
  if (!meta?.reasoning_efforts?.length) return null;
  return { efforts: meta.reasoning_efforts, defaultEffort: meta.default_effort ?? null };
}

// Whether a provider is actually usable right now: its package is installed and
// it is authenticated. OAuth-style providers (e.g. OpenAI Codex) need a live
// connection; key-based providers need their API key present (env or vault);
// keyless local providers (Ollama) are usable once installed.
export function providerUsable(p: ProviderEntry): boolean {
  if (p.available === false) return false;
  if (p.auth) return !!p.auth.connected;
  if (p.env_var) return p.env_set === true;
  return true;
}

// Model options restricted to authenticated/available providers — used by the
// chat model picker so it never offers models the user can't actually run. An
// alias option is usable iff the provider of its current TARGET is usable —
// "Default · Turbo" pointing at an unauthenticated provider would fail exactly
// like picking that provider directly.
export function usableModelOptions(providers: ProviderEntry[]): ModelOption[] {
  const usable = providers.filter(providerUsable);
  const usableKeys = new Set(usable.map((p) => p.key));
  return modelOptions(usable).filter((o) => {
    if (!o.alias) return true;
    const target = o.target ?? "";
    const i = target.indexOf(":");
    return i > 0 && usableKeys.has(target.slice(0, i));
  });
}
