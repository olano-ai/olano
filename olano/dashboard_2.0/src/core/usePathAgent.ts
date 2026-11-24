import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { usePersistentState } from "./usePersistentState";
import { parseRoute, viewPath } from "./routeUrls";
import type { UrlHistory } from "./useUrlParam";

/**
 * The optional agent segment of a fleet-wide view (/scheduler/<agent>,
 * /logs/<agent>, /overview/<agent>; FILTER_VIEWS in core/routeUrls.ts) as
 * two-way state. "" means no agent: the whole fleet, or nothing open.
 *
 * The URL decides. The last value is still remembered for the session, so the
 * sidebar's bare link returns to it exactly as before the agent was in the
 * address, and the address is then filled in (replace: same place).
 *
 * `history` is how a change is recorded: "replace" when the agent only
 * narrows a list (a filter is not a Back step), "push" when it opens
 * something (Overview's agent panel).
 */
export function usePathAgent(
  view: string,
  rememberKey: string,
  history: UrlHistory,
): [string, (name: string) => void] {
  const location = useLocation();
  const navigate = useNavigate();
  const [remembered, setRemembered] = usePersistentState<string>(rememberKey, "");
  const [arrived, setArrived] = useState(false);
  const fromUrl = parseRoute(location.pathname).agent ?? "";
  const value = fromUrl || (arrived ? "" : remembered);

  useEffect(() => {
    if (arrived) return;
    setArrived(true);
    if (!fromUrl && remembered) {
      navigate(
        { pathname: viewPath(view, { agent: remembered }), search: location.search, hash: location.hash },
        { replace: true },
      );
    }
  }, [arrived, fromUrl, remembered, view, navigate, location.search, location.hash]);

  useEffect(() => {
    if (arrived && value !== remembered) setRemembered(value);
  }, [arrived, value, remembered, setRemembered]);

  const set = useCallback(
    (name: string) => {
      if (name === fromUrl) return;
      navigate(
        { pathname: viewPath(view, { agent: name || null }), search: location.search, hash: location.hash },
        { replace: history === "replace" },
      );
    },
    [fromUrl, view, navigate, location.search, location.hash, history],
  );
  return [value, set];
}
