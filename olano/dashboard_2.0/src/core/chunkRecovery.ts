// Recovery for a failed lazy-chunk import.
//
//   "Failed to fetch dynamically imported module:
//    https://<host>/assets/AgentDetail-DmZEsNLP.js"
//
// Why it happens: every view is a content-hashed chunk and this dashboard is a
// long-lived tab. A deploy replaces /srv (Caddy all-in-one) or the deployment
// with a build whose chunks carry NEW hashes — the old file names are
// gone the moment the swap lands. A tab opened before the deploy is still
// running the old index.html's module graph, so the first navigation to a route
// whose chunk it hadn't already downloaded asks for a file that no longer
// exists. Nothing is wrong with the server or the code: the tab is pinned to a
// build that isn't deployed anymore. Routes that are never prefetched (the
// agent detail page — reached by a click, not by the idle warm list) are the
// ones that hit it, because their fetch happens minutes or hours after load.
//
// Why we reload instead of retrying: the HTML spec makes a module map entry
// permanent. Once a URL fails to fetch, every later `import()` of that same
// specifier replays the failure from the map WITHOUT touching the network, so
// "just retry the import" is a no-op that only looks like a retry. The one
// thing that genuinely recovers is a document reload — it re-fetches
// index.html and with it the hashes of the build that is actually deployed.
//
// Guards, because an automatic reload is a big hammer:
//   • only for errors that are actually a module fetch failure (a view whose
//     top-level code throws is a real bug — reloading would hide it),
//   • only when the origin still answers (offline: a reload lands on the
//     browser's error page, strictly worse than the error card),
//   • at most one per cooldown window per tab, so a chunk that keeps failing
//     settles into the error card instead of a reload loop.
// When any guard says no, the error propagates to RouteErrorBoundary, which
// explains it and offers the same reload as a button.

import { hardReset } from "./pwa";

/** How long after an automatic reload we refuse to trigger another one. */
const RELOAD_COOLDOWN_MS = 30_000;
const RELOAD_KEY = "lc.chunkReloadAt";

// Every engine words this differently and Vite's preload helper adds two more
// of its own; all of them mean "the bytes for this module never arrived".
const CHUNK_ERROR_RE = new RegExp(
  [
    "failed to fetch dynamically imported module", // Chrome / Edge
    "error loading dynamically imported module", // Firefox
    "importing a module script failed", // Safari
    "dynamically imported module", // any future rewording of the above
    "unable to preload", // Vite's __vitePreload, CSS deps
    "failed to fetch", // the raw fetch rejection it rethrows
    "load failed", // Safari's wording for the same
    "networkerror",
  ].join("|"),
  "i",
);

/**
 * Does this rejection mean the chunk itself couldn't be fetched (as opposed to
 * the module loading fine and then throwing)? Only the former is recoverable
 * by reloading.
 */
export function isChunkLoadError(err: unknown): boolean {
  if (!err) return false;
  const message = err instanceof Error ? err.message : String(err);
  if (CHUNK_ERROR_RE.test(message)) return true;
  // Backstop for a wording none of the above covers: a fetch-layer TypeError
  // (what the Vite preload helper rethrows for a dep it couldn't get) that
  // names a build asset can only be a chunk that didn't arrive.
  const name = err instanceof Error ? err.name : "";
  return name === "TypeError" && message.includes("/assets/");
}

function lastAutoReloadAt(): number {
  try {
    return Number(sessionStorage.getItem(RELOAD_KEY)) || 0;
  } catch {
    return 0; // private mode / storage blocked — treat as "never reloaded"
  }
}

function markAutoReload(): void {
  try {
    sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
  } catch {
    /* storage blocked: the cooldown is best-effort, the error card is the net */
  }
}

/**
 * Is a reload going to land on a page? Confirms the origin is still serving the
 * HTML shell — `navigator.onLine` alone lies (it reports link state, not
 * reachability), and reloading into a dead server replaces a readable error
 * card with the browser's offline page.
 */
async function shellIsReachable(): Promise<boolean> {
  if (navigator.onLine === false) return false;
  try {
    const res = await fetch("/index.html", { cache: "no-store", credentials: "same-origin" });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * Run a `() => import(...)` and, when it fails because the chunk is gone,
 * reload the document instead of surfacing an error the user can do nothing
 * about. Anything else — including a chunk that loads and then throws —
 * propagates untouched.
 *
 * Wrap the loaders behind `React.lazy`, not background prefetches: a reload is
 * only acceptable when the user is waiting on that view anyway (see
 * `routeChunks.ts`, which keeps prefetch failures silent).
 */
export async function importChunk<T>(load: () => Promise<T>): Promise<T> {
  try {
    return await load();
  } catch (err) {
    if (!isChunkLoadError(err)) throw err;
    if (Date.now() - lastAutoReloadAt() < RELOAD_COOLDOWN_MS) throw err;
    if (!(await shellIsReachable())) throw err;
    markAutoReload();
    // `hardReset`, not `location.reload()`. The probe above deliberately reads
    // past every cache (`no-store`), so it can confirm the server is serving a
    // NEW build while a plain reload — an ordinary navigation — comes back
    // through the service worker and the HTTP cache and re-renders the SAME
    // dead one. That mismatch is why the installed app could never recover:
    // the guard passed, the reload ran, and nothing changed.
    void hardReset();
    // Never settle: the document is on its way out, and resolving or rejecting
    // here would flash the error card (or a half-mounted view) over a page that
    // is already being replaced.
    return await new Promise<T>(() => {});
  }
}
