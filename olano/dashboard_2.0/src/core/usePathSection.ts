import { useCallback, useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { usePersistentState } from "./usePersistentState";
import { parseRoute } from "./routeUrls";

/**
 * A view's sub-tab as a path segment (/knowledge/<agent>/sources,
 * /cortex/<agent>/ideas, /calendar/week, …), two-way, so Back/Forward and a
 * reload land on it and a tab click is a Back step.
 *
 * A bare address (the sidebar link, a canonicalised /cortex) still opens the
 * tab last used this session, exactly as before the tab was in the URL, and
 * the address is then filled in (replace) so it says which tab is showing. The
 * default tab is the bare path, so the everyday URL stays short.
 *
 * `pathFor(section)` builds this view's path for a section (null = the
 * default); returning null means "no canonical path yet" (no agent known),
 * which leaves the URL alone.
 */
export function usePathSection<T extends string>(opts: {
  allowed: readonly T[];
  fallback: T;
  rememberKey: string;
  pathFor: (section: T | null) => string | null;
}): [T, (next: T) => void] {
  const { allowed, fallback, rememberKey, pathFor } = opts;
  const location = useLocation();
  const navigate = useNavigate();
  const [remembered, setRemembered] = usePersistentState<T>(rememberKey, fallback);
  const raw = parseRoute(location.pathname).section;
  const fromUrl = raw && (allowed as readonly string[]).includes(raw) ? (raw as T) : null;
  const rememberedOk = (allowed as readonly string[]).includes(remembered) ? remembered : fallback;
  const value: T = fromUrl ?? (raw ? fallback : rememberedOk);

  // Fill in (or correct) the address so it names what is on screen.
  const target = pathFor(value === fallback ? null : value);
  useEffect(() => {
    if (target && target !== location.pathname) {
      navigate({ pathname: target, search: location.search, hash: location.hash }, { replace: true });
    }
  }, [target, location.pathname, location.search, location.hash, navigate]);

  useEffect(() => {
    if (value !== remembered) setRemembered(value);
  }, [value, remembered, setRemembered]);

  const set = useCallback(
    (next: T) => {
      if (next === value) return;
      setRemembered(next);
      const to = pathFor(next === fallback ? null : next);
      // A tab's own selection (?item=, ?task=, …) belongs to the tab it was
      // opened on, so it does not ride along.
      if (to) navigate(to);
    },
    [value, fallback, pathFor, navigate, setRemembered],
  );
  return [value, set];
}
