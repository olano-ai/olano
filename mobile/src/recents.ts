/**
 * The "recent deployments" list on the connect screen.
 *
 * Kept in the local page's own localStorage (origin `https://localhost` on
 * Android, `capacitor://localhost` on iOS), never on a deployment's origin, so
 * one deployment's dashboard cannot read the addresses of the others. It is a
 * convenience: losing it costs a person one retyped address, so every read and
 * write tolerates a missing or broken store.
 */

import { hostOf } from "./address.ts";

export const RECENTS_KEY = "olano.recentDeployments";
export const MAX_RECENTS = 5;

export interface RecentDeployment {
  origin: string;
  host: string;
  /** Milliseconds since the epoch. */
  lastOpened: number;
}

type Store = Pick<Storage, "getItem" | "setItem">;

function defaultStore(): Store | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function loadRecents(store: Store | null = defaultStore()): RecentDeployment[] {
  if (!store) return [];
  let raw: unknown;
  try {
    raw = JSON.parse(store.getItem(RECENTS_KEY) ?? "[]");
  } catch {
    return [];
  }
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const out: RecentDeployment[] = [];
  for (const item of raw) {
    if (typeof item !== "object" || item === null) continue;
    const { origin, lastOpened } = item as Record<string, unknown>;
    if (typeof origin !== "string" || !/^https?:\/\//.test(origin) || seen.has(origin)) continue;
    seen.add(origin);
    out.push({
      origin,
      host: hostOf(origin),
      lastOpened: typeof lastOpened === "number" && Number.isFinite(lastOpened) ? lastOpened : 0,
    });
  }
  return out.sort((a, b) => b.lastOpened - a.lastOpened).slice(0, MAX_RECENTS);
}

function save(list: RecentDeployment[], store: Store | null): void {
  if (!store) return;
  try {
    store.setItem(RECENTS_KEY, JSON.stringify(list));
  } catch {
    // Quota or a disabled store: the list is a convenience, carry on.
  }
}

/** Puts `origin` first (opened now) and trims the list to MAX_RECENTS. */
export function rememberDeployment(
  origin: string,
  now: number = Date.now(),
  store: Store | null = defaultStore(),
): RecentDeployment[] {
  const rest = loadRecents(store).filter((item) => item.origin !== origin);
  const list = [{ origin, host: hostOf(origin), lastOpened: now }, ...rest].slice(0, MAX_RECENTS);
  save(list, store);
  return list;
}

export function forgetDeployment(origin: string, store: Store | null = defaultStore()): RecentDeployment[] {
  const list = loadRecents(store).filter((item) => item.origin !== origin);
  save(list, store);
  return list;
}

/** "just now", "5 minutes ago", "yesterday", "3 weeks ago". */
export function describeWhen(then: number, now: number = Date.now()): string {
  if (!then) return "";
  const seconds = Math.round((then - now) / 1000);
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const steps: [Intl.RelativeTimeFormatUnit, number][] = [
    ["second", 60],
    ["minute", 60],
    ["hour", 24],
    ["day", 7],
    ["week", 4.35],
    ["month", 12],
    ["year", Infinity],
  ];
  if (Math.abs(seconds) < 45) return "just now";
  let value = seconds;
  for (const [unit, size] of steps) {
    if (Math.abs(value) < size) return format.format(Math.round(value), unit);
    value /= size;
  }
  return "";
}
