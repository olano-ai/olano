/**
 * The app's remote configuration: a small public JSON document Olano Cloud
 * serves (`GET https://cloud.olano.ai/api/v1/mobile/config`), read at launch.
 *
 * It exists so the "Try the demo" entry on the connect screen can be turned
 * off, or pointed at another demo deployment, without an app update. It can
 * only take things away or say where the demo lives: nothing in it switches on
 * a feature the reviewed build does not show (App Review Guideline 2.3.1), so
 * the schema carries no feature flags, only:
 *
 * - `demo`: whether the connect screen offers the demo, the demo deployment's
 *   address (an https origin under olano.run or olano.ai, checked here), the
 *   button's label and one sentence shown under it;
 * - `minSupportedVersion`: the oldest app version Olano still supports; an
 *   older app shows a notice asking to update (and keeps working).
 *
 * The app never waits for it: the screens draw from the last good copy kept
 * on the phone, or from `BUNDLED_CONFIG` before the first fetch, and a fetch
 * runs at most once every `FRESH_MS`. A failed, slow or malformed answer
 * changes nothing. mobile/store/DEMO.md is the procedure around it.
 *
 * Pure apart from the injectable `fetchText`, so `node --test` covers it.
 */

export const APP_CONFIG_URL = "https://cloud.olano.ai/api/v1/mobile/config";
export const APP_CONFIG_KEY = "olano.appConfig";
/** A fetch younger than this is not repeated at the next launch. */
export const FRESH_MS = 10 * 60 * 1000;
export const FETCH_TIMEOUT_MS = 4000;

/** The demo deployment may only live on Olano's own domains. */
const DEMO_DOMAINS = ["olano.run", "olano.ai"];
const MAX_LABEL = 40;
const MAX_HINT = 280;

export interface DemoConfig {
  enabled: boolean;
  /** The demo deployment's origin, e.g. "https://demo.olano.run". */
  url: string;
  /** The button's text on the connect screen. */
  label: string;
  /** One sentence under the button: what the demo is and whose account it uses. */
  accountHint: string;
}

export interface AppConfig {
  demo: DemoConfig;
  /** "1.2.0", or null when every version is supported. */
  minSupportedVersion: string | null;
}

export const BUNDLED_CONFIG: AppConfig = {
  demo: {
    enabled: true,
    url: "https://demo.olano.run",
    label: "Try the demo",
    accountHint:
      "Opens an Olano demo with sample agents, under a demo account of your own. The demo is public, so type nothing private; your demo account and its conversations are deleted after a day.",
  },
  minSupportedVersion: null,
};

type Store = Pick<Storage, "getItem" | "setItem">;

interface Cached {
  config: AppConfig;
  /** Milliseconds since the epoch. */
  fetchedAt: number;
}

/** The demo address if it is an https origin on one of Olano's domains, else null. */
export function demoOrigin(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username || url.password) return null;
  if ((url.pathname !== "/" && url.pathname !== "") || url.search || url.hash) return null;
  const host = url.hostname.toLowerCase();
  if (!DEMO_DOMAINS.some((domain) => host === domain || host.endsWith(`.${domain}`))) return null;
  return url.origin;
}

function text(raw: unknown, max: number): string | null {
  if (typeof raw !== "string") return null;
  const value = raw.replace(/\s+/g, " ").trim();
  if (!value || value.length > max) return null;
  return value;
}

const VERSION = /^\d+(\.\d+){0,3}$/;

/**
 * A served document as an AppConfig, or null when it is not one. Fields the
 * app does not know are ignored; a field present but wrong turns the demo off
 * rather than guessing (an unusable entry is worse than none).
 */
