import { useEffect, useState } from "react";
import { api } from "./api";

// Server capability flags from /api/status (e.g. terminal_enabled — which folds
// in both the runtime feature flag AND whether the current human may use it).
// Fetched once and shared; call refreshCapabilities() after an action that could
// change them (e.g. an admin granting terminal access).

let cache: Promise<Record<string, boolean>> | null = null;
let version = 0;
const listeners = new Set<() => void>();

function fetchCaps(): Promise<Record<string, boolean>> {
  if (!cache) {
    // cachedGet, not get: /overview asks for the same payload on mount, and
    // landing on the dashboard would otherwise issue two identical /api/status
    // round-trips side by side.
    cache = api
      .cachedGet<{ capabilities?: Record<string, boolean> }>("/api/status")
      .then((s) => s.capabilities || {})
      .catch(() => ({}));
  }
  return cache;
}

export function refreshCapabilities(): void {
  cache = null;
  version += 1;
  for (const listener of listeners) listener();
}

// undefined while loading, then the whole capability map — for callers that
// need many flags at once (e.g. the nav filter reading every tab_<name> flag).
// Same tri-state contract as useCapability: undefined must NOT be treated as
// false, or default-on items would flash out during load.
export function useCapabilities(): Record<string, boolean> | undefined {
  const [caps, setCaps] = useState<Record<string, boolean> | undefined>(undefined);
  const [seenVersion, setSeenVersion] = useState(version);

  useEffect(() => {
    function onRefresh() {
      setSeenVersion(version);
    }
    listeners.add(onRefresh);
    return () => {
      listeners.delete(onRefresh);
    };
  }, []);

  useEffect(() => {
    let on = true;
    fetchCaps().then((all) => {
      if (on) setCaps(all);
    });
    return () => {
      on = false;
    };
  }, [seenVersion]);
  return caps;
}

// undefined while loading, then the boolean — lets callers avoid a flash of the
// "disabled" state before the first response.
export function useCapability(key: string): boolean | undefined {
  const [val, setVal] = useState<boolean | undefined>(undefined);
  const [seenVersion, setSeenVersion] = useState(version);

  useEffect(() => {
    function onRefresh() {
      setSeenVersion(version);
    }
    listeners.add(onRefresh);
    return () => {
      listeners.delete(onRefresh);
    };
  }, []);

  useEffect(() => {
    let on = true;
    fetchCaps().then((caps) => {
      if (on) setVal(!!caps[key]);
    });
    return () => {
      on = false;
    };
  }, [key, seenVersion]);
  return val;
}
