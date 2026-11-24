// Progressive-web-app plumbing: service-worker registration, the "a new build
// is ready" signal, and the install prompt.
//
// The worker itself (public/sw.js) carries the rules about what it may cache;
// this module owns the page side of the contract:
//
//   * Registration happens in production builds only. Under `vite dev` the
//     module graph is served unbundled and rewritten on every save, and a
//     worker caching /assets/* there would serve yesterday's module for a file
//     the user just edited.
//   * A waiting worker is never activated silently. sw.js refuses to
//     skipWaiting on its own, so the swap happens when — and only when — the
//     user accepts the prompt, which then reloads the tab. Hot-swapping a
//     build under a live React tree strands it on chunks that no longer exist.
//   * `controllerchange` reloads exactly once. Two workers racing (two tabs
//     accepting the prompt together) would otherwise reload each other in a
//     loop.

import { useCallback, useEffect, useState } from "react";

const SW_URL = "/sw.js";
const INSTALL_DISMISSED_KEY = "lc-pwa-install-dismissed";

// `beforeinstallprompt` is Chromium-only and not in lib.dom.
type InstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};

let deferredInstall: InstallPromptEvent | null = null;
let waitingWorker: ServiceWorker | null = null;
let reloading = false;

type Listener = () => void;
const installListeners = new Set<Listener>();
const updateListeners = new Set<Listener>();
const notify = (set: Set<Listener>) => set.forEach((fn) => fn());

function readDismissed(): boolean {
  // Storage throws outright in some privacy modes — a missing preference must
  // never take the card (or the app) down with it.
  try {
    return localStorage.getItem(INSTALL_DISMISSED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeDismissed(): void {
  try {
    localStorage.setItem(INSTALL_DISMISSED_KEY, "1");
  } catch {
    /* best effort — the card reappears next session, which is survivable */
  }
}

/** True when the app is running from the home screen rather than a browser tab. */
export function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  // `standalone` is the iOS Safari spelling; the media query is everyone else's.
  const iosStandalone = (window.navigator as { standalone?: boolean }).standalone === true;
  return iosStandalone || window.matchMedia("(display-mode: standalone)").matches;
}

/**
 * True when this module was served by `vite dev` rather than a built bundle.
 *
 * `import.meta.env.PROD` would be the obvious test and is deliberately not used:
 * no build-time env channel reaches this bundle (see views/onboarding/
 * cloudDeployment.ts), and adding one for a boolean would mean typing Vite's
 * ImportMeta extension across the whole project. `import.meta.url` is plain ES
 * and says the same thing — dev serves modules from their source paths under
 * /src/, a production build emits one content-hashed file under /assets/.
 */
function isDevServer(): boolean {
  try {
    return new URL(import.meta.url).pathname.startsWith("/src/");
  } catch {
    // An unparseable module URL is not a dev server; registering is the
    // behaviour that matters in production, so fail towards it.
    return false;
  }
}

/** True on iPhone/iPad, where installing is a manual Share-sheet step. */
export function isIOS(): boolean {
  if (typeof navigator === "undefined") return false;
  // iPadOS reports itself as MacIntel and is the only "Mac" with a touch
  // screen — the same test index.html uses for the auto-zoom fix.
  return (
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
  );
}

/**
 * Register the worker and wire the install/update signals. Safe to call on
 * every boot; a no-op where service workers are unavailable (an insecure
 * origin, a privacy mode, an old browser) — the app is fully functional
 * without one, so every failure here is swallowed rather than surfaced.
 */
export function installPwa(): void {
  if (typeof window === "undefined") return;

  // Marks the root element so CSS can inset for the OS status bar and home
  // indicator on iOS versions that support the home screen but not the
  // `display-mode: standalone` media query. Set once: the display mode cannot
  // change without a fresh launch.
  if (isStandalone()) document.documentElement.dataset.standalone = "1";

  window.addEventListener("beforeinstallprompt", (event) => {
    // Chromium shows its own mini-infobar unless the event is cancelled; we
    // want the prompt to come from our card, at a moment the user chose.
    event.preventDefault();
    deferredInstall = event as InstallPromptEvent;
    notify(installListeners);
  });

  window.addEventListener("appinstalled", () => {
    deferredInstall = null;
    notify(installListeners);
  });

  if (!("serviceWorker" in navigator)) return;
  // Dev builds are served unbundled and mutate on every save.
  if (isDevServer()) return;

  const trackWaiting = (registration: ServiceWorkerRegistration) => {
    const candidate = registration.waiting;
    if (candidate && navigator.serviceWorker.controller) {
      // A worker only *waits* when another one already controls the page, so
      // this is a genuine update rather than the first install.
      waitingWorker = candidate;
      notify(updateListeners);
    }
  };

  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register(SW_URL)
      .then((registration) => {
        trackWaiting(registration);
        registration.addEventListener("updatefound", () => {
          const installing = registration.installing;
          if (!installing) return;
          installing.addEventListener("statechange", () => {
            if (installing.state === "installed") trackWaiting(registration);
          });
        });
        // A tab left open for days would otherwise never look for a new build.
        // Cheap: the worker script is a conditional request that 304s.
        setInterval(() => registration.update().catch(() => {}), 60 * 60 * 1000);
      })
      .catch(() => {
        /* no worker — the app runs exactly as it did before this file existed */
      });

    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (reloading) return;
      reloading = true;
      window.location.reload();
    });
  });
}

