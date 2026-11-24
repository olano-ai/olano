// The Files view's network search: the side-effect half of `search.ts`.
//
// `search.ts` decides whether a query deserves a request; this hook performs
// what it decided — debounced, cancellable, stale-proof — and exposes the hits
// for `flattenTree`. The rules that matter (why a keystroke is not a request)
// are documented and tested in search.ts; here they only get a timer, an
// AbortController and a request counter.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import {
  SEARCH_DEBOUNCE_MS,
  SearchCache,
  decideSearch,
  isLatest,
  normalizeQuery,
  type LastSearch,
  type SearchResult,
} from "./search";
import type { SearchHit } from "./tree";

interface SearchResponse {
  results: SearchHit[];
  truncated: boolean;
  agents_searched: string[];
}

export interface WorkspaceSearch {
  /** Server matches for the current query; empty when it is too short. */
  hits: SearchHit[];
  /** A request is in flight. Previous hits stay on screen meanwhile. */
  searching: boolean;
  /** The server stopped early — the hits are the first N, not all. */
  truncated: boolean;
  /** Search the current query now, skipping the debounce (Enter). */
  flush: () => void;
}

export function useWorkspaceSearch(rawQuery: string, hidden: boolean): WorkspaceSearch {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const cacheRef = useRef(new SearchCache());
  const lastRef = useRef<LastSearch | null>(null);
  const timerRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);
  const latestArgsRef = useRef({ q: normalizeQuery(rawQuery), hidden });
  latestArgsRef.current = { q: normalizeQuery(rawQuery), hidden };

  const show = useCallback((q: string, hid: boolean, result: SearchResult) => {
    lastRef.current = { q, hidden: hid, result };
    setHits(result.hits);
    setTruncated(result.truncated);
  }, []);

  const run = useCallback(() => {
    const { q, hidden: hid } = latestArgsRef.current;
    const decision = decideSearch({ q, hidden: hid, last: lastRef.current, cache: cacheRef.current });
    if (decision.kind === "none") {
      // Too short: drop whatever the network had found. The in-flight request
      // (if any) is for a query no longer wanted.
      abortRef.current?.abort();
      abortRef.current = null;
      requestIdRef.current += 1;
      lastRef.current = null;
      setHits([]);
      setTruncated(false);
      setSearching(false);
      return;
    }
    if (decision.kind === "skip") return;
    if (decision.kind === "local") {
      abortRef.current?.abort();
      abortRef.current = null;
      requestIdRef.current += 1;
      setSearching(false);
      show(q, hid, decision.result);
      return;
    }
    // A new request supersedes whatever is still in flight.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const id = ++requestIdRef.current;
    setSearching(true);
    const params = new URLSearchParams({ q });
    if (hid) params.set("hidden", "true");
    api
      .get<SearchResponse>(`/api/files/search?${params.toString()}`, { signal: controller.signal })
      .then((res) => {
        if (!isLatest(id, requestIdRef.current)) return; // a newer query took over
        const result: SearchResult = { hits: res.results, truncated: res.truncated };
        cacheRef.current.set(q, hid, result);
        show(q, hid, result);
      })
      .catch((err: unknown) => {
        if (!isLatest(id, requestIdRef.current)) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        // A failed search leaves the previous hits in place: the local filter
        // still narrows what is loaded, and the next keystroke retries.
      })
      .finally(() => {
        if (isLatest(id, requestIdRef.current)) setSearching(false);
      });
  }, [show]);

  // Debounce: one request per pause in typing, not one per keystroke.
  useEffect(() => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      run();
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [rawQuery, hidden, run]);

  const flush = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    run();
  }, [run]);

  // What the server knows changes when files change: forget cached answers so
  // the next query asks again. The tree already consumes this frame for its
  // own listings; the cache is the only extra state here.
  useEffect(
    () =>
      subscribeStream((frame) => {
        if ((frame as { type?: string }).type === "file_updated") cacheRef.current.clear();
      }),
    [],
  );

  // Leaving the view abandons an in-flight request.
  useEffect(() => () => abortRef.current?.abort(), []);

  return { hits, searching, truncated, flush };
}
