import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";

// Session-scoped state cache. React Router unmounts a route's component when you
// navigate away (left-nav tabs), which discards its useState — so selected
// items, search text, active sub-tabs, form drafts and toggles are lost on
// return. This cache lives at module scope, so it survives unmount/remount for
// the life of the SPA session (a full page reload starts fresh).
//
// Use it as a drop-in replacement for useState for USER-DRIVEN UI state that
// should persist across navigation. Do NOT use it for fetched data, loading
// flags, errors, or live/streaming state — those should re-initialise on mount.
//
// Keys are global: namespace them per view (e.g. "agents.search").

const cache = new Map<string, unknown>();

export function usePersistentState<T>(key: string, initial: T | (() => T)) {
  const [value, setValue] = useState<T>(() => {
    if (cache.has(key)) return cache.get(key) as T;
    return typeof initial === "function" ? (initial as () => T)() : initial;
  });

  const set = useCallback<Dispatch<SetStateAction<T>>>(
    (next) => {
      setValue((prev) => {
        const resolved = typeof next === "function" ? (next as (p: T) => T)(prev) : next;
        cache.set(key, resolved);
        return resolved;
      });
    },
    [key],
  );

  return [value, set] as [T, Dispatch<SetStateAction<T>>];
}

// Read a cached value without subscribing (e.g. to seed a ref on mount).
export function peekPersistentState<T>(key: string): T | undefined {
  return cache.has(key) ? (cache.get(key) as T) : undefined;
}

// Write a cached value without triggering a React update — for high-frequency,
// non-rendering state like scroll offsets.
export function pokePersistentState<T>(key: string, value: T): void {
  cache.set(key, value);
}

// Persist a scroll container's scrollTop across unmount/remount (left-nav tab
// switches). Attach `ref` + `onScroll` to the scrollable element. The saved
// offset is re-applied whenever `deps` change, so async-loaded content (a file
// tree filling in, a long file rendering) restores once it's tall enough.
export function useScrollRestore<T extends HTMLElement>(key: string, deps: unknown[]) {
  const ref = useRef<T | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const saved = cache.get(key) as number | undefined;
    if (saved && saved > 0 && Math.abs(el.scrollTop - saved) > 1) {
      el.scrollTop = saved;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ...deps]);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (el) cache.set(key, el.scrollTop);
  }, [key]);

  return { ref, onScroll };
}

// Per-route scroll memory for the app's SINGLE shared scroll container — the
// <main> element in Layout, which React Router never unmounts (only its Outlet
// children swap on navigation). useScrollRestore can't help here: it keys off a
// component that unmounts, but <main> persists, so its scrollTop must instead be
// remembered per route path and re-applied when that path is shown again — which
// is what makes a left-nav round-trip return you to where you left off. (It also
// resets to the top when you open a not-yet-seen route, since the shared scroller
// would otherwise keep the previous route's offset.)
//
// Restoration re-applies on each frame until the saved offset becomes reachable
// (the view's async content has filled in), then stops. It yields the moment the
// user scrolls, so it never fights a deliberate scroll (wheel/touch/keyboard or a
// scrollbar drag).
const routeScroll = new Map<string, number>();

export function useRouteScrollMemory(ref: RefObject<HTMLElement | null>, pathname: string): void {
  // Latest path, readable from the single long-lived scroll listener below so it
  // always saves under the *current* route — never a stale captured one.
  const pathnameRef = useRef(pathname);
  pathnameRef.current = pathname;
  // True while we're programmatically restoring, so the listener doesn't record
  // the intermediate (clamped) offsets we set as if the user had scrolled there.
  const restoring = useRef(false);

  // ONE listener for the life of the scroller (deps: [ref] only). Records the
  // offset for whatever route is current when the user scrolls (one write/frame).
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let frame = 0;
    const onScroll = () => {
      if (restoring.current || frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const node = ref.current;
        if (node) routeScroll.set(pathnameRef.current, node.scrollTop);
      });
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [ref]);

  // Restore the saved offset when a path is (re)shown, re-applying as content
  // grows. `target` is captured locally so the save-on-scroll above can't perturb
  // it mid-restore.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const target = routeScroll.get(pathname) ?? 0;
    restoring.current = true;
    el.scrollTop = target; // also resets to the top for routes with no saved offset
    let lastApplied = el.scrollTop; // clamped to the current max if content is short
    let raf = 0;
    const done = () => {
      restoring.current = false;
    };
    if (target <= 0) {
      // Re-enable saving once the (suppressed) programmatic scroll event flushes.
      raf = requestAnimationFrame(done);
      return () => {
        if (raf) cancelAnimationFrame(raf);
        restoring.current = false;
      };
    }
    const start = performance.now();
    const tick = () => {
      const node = ref.current;
      if (!node) {
        done();
        return;
      }
      // User moved the scroll since our last programmatic set → keep their spot
      // and stop, so we never fight a deliberate scroll.
      if (Math.abs(node.scrollTop - lastApplied) > 1) {
        routeScroll.set(pathname, node.scrollTop);
        done();
        return;
      }
      const max = node.scrollHeight - node.clientHeight;
      if (max + 1 >= target) {
        node.scrollTop = target; // content is tall enough now — land exactly
        done();
        return;
      }
      node.scrollTop = max; // not yet — sit at the bottom and wait for more
      lastApplied = node.scrollTop;
      if (performance.now() - start < 2000) raf = requestAnimationFrame(tick);
      else done();
    };
    raf = requestAnimationFrame(tick);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      restoring.current = false;
    };
  }, [ref, pathname]);
}

// Drop cached entries — all, or those under a "view." prefix.
export function clearPersistentState(prefix?: string): void {
  if (!prefix) {
    cache.clear();
    return;
  }
  for (const k of Array.from(cache.keys())) if (k.startsWith(prefix)) cache.delete(k);
}