/**
 * Purge every service-worker cache, then reload — the recovery path for a
 * stale build.
 *
 * This is the bridge that did not exist. `chunkRecovery.ts` and
 * `RouteErrorBoundary` both recovered with a bare `window.location.reload()`,
 * which is a navigation, which goes back through the worker and the HTTP cache
 * and can be answered with the very document whose chunks just 404'd. Worse,
 * `chunkRecovery` guards that reload with a `cache: "no-store"` probe — the one
 * request in the app that bypasses both caches — so it would confirm the server
 * was fine and then reload into the stale copy anyway. The guard proved the
 * opposite of what the action did.
 *
 * Everything here is best-effort and bounded: a missing worker, a refused
 * message, or a worker that never answers must still end in a reload, because
 * the alternative is a user stuck on an error card with a button that does
 * nothing.
 */
export async function hardReset(): Promise<void> {
  try {
    await Promise.race([purgeWorkerCaches(), delay(HARD_RESET_TIMEOUT_MS)]);
  } catch {
    /* fall through to the reload — a failed purge is still better than none */
  }
  // Cache-bust the document itself too. Even with the caches gone, the HTTP
  // cache can still hold the shell on a deployment whose server has not picked
  // up the `no-cache` header yet (see deploy/Caddyfile.allinone).
  try {
    const url = new URL(window.location.href);
    url.searchParams.set(RELOAD_BUST_PARAM, String(Date.now()));
    window.location.replace(url.toString());
  } catch {
    window.location.reload();
  }
}

const HARD_RESET_TIMEOUT_MS = 2_000;
/** Stripped from the URL on boot — see `stripReloadBustParam`. */
const RELOAD_BUST_PARAM = "lc_reload";

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Ask the active worker to drop every cache, resolving when it confirms. */
function purgeWorkerCaches(): Promise<void> {
  return new Promise((resolve) => {
    const worker = navigator.serviceWorker?.controller;
    if (!worker) {
      resolve();
      return;
    }
    const channel = new MessageChannel();
    channel.port1.onmessage = () => resolve();
    try {
      worker.postMessage({ type: "HARD_RESET" }, [channel.port2]);
    } catch {
      resolve();
    }
  });
}

/**
 * Drop the cache-buster `hardReset` added, so it never reaches the router or
 * gets shared in a copied URL. Replaces the history entry rather than pushing,
 * so Back still goes where the user expects.
 */
export function stripReloadBustParam(): void {
  try {
    const url = new URL(window.location.href);
    if (!url.searchParams.has(RELOAD_BUST_PARAM)) return;
    url.searchParams.delete(RELOAD_BUST_PARAM);
    window.history.replaceState(window.history.state, "", url.toString());
  } catch {
    /* a URL we cannot parse is one we cannot clean — harmless either way */
  }
}

/** Whether an install prompt can be shown, and the call that shows it. */
export function usePwaInstall() {
  const [available, setAvailable] = useState(() => deferredInstall !== null);
  const [dismissed, setDismissed] = useState(readDismissed);

  useEffect(() => {
    const listener = () => setAvailable(deferredInstall !== null);
    installListeners.add(listener);
    return () => {
      installListeners.delete(listener);
    };
  }, []);

  const promptInstall = useCallback(async () => {
    const event = deferredInstall;
    if (!event) return false;
    // The event is single-use: Chromium refuses a second prompt() on the same
    // one, so it is dropped whatever the user chose.
    deferredInstall = null;
    notify(installListeners);
    try {
      await event.prompt();
      const { outcome } = await event.userChoice;
      return outcome === "accepted";
    } catch {
      return false;
    }
  }, []);

  const dismiss = useCallback(() => {
    writeDismissed();
    setDismissed(true);
  }, []);

  return { available, dismissed, dismiss, promptInstall };
}

/** Whether a newer build is installed and waiting, and the call that adopts it. */
export function usePwaUpdate() {
  const [ready, setReady] = useState(() => waitingWorker !== null);

  useEffect(() => {
    const listener = () => setReady(waitingWorker !== null);
    updateListeners.add(listener);
    return () => {
      updateListeners.delete(listener);
    };
  }, []);

  const applyUpdate = useCallback(() => {
    const worker = waitingWorker;
    if (!worker) return;
    // The reload rides on `controllerchange` above, once the new worker has
    // actually taken over — reloading here would just re-run the old build.
    worker.postMessage({ type: "SKIP_WAITING" });
  }, []);

  return { ready, applyUpdate };
}
