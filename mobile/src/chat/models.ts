/**
 * Which model a conversation runs on, and how hard it thinks.
 *
 * Per conversation, as in the dashboard's chat header: picking a model in one
 * conversation changes that conversation only, a new one opens on the agent's
 * own model, and reopening one continues on the model its last reply used
 * (the runner stamps every reply with it; the thread payload reports the
 * newest stamp as `model` / `reasoning_effort`). Each message is sent with
 * the pair, and the deployment ignores a value equal to the agent's own.
 *
 * The choices come from `GET /api/catalog/providers` and follow the
 * dashboard's rules (views/agents/catalog.ts, views/chat/chatModel.ts): only
 * providers that are installed and signed in, the deployment's defaults
 * first, then the Olano tiers cheapest to most capable, then everything else.
 * That module keeps React state and its own API client, so unlike the pure
 * modules in shared/dashboard.ts the app cannot import it.
 *
 * Pure, for `node --test`.
 */

export interface ModelMeta {
  reasoning_efforts?: string[] | null;
  default_effort?: string | null;
}

export interface ProviderEntry {
  key: string;
  name: string;
  models: string[];
  model_meta?: Record<string, ModelMeta>;
  available?: boolean;
  env_var?: string | null;
  env_set?: boolean;
  auth?: { connected?: boolean } | null;
}

export interface ProvidersResponse {
  providers: ProviderEntry[];
  model_aliases?: { value: string; tier: string; target: string | null }[];
}

export interface ModelChoice {
  /** What is sent: `provider:model`, `olano:<tier>` or `default:<tier>`. */
  value: string;
  label: string;
  /** A second line: what an Olano tier is for, or what a default runs on now. */
  hint?: string;
  group: "default" | "olano" | "other";
}

const OLANO_TIERS: Record<string, { label: string; hint: string }> = {
  auto: { label: "Olano Auto", hint: "Picks the best tier for each task" },
  turbo: { label: "Olano Turbo", hint: "Fast and cheap for everyday tasks" },
  pro: { label: "Olano Pro", hint: "More power for harder, multi-step work" },
  max: { label: "Olano Max", hint: "Maximum capability, at the highest cost" },
};
const OLANO_ORDER = ["auto", "turbo", "pro", "max"];

function capitalize(word: string): string {
  return word ? word.charAt(0).toUpperCase() + word.slice(1) : word;
}

/** Installed and signed in: a model the deployment can actually run. */
export function providerUsable(p: ProviderEntry): boolean {
  if (p.available === false) return false;
  if (p.auth) return !!p.auth.connected;
  if (p.env_var) return p.env_set === true;
  return true;
}

function aliasTarget(res: ProvidersResponse | undefined, value: string): string | null {
  return res?.model_aliases?.find((a) => a.value === value)?.target ?? null;
}

/** "Olano Pro", "Default · Turbo", or the model's own id without its provider. */
export function modelLabel(value: string): string {
  if (!value) return "";
  if (value.startsWith("default:")) return `Default · ${capitalize(value.slice("default:".length))}`;
  if (value.startsWith("olano:")) {
    const tier = value.slice("olano:".length);
    return OLANO_TIERS[tier]?.label ?? `Olano ${capitalize(tier)}`;
  }
  const i = value.indexOf(":");
  return i > 0 ? value.slice(i + 1) : value;
}

/** Every model this deployment can run, in the order the dashboard lists them. */
export function modelChoices(res: ProvidersResponse | undefined): ModelChoice[] {
  if (!res) return [];
  const usable = res.providers.filter(providerUsable);
  const usableKeys = new Set(usable.map((p) => p.key));
  const defaults: ModelChoice[] = [];
  for (const alias of res.model_aliases ?? []) {
    const target = alias.target ?? "";
    const i = target.indexOf(":");
    if (i <= 0 || !usableKeys.has(target.slice(0, i))) continue;
    defaults.push({
      value: alias.value,
      label: modelLabel(alias.value),
      hint: `Currently ${modelLabel(target)}`,
      group: "default",
    });
  }
  const olano: ModelChoice[] = [];
  const other: ModelChoice[] = [];
  for (const p of usable) {
    for (const m of p.models) {
      if (p.key === "olano") {
        olano.push({ value: `olano:${m}`, label: modelLabel(`olano:${m}`), hint: OLANO_TIERS[m]?.hint, group: "olano" });
      } else {
        other.push({ value: `${p.key}:${m}`, label: `${p.name} · ${m}`, group: "other" });
      }
    }
  }
  const rank = (c: ModelChoice) => {
    const i = OLANO_ORDER.indexOf(c.value.slice("olano:".length));
    return i === -1 ? OLANO_ORDER.length : i;
  };
  olano.sort((a, b) => rank(a) - rank(b));
  return [...defaults, ...olano, ...other];
}

/**
 * The reasoning levels a model offers, and the one it uses when none is
 * picked; null when it has no such control. A default answers as the model
 * it currently runs on.
 */
export function reasoningLevels(
  res: ProvidersResponse | undefined,
  model: string,
): { levels: string[]; defaultLevel: string | null } | null {
  if (!res || !model) return null;
  const concrete = model.startsWith("default:") ? aliasTarget(res, model) ?? model : model;
  const i = concrete.indexOf(":");
  if (i <= 0) return null;
  const meta = res.providers.find((p) => p.key === concrete.slice(0, i))?.model_meta?.[concrete.slice(i + 1)];
  if (!meta?.reasoning_efforts?.length) return null;
  return { levels: meta.reasoning_efforts, defaultLevel: meta.default_effort ?? null };
}

/** "Low", "Medium", "High", "Xhigh"... */
export function levelLabel(level: string): string {
  return capitalize(level);
}

/**
 * Whether a conversation's restored model can still be offered: one this
 * deployment lists, or the agent's own model (always sendable). Anything
 * else falls back to the agent's model rather than failing every message.
 */
export function modelUsable(model: string, agentModel: string | undefined, choices: readonly ModelChoice[]): boolean {
  if (!model || model === agentModel) return true;
  return choices.some((c) => c.value === model);
}

/** A level the newly picked model does not offer goes back to its default. */
export function levelForModel(res: ProvidersResponse | undefined, model: string, level: string): string {
  if (!level) return "";
  return reasoningLevels(res, model)?.levels.includes(level) ? level : "";
}
