// The decision layer behind the Files view's network search — pure, React-free,
// unit-tested (`npm run test:unit`). The hook (`useWorkspaceSearch.ts`) owns
// the timer, the fetch and the state; everything that decides WHETHER a request
// should go out lives here, because those rules are what keep a type-ahead box
// from hitting the server once per character:
//
// * normalise first — whitespace and case changes are not new queries;
// * a minimum length — one character matches everything and helps nobody;
// * never repeat a query, and serve an earlier one from a small cache;
// * refine locally when the new query merely extends a query whose result was
//   complete (not truncated): the matches are a subset, so filtering the hits
//   already in hand is exact and costs no request;
// * drop a late response for a query that is no longer the latest.
//
// The debounce interval itself is a constant here so the hook and its test
// agree on it.

import type { SearchHit } from "./tree";

/** Wait this long after the last keystroke before asking the server. Long
 *  enough that typing a word at normal speed produces one request, short
 *  enough that the pause is not felt. (The instant client-side narrowing of
 *  loaded rows runs on its own, shorter debounce and is unaffected.) */
export const SEARCH_DEBOUNCE_MS = 350;
/** Below this the server refuses the query and the box only filters locally. */
export const SEARCH_MIN_LENGTH = 2;
/** Earlier results kept per session so revisiting a query is instant. */
export const SEARCH_CACHE_SIZE = 50;

export interface SearchResult {
  hits: SearchHit[];
  /** The server stopped early (result cap or time budget): the hits are a
   *  sample, so a longer query must be re-asked rather than refined locally. */
  truncated: boolean;
}

/** The last query that completed, with its result — what refinement builds on. */
export interface LastSearch {
  q: string;
  hidden: boolean;
  result: SearchResult;
}

export type SearchDecision =
  | { kind: "none" } // too short: clear network hits, local filter only
  | { kind: "skip" } // identical to what is already shown
  | { kind: "local"; result: SearchResult } // answered from cache or by refinement
  | { kind: "request" }; // ask the server

export function normalizeQuery(raw: string): string {
  return raw.trim().toLowerCase();
}

/** Narrow a complete result to a longer query without a request. Exact because
 *  a match for `q` is a match for every prefix of `q`, so the superset is the
 *  earlier result. */
export function refineHits(hits: readonly SearchHit[], q: string): SearchHit[] {
  return hits.filter((h) => h.name.toLowerCase().includes(q) || h.path.toLowerCase().includes(q));
}

/** A small LRU keyed by `hidden:q`. Whole-session memory of what was asked. */
export class SearchCache {
  private readonly map = new Map<string, SearchResult>();
  private readonly max: number;
  constructor(max: number = SEARCH_CACHE_SIZE) {
    this.max = max;
  }

  static key(q: string, hidden: boolean): string {
    return `${hidden ? "1" : "0"}:${q}`;
  }

  get(q: string, hidden: boolean): SearchResult | undefined {
    const k = SearchCache.key(q, hidden);
    const hit = this.map.get(k);
    if (hit) {
      // Re-insert to mark as most recently used.
      this.map.delete(k);
      this.map.set(k, hit);
    }
    return hit;
  }

  set(q: string, hidden: boolean, result: SearchResult): void {
    const k = SearchCache.key(q, hidden);
    this.map.delete(k);
    this.map.set(k, result);
    while (this.map.size > this.max) {
      const oldest = this.map.keys().next().value;
      if (oldest === undefined) break;
      this.map.delete(oldest);
    }
  }

  clear(): void {
    this.map.clear();
  }

  get size(): number {
    return this.map.size;
  }
}

export function decideSearch(input: {
  /** Already normalised. */
  q: string;
  hidden: boolean;
  /** What is currently shown (the last completed search), if anything. */
  last: LastSearch | null;
  cache: SearchCache;
}): SearchDecision {
  const { q, hidden, last, cache } = input;
  if (q.length < SEARCH_MIN_LENGTH) return { kind: "none" };
  if (last && last.q === q && last.hidden === hidden) return { kind: "skip" };
  const cached = cache.get(q, hidden);
  if (cached) return { kind: "local", result: cached };
  if (last && last.hidden === hidden && !last.result.truncated && q.startsWith(last.q)) {
    // The earlier result was complete, so this longer query's matches are
    // inside it. (An empty complete result makes every extension empty too.)
    const result: SearchResult = { hits: refineHits(last.result.hits, q), truncated: false };
    cache.set(q, hidden, result);
    return { kind: "local", result };
  }
  return { kind: "request" };
}

/** A response belongs to the latest request only when its id is the latest
 *  issued; anything older is a stale answer for a query no longer on screen. */
export function isLatest(responseId: number, latestId: number): boolean {
  return responseId === latestId;
}
