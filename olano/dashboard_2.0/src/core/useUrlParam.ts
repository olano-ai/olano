import { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { usePersistentState } from "./usePersistentState";

// Query-string state that is read on EVERY change, never only on mount, and
// written without clobbering anything else in the URL.
//
// Why not useSearchParams' own setter: it builds the next query from the params
// of the render that created it, so two writers in one tick (a view's reflect
// effect, the shell's `?focus=` consumer) silently undo each other. These
// writers start from the live address instead (history.pushState is
// synchronous, so window.location is always the latest URL), and every key not
// named in a patch rides along untouched.
//
// History: "push" for a move (opening an item, switching a sub-tab), so Back
// returns to where you were; "replace" (the default) for refinements (filters,
// search text, tidying the URL), which must not become Back steps.

export type UrlHistory = "push" | "replace";

export type UrlPatch = Record<string, string | null | undefined>;

/** Apply `patch` to the live query string; null/undefined/"" removes a key. */
export function useUrlUpdate(): (patch: UrlPatch, opts?: { history?: UrlHistory }) => void {
  const navigate = useNavigate();
  return useCallback(
    (patch: UrlPatch, opts?: { history?: UrlHistory }) => {
      const params = new URLSearchParams(window.location.search);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === undefined || v === "") params.delete(k);
        else params.set(k, v);
      }
      const search = params.toString();
      if (`?${search}` === window.location.search || (!search && !window.location.search)) return;
      navigate(
        { pathname: window.location.pathname, search: search ? `?${search}` : "", hash: window.location.hash },
        { replace: (opts?.history ?? "replace") === "replace" },
      );
    },
    [navigate],
  );
}

/**
 * navigate(), except that going to the URL already on screen replaces instead
 * of pushing. That is what <Link> does by itself, and what a button-driven move must
 * do too, or clicking "New chat" twice leaves two identical Back steps.
 */
export function useGo(): (to: string, opts?: { history?: UrlHistory }) => void {
  const navigate = useNavigate();
  return useCallback(
    (to: string, opts?: { history?: UrlHistory }) => {
      const here = window.location.pathname + window.location.search + window.location.hash;
      const same = new URL(to, window.location.href);
      const replace = opts?.history === "replace" || same.pathname + same.search + same.hash === here;
      navigate(to, { replace });
    },
    [navigate],
  );
}

/** One query parameter as two-way state: [value or null, setter]. */
export function useUrlParam(
  key: string,
  defaults?: { history?: UrlHistory },
): [string | null, (value: string | null, opts?: { history?: UrlHistory }) => void] {
  const [params] = useSearchParams();
  const update = useUrlUpdate();
  const value = params.get(key);
  const set = useCallback(
    (next: string | null, opts?: { history?: UrlHistory }) =>
      update({ [key]: next }, { history: opts?.history ?? defaults?.history }),
    [update, key, defaults?.history],
  );
  return [value, set];
}

/**
 * useUrlParam, plus a session memory of the last value: the URL decides, but a
 * bare arrival (the sidebar link) shows the remembered value and fills it into
 * the address (replace: same place). This is how a view that already kept its
 * open item across a left-nav round-trip keeps doing so once the item is in
 * the URL.
 */
export function useRememberedUrlParam(
  key: string,
  rememberKey: string,
  defaults?: { history?: UrlHistory },
): [string | null, (value: string | null, opts?: { history?: UrlHistory }) => void] {
  const [value, set] = useUrlParam(key, defaults);
  const [remembered, setRemembered] = usePersistentState<string | null>(rememberKey, null);
  const [arrived, setArrived] = useState(false);
  useEffect(() => {
    if (arrived) return;
    setArrived(true);
    if (value === null && remembered) set(remembered, { history: "replace" });
  }, [arrived, value, remembered, set]);
  useEffect(() => {
    if (arrived && value !== remembered) setRemembered(value);
  }, [arrived, value, remembered, setRemembered]);
  return [arrived ? value : value ?? remembered, set];
}
