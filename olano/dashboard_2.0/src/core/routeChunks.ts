// Route chunk loaders — shared by app.tsx's lazy() definitions and the
// prefetcher below, so a prefetch and the eventual render resolve the *same*
// module promise (the second caller gets the already-warm one).
//
// Why this exists: every view except Overview is route-split, and a React.lazy
// chunk is only requested once its route actually renders. That put the chunk
// fetch at the end of a serial chain — index.js → /api/auth/me → Layout mount →
// chunk → the view's own data fetches — so the first visit to each tab paid a
// full round-trip plus transfer *after* the click, on top of everything else.
// Warming the chunks while the browser is idle moves that cost off the click.

import { useEffect } from "react";
import { importChunk, isChunkLoadError } from "./chunkRecovery";

export const ROUTE_LOADERS = {
  "/insights": () => import("../views/insights/InsightsView"),
  "/chat": () => import("../views/chat/ChatView"),
  "/agents": () => import("../views/agents/AgentsView"),
  "/agents/:name": () => import("../views/agents/AgentDetail"),
  "/groups/:id": () => import("../views/groups/GroupChatPage"),
  "/cortex": () => import("../views/cortex/CortexView"),
  "/scheduler": () => import("../views/scheduler/SchedulerView"),
  "/calendar": () => import("../views/calendar/CalendarView"),
  "/files": () => import("../views/files/FilesView"),
  "/memory": () => import("../views/memory/MemoryView"),
  "/notes": () => import("../views/notes/NotesView"),
  "/knowledge": () => import("../views/knowledge/KnowledgeView"),
  "/tools": () => import("../views/tools/ToolsView"),
  "/issues": () => import("../views/issues/IssuesView"),
  "/revisions": () => import("../views/revisions/RevisionsView"),
  "/providers": () => import("../views/providers/ProvidersView"),
  "/connections": () => import("../views/connections/ConnectionsView"),
  "/vault": () => import("../views/vault/VaultView"),
  "/activity": () => import("../views/activity/ActivityView"),
  "/orgchart": () => import("../views/orgchart/OrgChartView"),
  "/logs": () => import("../views/logs/LogsView"),
  "/terminal": () => import("../views/terminal/TerminalView"),
  "/playground": () => import("../views/playground/PlaygroundView"),
  "/config": () => import("../views/config/ConfigView"),
  "/admin": () => import("../views/admin/AdminView"),
  // The simple (bot chat) shell. It is not a route of the dashboard — it
  // replaces it — but it lives here so it ships as its own chunk and inherits
  // loadRoute()'s stale-build recovery. Advanced users never download it, and
  // simple users never download the dashboard.
  "/simple": () => import("../views/simple/SimpleApp"),
} as const;

export type RoutePath = keyof typeof ROUTE_LOADERS;

/**
 * Load a route's chunk for rendering — this is what app.tsx's `lazy()` calls
 * await. Identical to calling the loader directly except that a chunk the
 * server no longer has (the tab is pinned to a build that was deployed over)
 * reloads the document instead of dead-ending in the error card. Prefetches
 * deliberately do NOT go through here: nobody is waiting on them, so a
 * background failure must never yank the page the user is reading.
 */
export function loadRoute<P extends RoutePath>(
  path: P,
): Promise<Awaited<ReturnType<(typeof ROUTE_LOADERS)[P]>>> {
  // Indexing the map with a type parameter widens the value to the union of
  // every loader, which no longer matches any one module's shape. The cast
  // pins it back to the module this key actually loads, so call sites keep
  // their exact named exports (`m.InsightsView`, …).
  type Module = Awaited<ReturnType<(typeof ROUTE_LOADERS)[P]>>;
  return importChunk(ROUTE_LOADERS[path] as () => Promise<Module>);
}

const started = new Set<string>();

/** Start (once) the chunk download for a route. Safe to call on every hover. */
export function prefetchRoute(path: string): void {
  const loader = ROUTE_LOADERS[path as RoutePath];
  if (!loader || started.has(path)) return;
  started.add(path);
  // A failed prefetch is never fatal: the route's own lazy() will retry and
  // surface the error through the Suspense boundary if it still can't load.
  void loader().catch(() => started.delete(path));
}

// Warm order — the tabs an operator actually opens, most-used first. Heavy
// chunks (insights pulls recharts, terminal pulls xterm) sit behind the cheap
// ones so a short idle window still buys the common navigations.
//
// Advanced mode only. The simple shell has no tabs to warm — prefetching this
// list there would download recharts and xterm onto a phone for screens that
// user can never open, which is why app.tsx passes `false` in simple mode.
const WARM_ORDER: RoutePath[] = [
  "/chat",
  "/agents",
  "/files",
  "/activity",
  "/insights",
  "/scheduler",
  "/connections",
  "/memory",
];

type IdleHandle = { cancel: () => void };

function onIdle(fn: () => void): IdleHandle {
  if (typeof window.requestIdleCallback === "function") {
    const id = window.requestIdleCallback(fn, { timeout: 3000 });
    return { cancel: () => window.cancelIdleCallback(id) };
  }
  const id = window.setTimeout(fn, 300);
  return { cancel: () => window.clearTimeout(id) };
}

/** Resolve once the initial page load has finished, so speculative work can't
 *  compete with the resources the visible route is still waiting on. */
function afterPageLoad(run: () => void): () => void {
  if (document.readyState === "complete") {
    run();
    return () => {};
  }
  window.addEventListener("load", run, { once: true });
  return () => window.removeEventListener("load", run);
}

/**
 * Warm the route chunks in the background once the app is up.
 *
 * Two guards keep this from stealing bandwidth from the page the user is
 * actually looking at: it doesn't start until the load event (the current
 * route's own chunk and first data fetches are in flight before that), and it
 * then pulls one chunk per idle slice, strictly sequentially. Firing them all
 * at once would make the visible page slower to win a race nobody is running
 * yet. Skipped entirely on Save-Data / 2g — never spend metered bytes on a
 * guess.
 */
export function useRouteChunkPrefetch(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    const conn = (
      navigator as Navigator & {
        connection?: { saveData?: boolean; effectiveType?: string };
      }
    ).connection;
    if (conn?.saveData || /(^|-)2g$/.test(conn?.effectiveType || "")) return;

    let cancelled = false;
    let handle: IdleHandle | null = null;
    let i = 0;

    const step = () => {
      if (cancelled) return;
      while (i < WARM_ORDER.length && started.has(WARM_ORDER[i])) i++;
      if (i >= WARM_ORDER.length) return;
      const path = WARM_ORDER[i++];
      prefetchRoute(path);
      const loader = ROUTE_LOADERS[path];
      // Chain on the same promise prefetchRoute kicked off (module loads are
      // memoized), so the next chunk starts only once this one has landed.
      void loader()
        .then(
          () => true,
          // A chunk the server no longer has means this tab is pinned to a
          // build that was deployed over — every remaining chunk is missing
          // too. Stop warming instead of firing the rest of the list at 404s;
          // the next real navigation goes through loadRoute(), which recovers
          // by reloading. Any other failure is that one module's problem.
          (err: unknown) => !isChunkLoadError(err),
        )
        .then((keepGoing) => {
          if (keepGoing && !cancelled) handle = onIdle(step);
        });
    };

    const cancelLoadWait = afterPageLoad(() => {
      if (!cancelled) handle = onIdle(step);
    });
    return () => {
      cancelled = true;
      cancelLoadWait();
      handle?.cancel();
    };
  }, [enabled]);
}
