/**
 * A minimal external store for React's useSyncExternalStore: one value,
 * replaced (never mutated) on every change, so a component re-renders exactly
 * when what it reads changed.
 */

import { useSyncExternalStore } from "react";

export interface Store<T> {
  get(): T;
  set(next: T | ((prev: T) => T)): void;
  subscribe(listener: () => void): () => void;
}

export function createStore<T>(initial: T): Store<T> {
  let value = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => value,
    set(next) {
      const resolved = typeof next === "function" ? (next as (prev: T) => T)(value) : next;
      if (Object.is(resolved, value)) return;
      value = resolved;
      for (const listener of [...listeners]) listener();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export function useStore<T>(store: Store<T>): T {
  return useSyncExternalStore(store.subscribe, store.get, store.get);
}

/**
 * A cached read of one API resource: what was last fetched stays on screen
 * while a refresh runs, and an error never replaces data that was good.
 */
export interface Resource<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  /** Milliseconds since the epoch of the last successful load, 0 before it. */
  loadedAt: number;
}

export function emptyResource<T>(): Resource<T> {
  return { data: undefined, error: null, loading: false, loadedAt: 0 };
}
