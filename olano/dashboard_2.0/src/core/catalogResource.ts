// Shared store for the dashboard's read-mostly catalogs — archetypes,
// integrations, the MCP directory, tools/skills/providers. One implementation
// so every catalog gets the same guarantees, replacing three hand-rolled module
// singletons that each got a different subset of them right.
//
// The rules, each of which was a live bug in at least one of those copies:
//
//  1. A FAILED FETCH IS NEVER CACHED AS DATA. The old loaders did
//     `api.get(...).catch(() => ({}))` and wrote the resulting empty object
//     into the module cache, permanently — a single 401 during session
//     bootstrap, a 429 from the runtime rate limiter, or a request that
//     outlived a slow backend left every picker in the SPA reading "Nothing
//     available." until the user reloaded the page. Here an error keeps the
//     last good value (or the caller's fallback) and reports `status: "error"`.
//  2. LOADING IS OBSERVABLE. An empty list while in flight is not the same
//     thing as an empty catalog, and the UI must be able to tell them apart.
//  3. FAILURES RETRY, with bounded exponential backoff, instead of wedging
//     until a reload.
//  4. CACHE INVALIDATION IS SHARED. The `catalog_updated` SSE drops every
//     resource, so a background sync or an explicit refresh reaches all of them
//     rather than only the one loader that happened to subscribe.
//  5. ONE FETCH PER KEY. Concurrent mounts join the in-flight promise.

import { useCallback, useEffect, useSyncExternalStore } from "react";
import { subscribeStream } from "./agentStream";
import { api } from "./api";

export type CatalogStatus = "loading" | "ready" | "error";

export interface CatalogResource<T> {
  data: T;
  status: CatalogStatus;
  /** Detail from the last failed fetch — set only while `status === "error"`. */
  error: string | null;
  /** Force a refetch now (bypasses the TTL, resets any backoff). */
  refresh: () => void;
}

/** Fetcher signature: receives the typed GET so tests can inject a stub. */
export type CatalogFetcher<T> = (get: <R>(path: string) => Promise<R>) => Promise<T>;

/**
 * Immutable view handed to components. Its identity changes only when the
 * underlying state does, which is what `useSyncExternalStore` requires — a
 * freshly built object every read would loop forever.
 */
interface Snapshot<T> {
  data: T | null;
  status: CatalogStatus;
  error: string | null;
}

interface Entry<T> {
  data: T | null;
  error: string | null;
  fetchedAt: number;
  inflight: Promise<void> | null;
  /** Consecutive failures — drives the retry backoff. */
  failures: number;
  /** Earliest time a retry may be attempted, after a failure. */
  retryAfter: number;
  listeners: Set<() => void>;
  /** Memoized `Snapshot`; nulled by `touched()` on every state change. */
  snapshot: Snapshot<T> | null;
  /**
   * How to refetch this key, remembered from the first load — so a
   * `catalog_updated` SSE can refresh a mounted catalog without the SSE handler
   * having to know which fetcher belongs to which key.
   */
  fetcher: CatalogFetcher<T> | null;
  /** Pending auto-retry timer after a failure; at most one per key. */
  retryTimer: ReturnType<typeof setTimeout> | null;
}

const DEFAULT_TTL_MS = 5 * 60 * 1000;
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30_000;

const entries = new Map<string, Entry<unknown>>();

function entryFor<T>(key: string): Entry<T> {
  let e = entries.get(key) as Entry<T> | undefined;
  if (!e) {
    e = {
      data: null,
      error: null,
      fetchedAt: 0,
      inflight: null,
      failures: 0,
      retryAfter: 0,
      listeners: new Set(),
      snapshot: null,
      fetcher: null,
      retryTimer: null,
    };
    entries.set(key, e as Entry<unknown>);
  }
  return e;
}

function snapshotOf<T>(e: Entry<T>): Snapshot<T> {
  if (e.snapshot === null) {
    e.snapshot = {
      data: e.data,
      status: e.data !== null ? "ready" : e.error !== null ? "error" : "loading",
      // An error alongside usable data is a stale-read, not a failure to show:
      // the list stays up and the retry happens quietly in the background.
      error: e.data === null ? e.error : null,
    };
  }
  return e.snapshot;
}

/** Mark an entry changed: drop its memoized snapshot, then notify. */
function touched<T>(e: Entry<T>): void {
  e.snapshot = null;
  for (const l of [...e.listeners]) l();
}

function errText(err: unknown): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail);
  }
  return err instanceof Error ? err.message : String(err);
}

