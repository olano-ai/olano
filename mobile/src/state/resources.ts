/**
 * Cached API reads for the list screens (agents, threads, Cortex, inbox).
 *
 * A screen asks for a key and a loader. What was loaded last stays on screen
 * while a refresh runs; a failed refresh keeps it and shows the error beside
 * it. Live events from the deployment (`invalidate`) refresh only the keys a
 * screen is showing, so a quiet app makes no requests.
 *
 * The lists (`persisted.ts`: the agents and their threads) also start from the
 * copy the last launch kept, so a cold start shows them, and tapping an agent
 * opens its last conversation, before the deployment has answered.
 */

import { useCallback, useEffect, useSyncExternalStore } from "react";

import { messageOf } from "../native/shell.ts";
import { defaultCacheStore, readCached, writeCached } from "./persisted.ts";
import { session } from "./session.ts";
import { emptyResource, type Resource } from "./store.ts";

interface Entry<T> {
  value: Resource<T>;
  loader: () => Promise<T>;
  inflight: Promise<void> | null;
  /**
   * A refresh or a local write arrived while a request was in flight. That
   * request started before whatever prompted it (a turn's first message, a
   * conversation that just got its thread id), so its answer is not shown and
   * one more request runs when it lands. Joining it instead is how a
   * conversation started on the phone could be missing from the agent's row
   * and from its conversations until the turn ended.
   */
  again: boolean;
  listeners: Set<() => void>;
}

const entries = new Map<string, Entry<unknown>>();

/** Whose kept copies these are: the deployment and the person signed in to it. */
function cacheScope(): string {
  const { origin, me } = session.get();
  return origin && me ? `${origin}|${me.user.id}` : "";
}

function seeded<T>(key: string): Resource<T> {
  const cached = readCached<T>(defaultCacheStore(), cacheScope(), key);
  return cached ? { data: cached.data, error: null, loading: false, loadedAt: cached.loadedAt } : emptyResource<T>();
}

function keep(key: string, value: Resource<unknown>): void {
  if (value.loadedAt > 0) writeCached(defaultCacheStore(), cacheScope(), key, value.data, value.loadedAt);
}

function entry<T>(key: string, loader: () => Promise<T>): Entry<T> {
  let e = entries.get(key) as Entry<T> | undefined;
  if (!e) {
    e = { value: seeded<T>(key), loader, inflight: null, again: false, listeners: new Set() };
    entries.set(key, e as Entry<unknown>);
  }
  e.loader = loader;
  return e;
}

function emit(e: Entry<unknown>): void {
  for (const listener of [...e.listeners]) listener();
}

export function refresh(key: string): Promise<void> {
  const e = entries.get(key);
  if (!e) return Promise.resolve();
  if (e.inflight) {
    e.again = true;
    return e.inflight;
  }
  e.again = false;
  e.value = { ...e.value, loading: true };
  emit(e);
  e.inflight = e
    .loader()
    .then((data) => {
      // Overtaken (see `Entry.again`): the next request's answer is shown instead.
      if (e.again) return;
      e.value = { data, error: null, loading: false, loadedAt: Date.now() };
      keep(key, e.value);
    })
    .catch((error: unknown) => {
      e.value = { ...e.value, error: messageOf(error, "Could not load this."), loading: false };
    })
    .finally(() => {
      e.inflight = null;
      if (e.again) {
        void refresh(key);
        return;
      }
      emit(e);
    });
  return e.inflight;
}

/**
 * Refreshes every watched key that starts with `prefix`, and marks the others
 * stale so the screen that shows one next loads it again instead of trusting
 * a copy younger than its stale time. Only watched keys are requested (a quiet
 * app makes no requests); the unwatched ones were skipped outright before,
 * which left the conversations screen and a folded agent row a whole turn
 * behind after the person left a conversation mid-reply.
 */
export function invalidate(prefix: string): void {
  for (const [key, e] of entries) {
    if (!key.startsWith(prefix)) continue;
    if (e.listeners.size > 0) void refresh(key);
    else e.value = { ...e.value, loadedAt: 0 };
  }
}

/**
 * Marks every key that starts with `prefix` stale without requesting any: the
 * screen that shows one next loads it again. For a change the deployment may
 * not have on disk yet, where a request made now could answer from before it.
 */
export function markStale(prefix: string): void {
  for (const [key, e] of entries) {
    if (key.startsWith(prefix)) e.value = { ...e.value, loadedAt: 0 };
  }
}

const pending = new Map<string, ReturnType<typeof setTimeout>>();

/**
 * `invalidate`, once `delayMs` after the last call for `prefix`: one turn
 * announces itself several times within seconds, and each refresh of a thread
 * list asks the deployment to look at every thread the agent has.
 */
export function invalidateSoon(prefix: string, delayMs = 800): void {
  const timer = pending.get(prefix);
  if (timer !== undefined) clearTimeout(timer);
  pending.set(
    prefix,
    setTimeout(() => {
      pending.delete(prefix);
      invalidate(prefix);
    }, delayMs),
  );
}

/** Drops everything held in memory (sign out, switch deployment). */
export function resetResources(): void {
  for (const timer of pending.values()) clearTimeout(timer);
  pending.clear();
  entries.clear();
}

/**
 * Writes a value without a request (an optimistic update, or a list a sibling
 * call returned). A request already in flight started before this write, so
 * its answer is dropped and one more request follows (see `Entry.again`).
 */
export function patchResource<T>(key: string, update: (prev: T | undefined) => T): void {
  const e = entries.get(key) as Entry<T> | undefined;
  if (!e) return;
  if (e.inflight) e.again = true;
  e.value = { ...e.value, data: update(e.value.data) };
  keep(key, e.value);
  emit(e as Entry<unknown>);
}

/**
 * Reads `key` with `loader`, loading it when first shown and again when it is
 * older than `staleMs`. Returns the resource and a `reload` for pull to refresh.
 */
export function useResource<T>(key: string | null, loader: () => Promise<T>, staleMs = 30000): [Resource<T>, () => Promise<void>] {
  const e = key ? entry(key, loader) : null;
  const subscribe = useCallback(
    (listener: () => void) => {
      if (!e) return () => undefined;
      e.listeners.add(listener as () => void);
      return () => e.listeners.delete(listener as () => void);
    },
    [e],
  );
  const value = useSyncExternalStore(
    subscribe,
    () => (e ? e.value : (EMPTY as Resource<T>)),
    () => (e ? e.value : (EMPTY as Resource<T>)),
  );
  useEffect(() => {
    if (!key || !e) return;
    if (!e.inflight && (e.value.loadedAt === 0 || Date.now() - e.value.loadedAt > staleMs)) void refresh(key);
  }, [key, e, staleMs]);
  const reload = useCallback(() => (key ? refresh(key) : Promise.resolve()), [key]);
  return [value, reload];
}

const EMPTY = emptyResource<unknown>();
