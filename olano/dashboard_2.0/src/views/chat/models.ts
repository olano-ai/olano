// Context-window size by model id, used to draw the context-usage bar in the
// chat header. Resolves per-model from the synced cloud catalog first
// (agents/catalog.ts indexes model_meta.context_window once the catalog
// loads); the provider-prefix ladder below is the fallback for models the
// catalog doesn't know — keep it in sync with the backend's
// `runtime/context_report.py:model_context_window()`.
// Model strings are `provider:model` (e.g. `anthropic:claude-sonnet-4-6`).

import { catalogContextWindow, resolveModelAlias } from "../agents/catalog";

export function modelContextWindow(modelId: string | undefined | null): number | null {
  if (!modelId) return null;
  // A `default:*` alias has no window of its own — its current target does.
  modelId = resolveModelAlias(modelId) ?? modelId;
  const fromCatalog = catalogContextWindow(modelId);
  if (fromCatalog) return fromCatalog;
  const id = modelId.toLowerCase();
  if (id.startsWith("olano:")) {
    // Olano-managed tiers: conservative floor per tier (min across each
    // tier's routed targets) — mirrors OLANO_TIER_INFO in
    // olano/models/olano_gateway.py. The gateway never reveals the
    // backing model, so there is nothing to probe; the catalog rows the
    // cloud publishes later take precedence via catalogContextWindow above.
    const tier = id.slice("olano:".length);
    // Since the 2026-08 re-route every tier is backed by 1M-token models, so
    // the floor is 1M for all of them (turbo/auto were 200K while a
    // claude-haiku fallback set the minimum).
    if (tier === "pro" || tier === "reasoning") return 1_000_000;
    if (tier === "auto" || tier === "turbo" || tier === "flash") return 1_000_000;
    return null;
  }
  if (id.startsWith("anthropic:")) return 200_000;
  if (id.startsWith("openai:")) {
    if (id.includes("o1") || id.includes("o3")) return 200_000;
    return 128_000; // gpt-4o / gpt-4.1 / gpt-5 family
  }
  if (id.startsWith("google:") || id.startsWith("gemini:")) return 1_000_000;
  if (id.startsWith("deepseek:")) return 1_000_000; // DeepSeek V4 / V4.1 family
  if (id.startsWith("mistral:")) return 128_000;
  if (id.startsWith("xai:") || id.includes("grok")) return 131_072;
  if (id.startsWith("ollama:")) return 32_768;
  return null;
}