function load<T>(key: string, fetcher: CatalogFetcher<T>, force: boolean): Promise<void> {
  const e = entryFor<T>(key);
  e.fetcher = fetcher;
  if (e.inflight) return e.inflight;

  const now = Date.now();
  if (!force) {
    if (e.data !== null && now - e.fetchedAt < DEFAULT_TTL_MS) return Promise.resolve();
    // Back off after a failure so a dead endpoint isn't hammered by every
    // component that mounts — but keep the previous data readable throughout.
    if (e.failures > 0 && now < e.retryAfter) return Promise.resolve();
  }

  if (e.retryTimer) {
    clearTimeout(e.retryTimer);
    e.retryTimer = null;
  }

  const p = fetcher(api.get)
    .then((data) => {
      e.data = data;
      e.error = null;
      e.failures = 0;
      e.retryAfter = 0;
      e.fetchedAt = Date.now();
    })
    .catch((err: unknown) => {
      // Deliberately does NOT touch e.data: a failed read must never downgrade
      // a catalog that previously loaded, and must never masquerade as empty.
      e.error = errText(err);
      e.failures += 1;
      const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** (e.failures - 1));
      e.retryAfter = Date.now() + delay;
      // Auto-retry, but only while something is actually displaying this
      // catalog — a background key that nobody is watching waits for its next
      // mount instead of retrying into an empty room forever.
      if (e.listeners.size > 0) {
        e.retryTimer = setTimeout(() => {
          e.retryTimer = null;
          if (e.listeners.size > 0 && e.fetcher) void load(key, e.fetcher, true);
        }, delay);
      }
    })
    .finally(() => {
      e.inflight = null;
      touched(e);
    });

  e.inflight = p;
  return p;
}

/**
 * Mark every cached catalog stale and refetch the ones anything is watching.
 *
 * `data` is deliberately kept: an SSE saying the catalog changed is not a
 * reason to blank the UI while the new copy is in flight — the stale list stays
 * readable until the refetch lands. Unwatched keys are only marked, so a
 * background sync doesn't refetch catalogs nothing is displaying.
 */
export function invalidateAllCatalogs(): void {
  for (const [key, e] of entries) {
    e.fetchedAt = 0;
    e.failures = 0;
    e.retryAfter = 0;
    if (e.listeners.size > 0 && e.fetcher) void load(key, e.fetcher, true);
    else touched(e);
  }
}

/**
 * Warm a catalog without rendering it. Called from the app shell on idle so the
 * data is already in memory by the time a modal that needs it opens — the fetch
 * used to start only on that modal's first mount.
 */
export function prefetchCatalog<T>(key: string, fetcher: CatalogFetcher<T>): void {
  void load(key, fetcher, false);
}

/**
 * Run `task` once, during an idle slice, while `enabled`.
 *
 * Deliberately mirrors `useRouteChunkPrefetch`'s guards: waits for idle rather
 * than competing with the current view's own fetches, and skips entirely on
 * Save-Data / 2g — a speculative fetch must never spend metered bytes. Callers
 * pass a stable module-level function; `task` is not a dependency, so an inline
 * closure won't reschedule on every render.
 */
export function useIdlePrefetch(enabled: boolean, task: () => void): void {
  useEffect(() => {
    if (!enabled) return;
    const conn = (
      navigator as Navigator & { connection?: { saveData?: boolean; effectiveType?: string } }
    ).connection;
    if (conn?.saveData || /(^|-)2g$/.test(conn?.effectiveType || "")) return;

    let cancelled = false;
    const run = () => {
      if (!cancelled) task();
    };
    const idle = window.requestIdleCallback;
    if (typeof idle === "function") {
      const id = idle(run, { timeout: 3000 });
      return () => {
        cancelled = true;
        window.cancelIdleCallback?.(id);
      };
    }
    const timer = window.setTimeout(run, 1200);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);
}

// One `catalog_updated` subscription shared by every mounted catalog consumer,
// ref-counted so it is released when the last one unmounts (subscribeStream
// holds the SSE connection open while subscribed — a module-level subscription
// that never unsubscribed would pin it for the lifetime of the page).
//
// Missing an event while nothing is mounted costs nothing worse than the normal
// TTL: the next mount refetches once its cache expires.
let sseRefs = 0;
let sseOff: (() => void) | null = null;

function bindSse(): () => void {
  if (++sseRefs === 1) {
    sseOff = subscribeStream((frame) => {
      if (frame.type === "catalog_updated") invalidateAllCatalogs();
    });
  }
  return () => {
    if (--sseRefs === 0) {
      sseOff?.();
      sseOff = null;
    }
  };
}

/**
 * Subscribe a component to a shared catalog.
 *
 * `fallback` is what `data` reads as before anything has ever loaded — pass the
 * empty shape, or a bundled snapshot when one exists. It is NOT written into
 * the cache, so a later successful fetch always replaces it.
 */
export function useCatalogResource<T>(
  key: string,
  fetcher: CatalogFetcher<T>,
  fallback: T,
): CatalogResource<T> {
  // useSyncExternalStore rather than useState + a listener: the store is
  // module-level and shared, so a value landing between render and the
  // subscribe effect would otherwise be missed and the component would sit on
  // the fallback forever.
  const subscribe = useCallback(
    (onChange: () => void) => {
      const e = entryFor<T>(key);
      e.listeners.add(onChange);
      return () => {
        e.listeners.delete(onChange);
      };
    },
    [key],
  );
  const snap = useSyncExternalStore(subscribe, () => snapshotOf(entryFor<T>(key)));

  useEffect(() => {
    const releaseSse = bindSse();
    void load(key, fetcher, false);
    return releaseSse;
    // `fetcher` is a stable module-level function per key; depending on its
    // identity would re-fetch every render for callers passing a closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const refresh = useCallback(() => {
    void load(key, fetcher, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return { data: snap.data ?? fallback, status: snap.status, error: snap.error, refresh };
}