export function parseAppConfig(raw: unknown): AppConfig | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const demoRaw = record.demo;
  let demo: DemoConfig = { ...BUNDLED_CONFIG.demo, enabled: false };
  if (typeof demoRaw === "object" && demoRaw !== null && !Array.isArray(demoRaw)) {
    const d = demoRaw as Record<string, unknown>;
    const url = demoOrigin(d.url);
    const label = d.label === undefined ? BUNDLED_CONFIG.demo.label : text(d.label, MAX_LABEL);
    const accountHint = d.accountHint === undefined ? BUNDLED_CONFIG.demo.accountHint : text(d.accountHint, MAX_HINT);
    const usable = d.enabled === true && url !== null && label !== null && accountHint !== null;
    demo = {
      enabled: usable,
      url: url ?? BUNDLED_CONFIG.demo.url,
      label: label ?? BUNDLED_CONFIG.demo.label,
      accountHint: accountHint ?? BUNDLED_CONFIG.demo.accountHint,
    };
  } else if (demoRaw !== undefined && demoRaw !== null) {
    return null;
  }
  const min = record.minSupportedVersion;
  const minSupportedVersion = typeof min === "string" && VERSION.test(min.trim()) ? min.trim() : null;
  return { demo, minSupportedVersion };
}

function defaultStore(): Store | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

function readCached(store: Store | null): Cached | null {
  if (!store) return null;
  try {
    const raw = JSON.parse(store.getItem(APP_CONFIG_KEY) ?? "null") as { config?: unknown; fetchedAt?: unknown } | null;
    if (!raw || typeof raw.fetchedAt !== "number" || !Number.isFinite(raw.fetchedAt)) return null;
    const config = parseAppConfig(raw.config);
    return config ? { config, fetchedAt: raw.fetchedAt } : null;
  } catch {
    return null;
  }
}

/** What the screens draw from right now: the last good copy, else the bundled one. */
export function currentAppConfig(store: Store | null = defaultStore()): AppConfig {
  return readCached(store)?.config ?? BUNDLED_CONFIG;
}

export interface RefreshOptions {
  /** GETs the URL and resolves its status and body; rejects on a network failure or timeout. */
  fetchText: (url: string, timeoutMs: number) => Promise<{ status: number; body: string }>;
  store?: Store | null;
  now?: number;
  /** Ignore FRESH_MS (a test, or a person pulling to refresh). */
  force?: boolean;
}

/**
 * Fetches the config when the kept copy is older than FRESH_MS, keeps a good
 * answer, and resolves what the screens should draw. Never rejects.
 */
export async function refreshAppConfig(options: RefreshOptions): Promise<AppConfig> {
  const store = options.store === undefined ? defaultStore() : options.store;
  const now = options.now ?? Date.now();
  const cached = readCached(store);
  if (cached && !options.force && now - cached.fetchedAt >= 0 && now - cached.fetchedAt < FRESH_MS) {
    return cached.config;
  }
  let config: AppConfig | null = null;
  try {
    const response = await options.fetchText(APP_CONFIG_URL, FETCH_TIMEOUT_MS);
    if (response.status >= 200 && response.status < 300) config = parseAppConfig(JSON.parse(response.body));
  } catch {
    config = null;
  }
  if (!config) return cached?.config ?? BUNDLED_CONFIG;
  if (store) {
    try {
      store.setItem(APP_CONFIG_KEY, JSON.stringify({ config, fetchedAt: now } satisfies Cached));
    } catch {
      // A full or disabled store: the answer still applies to this launch.
    }
  }
  return config;
}

/** Compares dotted numeric versions: negative when a < b, 0 when equal, positive when a > b. */
export function compareVersions(a: string, b: string): number {
  const pa = a.split(".").map((part) => Number.parseInt(part, 10) || 0);
  const pb = b.split(".").map((part) => Number.parseInt(part, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

/** True when this app is older than the oldest version Olano still supports. */
export function needsUpdate(appVersion: string, config: AppConfig): boolean {
  if (!config.minSupportedVersion || !VERSION.test(appVersion)) return false;
  return compareVersions(appVersion, config.minSupportedVersion) < 0;
}
