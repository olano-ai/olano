/**
 * The last good copy of the list screens' data, kept on the phone so a cold
 * launch shows them at once (and tapping an agent opens its last conversation
 * at once) while the refresh runs, the way a messaging app opens on its own
 * local copy instead of a spinner.
 *
 * Only the lists persist: the agents and each agent's threads, never a
 * transcript. A thread list is trimmed to its newest rows (plus the newest
 * conversation, which is what tapping the agent opens) and loses its live
 * "running" dot, which only the deployment can vouch for. Every copy is filed
 * under the deployment and the person it belongs to, and signing out or
 * switching deployment drops all of them. localStorage can be full, missing or
 * broken, so every read and write tolerates that: losing the copy costs a
 * spinner, nothing else.
 *
 * Pure: no DOM beyond the store handed in, so `node --test` covers it.
 */

import { isConversation, lastConversation } from "../chat/threadPick.ts";
import type { ThreadSummary } from "../net/types.ts";
import { newestFirst } from "../shared/dashboard.ts";

export const CACHE_PREFIX = "olano.cache.v1:";
/** Thread rows kept per agent, newest first. */
export const MAX_PERSISTED_THREADS = 60;

export type CacheStore = Pick<Storage, "getItem" | "setItem" | "removeItem" | "key" | "length">;

export interface Cached<T> {
  data: T;
  loadedAt: number;
}

/** Whether a resource key is one of the lists kept across launches. */
export function persistable(key: string): boolean {
  return key === "agents" || (key.startsWith("threads:") && key.length > "threads:".length);
}

/** The rows of a thread list worth keeping: the newest, and the conversation tapping the agent opens. */
export function slimThreads(rows: readonly ThreadSummary[]): ThreadSummary[] {
  const sorted = newestFirst(rows);
  const kept = sorted.slice(0, MAX_PERSISTED_THREADS);
  if (!kept.some(isConversation)) {
    const latest = lastConversation(sorted);
    if (latest) kept.push(latest);
  }
  return kept.map(({ running: _running, ...row }) => row);
}

function slim(key: string, data: unknown): unknown {
  if (!key.startsWith("threads:")) return data;
  if (Array.isArray(data)) return slimThreads(data as ThreadSummary[]);
  // A page (chat/threadPages.ts): its rows trimmed the same way, the cursor kept.
  if (data && typeof data === "object" && Array.isArray((data as { threads?: unknown }).threads)) {
    const page = data as { threads: ThreadSummary[] };
    return { ...page, threads: slimThreads(page.threads) };
  }
  return data;
}

function itemKey(scope: string, key: string): string {
  return `${CACHE_PREFIX}${scope}\u0000${key}`;
}

export function readCached<T>(store: CacheStore | null, scope: string, key: string): Cached<T> | null {
  if (!store || !scope || !persistable(key)) return null;
  try {
    const raw = JSON.parse(store.getItem(itemKey(scope, key)) ?? "null") as unknown;
    if (!raw || typeof raw !== "object") return null;
    const { data, loadedAt } = raw as { data?: unknown; loadedAt?: unknown };
    if (data === undefined || typeof loadedAt !== "number" || !Number.isFinite(loadedAt)) return null;
    return { data: data as T, loadedAt };
  } catch {
    return null;
  }
}

export function writeCached(store: CacheStore | null, scope: string, key: string, data: unknown, loadedAt: number): void {
  if (!store || !scope || !persistable(key) || data === undefined) return;
  try {
    store.setItem(itemKey(scope, key), JSON.stringify({ data: slim(key, data), loadedAt }));
  } catch {
    // Full or unavailable: the next launch shows a spinner instead.
  }
}

/** Drops every kept copy, whoever it belonged to (sign out, switch deployment). */
export function clearCached(store: CacheStore | null): void {
  if (!store) return;
  try {
    const doomed: string[] = [];
    for (let i = 0; i < store.length; i += 1) {
      const name = store.key(i);
      if (name?.startsWith(CACHE_PREFIX)) doomed.push(name);
    }
    for (const name of doomed) store.removeItem(name);
  } catch {
    // Nothing more can be done about a store that will not answer.
  }
}

export function defaultCacheStore(): CacheStore | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}
