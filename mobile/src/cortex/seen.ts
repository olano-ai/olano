/**
 * Which Cortex items this phone has opened. The deployment keeps no record of
 * who read what, so the list's unread tint is the phone's own memory: an item
 * is read once its page has been opened here. Bounded, newest last, kept in
 * localStorage like the theme.
 */

import { createStore, useStore } from "../state/store.ts";

const KEY = "olano.cortex.seen";
/** Enough for a year of a busy fleet; past it the oldest keys fall off. */
export const SEEN_LIMIT = 2000;

/** `keys` with `key` moved to the end, trimmed to `limit`. Pure. */
export function remember(keys: readonly string[], key: string, limit = SEEN_LIMIT): string[] {
  const next = keys.filter((k) => k !== key);
  next.push(key);
  return next.length > limit ? next.slice(next.length - limit) : next;
}

function read(): string[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === "string") : [];
  } catch {
    return [];
  }
}

const seenKeys = createStore<readonly string[]>(read());
const seenSet = createStore<ReadonlySet<string>>(new Set(seenKeys.get()));
seenKeys.subscribe(() => seenSet.set(new Set(seenKeys.get())));

/** Records that `key` (an `itemKey`) has been opened on this phone. */
export function markSeen(key: string): void {
  if (seenSet.get().has(key)) return;
  seenKeys.set((prev) => remember(prev, key));
  try {
    localStorage.setItem(KEY, JSON.stringify(seenKeys.get()));
  } catch {
    // Not remembered past this session; the tint still clears now.
  }
}

/** The keys opened on this phone, as a set for the list to test against. */
export function useSeen(): ReadonlySet<string> {
  return useStore(seenSet);
}
