import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "./api";
import { useAuth } from "./auth";

// Per-user UI preferences, persisted server-side (GET/PUT /api/auth/me/prefs)
// so they follow the human across devices and survive a full reload — unlike
// device-local localStorage (theme, sidebar-collapsed) or the module-scoped
// session cache (usePersistentState). Used for things like the sidebar nav
// order. Writes are optimistic + debounced and shallow-merged on the server,
// so updating one key never clobbers another.

export interface UserPrefs {
  /** Ordered nav `to` paths shown in the always-visible Primary group. */
  nav_primary?: string[];
  /** Ordered nav `to` paths tucked under the collapsible More group. */
  nav_more?: string[];
  /** Whether the More group is collapsed (defaults to collapsed). */
  nav_more_collapsed?: boolean;
  /**
   * Ids of the nav promotions (Layout's `NAV_PROMOTIONS`) this user has already
   * been through. A saved nav split freezes at whatever the defaults were when
   * the human first dragged something, so promotions are how a later default
   * reaches them — recorded here so each one applies exactly once and a
   * deliberate move back under More is never undone.
   */
  nav_promotions?: string[];
  /**
   * Org Chart manual placement: node id → { x, y, w, h } in canvas px. Only
   * nodes the human has dragged or resized are stored; everything else falls
   * back to the computed hierarchy auto-layout. Lives in per-user prefs so a
   * curated arrangement follows the human across devices and reloads.
   */
  orgchart_layout?: Record<string, { x: number; y: number; w: number; h: number }>;
  /**
   * Org Chart manual connector adjustments: edge key → the user's dragged
   * placement of the connector's middle segment (shape family + channel
   * coordinate). Only edges the human has dragged are stored; everything else
   * auto-routes. Cleared together with orgchart_layout by "Reset layout".
   */
  orgchart_edges?: Record<string, { shape: string; d: number }>;
  [key: string]: unknown;
}

interface PrefsApi {
  prefs: UserPrefs;
  setPref: <K extends keyof UserPrefs>(key: K, value: UserPrefs[K]) => void;
  loaded: boolean;
}

const Ctx = createContext<PrefsApi | null>(null);

export function PrefsProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  // Re-key the load on the signed-in user so switching accounts (logout →
  // login) never shows the previous user's preferences.
  const userId = user?.id ?? null;
  const [prefs, setPrefs] = useState<UserPrefs>({});
  const [loaded, setLoaded] = useState(false);
  const pending = useRef<Partial<UserPrefs>>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    const patch = pending.current;
    pending.current = {};
    if (Object.keys(patch).length === 0) return;
    void api.put("/api/auth/me/prefs", { prefs: patch }).catch(() => {
      /* best-effort: prefs are non-critical UI state */
    });
  }, []);

  useEffect(() => {
    let alive = true;
    setLoaded(false);
    pending.current = {};
    api
      .get<{ prefs: UserPrefs }>("/api/auth/me/prefs")
      .then((r) => alive && setPrefs(r.prefs ?? {}))
      .catch(() => alive && setPrefs({}))
      .finally(() => alive && setLoaded(true));
    return () => {
      alive = false;
    };
  }, [userId]);

  // Flush any queued write before the provider unmounts (e.g. on logout).
  useEffect(() => () => flush(), [flush]);

  const setPref = useCallback<PrefsApi["setPref"]>(
    (key, value) => {
      setPrefs((prev) => ({ ...prev, [key]: value }));
      pending.current = { ...pending.current, [key]: value };
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(flush, 500);
    },
    [flush],
  );

  return <Ctx.Provider value={{ prefs, setPref, loaded }}>{children}</Ctx.Provider>;
}

export function usePrefs(): PrefsApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("usePrefs must be used within <PrefsProvider>");
  return ctx;
}
