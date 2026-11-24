/* Olano Mission Control — service worker.
 *
 * Exists for exactly two things: an installable app (a manifest alone does not
 * satisfy the install criteria) and a shell that still paints when the network
 * is gone. It is deliberately NOT a general response cache.
 *
 * ── The rule that matters ──────────────────────────────────────────────────
 * A service worker is the one asset you cannot fix by deploying: a bad one is
 * already installed on every device, and it decides whether the next request
 * ever reaches the server. So this file follows four rules, and a change that
 * breaks any of them is a defect no matter how much faster it makes something:
 *
 *   1. `/api/**` is NEVER intercepted. Not cached, not read, not rewritten.
 *      Chat is SSE (`/api/chat/stream`), status is polled every 2.5s, and
 *      responses are per-user and auth-gated. `fetch` returns without calling
 *      respondWith for them, so the browser's own stack handles them exactly as
 *      if no worker existed.
 *   2. The HTML shell is NEVER served cache-first. It is the only document that
 *      names the current asset hashes, so a cached one pins a tab to a dead
 *      build forever. Network wins; the cache is the offline fallback only.
 *   3. A new worker NEVER activates under a live tab on its own. `skipWaiting`
 *      fires only when the page asks (the user accepted the update prompt), so
 *      a mid-session swap can't tear hashed chunks out from under a running
 *      React tree.
 *   4. Only content-hashed assets are cached long-term. Their URL changes when
 *      their bytes do, so a stale entry is unreachable rather than wrong.
 *
 * ── Kill switch ───────────────────────────────────────────────────────────
 * If this worker ever misbehaves in the field, replace this whole file with:
 *
 *     self.addEventListener("install", () => self.skipWaiting());
 *     self.addEventListener("activate", async () => {
 *       for (const k of await caches.keys()) await caches.delete(k);
 *       await self.registration.unregister();
 *       for (const c of await self.clients.matchAll()) c.navigate(c.url);
 *     });
 *
 * and deploy. Every installed client picks it up on its next navigation (the
 * worker script itself is revalidated on every load — browsers refuse to cache
 * it for more than 24h, and every serving path sends `no-cache` for it), then
 * unregisters and goes back to plain browser behaviour.
 */

// Bump on any behavioural change to this file: `activate` deletes every cache
// whose name is not in CACHES, so a bump is also the cache purge. v1 → v2
// exists to evict the stale shells and dead-build chunks v1 could accumulate.
const VERSION = "v2";
const SHELL_CACHE = `olano-shell-${VERSION}`;
const ASSET_CACHE = `olano-assets-${VERSION}`;
const CACHES = [SHELL_CACHE, ASSET_CACHE];

// The document the SPA boots from. Every client route (/chat, /agents/x, …) is
// rewritten to this by Caddy's `try_files` and server.mjs's dotless-path rule,
// so one entry backs every navigation.
const SHELL_URL = "/index.html";

// Same-origin prefixes the worker must never touch. `/api` is the runtime,
// `/pages` is agent-published pages (their own auth), `/.well-known` is OAuth
// and MCP metadata, `/__control` is the dev-server restart bridge.
const BYPASS = ["/api/", "/pages/", "/.well-known/", "/__control/"];

// Vite emits content-hashed bundles under /assets/. `theme1/` is the one
// hand-managed subtree there — not hashed, so it must revalidate instead
// (mirrors the Cache-Control split in the Caddyfiles and server.mjs).
const isHashedAsset = (url) =>
  url.pathname.startsWith("/assets/") && !url.pathname.startsWith("/assets/theme1/");

// Only an actual HTML document may be stored as the app shell. `response.ok`
// alone would also accept an error page, an auth redirect body, or a
// JavaScript file — and a shell cache holding JavaScript serves JavaScript as
// the document to every later offline navigation.
const isHtml = (response) =>
  (response.headers.get("Content-Type") || "").toLowerCase().includes("text/html");

self.addEventListener("install", (event) => {
  // Warm the shell so the very first offline load has something to paint. It
  // is a fallback, never the served copy while online — see rule 2.
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.add(new Request(SHELL_URL, { cache: "reload" })))
      // A failed warm must not fail the install: the worker is still useful,
      // it just has no offline shell until the first successful navigation.
      .catch(() => {}),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Navigation preload is deliberately NOT enabled. Its request is issued
      // by the browser with default cache semantics, so it can hand back the
      // very stale document `handleNavigation` now revalidates past — and it
      // takes no options to change that. The latency it would save is one
      // worker startup; correctness of the shell is worth more.
      if (self.registration.navigationPreload) {
        await self.registration.navigationPreload.disable().catch(() => {});
      }
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k.startsWith("olano-") && !CACHES.includes(k)).map((k) => caches.delete(k)),
      );
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (event) => {
  const type = event.data && event.data.type;
  // Sent by core/pwa.ts when the user accepts the update prompt. This is the
  // ONLY path that activates a waiting worker — see rule 3.
  if (type === "SKIP_WAITING") self.skipWaiting();
  // Purge every cache, then answer so the page knows the reload it is about to
  // do will actually reach the network. Sent by core/pwa.ts `hardReset()` when
  // a chunk import fails — without it the page's reload is answered from the
  // same caches that caused the failure, which is why the in-app Reload button
  // used to do nothing at all.
  if (type === "HARD_RESET") {
    event.waitUntil(
      (async () => {
        try {
          for (const k of await caches.keys()) await caches.delete(k);
        } finally {
          // Always reply, even if a delete threw: the caller falls back to a
          // plain reload on timeout, and a silent hang is the worse failure.
          const port = event.ports && event.ports[0];
          if (port) port.postMessage({ ok: true });
        }
      })(),
    );
  }
  // Escape hatch usable from the console without a redeploy.
  if (type === "UNREGISTER") {
    event.waitUntil(
      (async () => {
        for (const k of await caches.keys()) await caches.delete(k);
        await self.registration.unregister();
      })(),
    );
  }
});

async function handleNavigation(event) {
  // NETWORK-FIRST, and never from the cache while online: the cached shell is
  // returned only from the `catch` below, i.e. only when the network actually
  // failed. That is rule 2, and it is the half this worker controls.
  //
  // The other half is the SERVER's, and it is where the bug lived: this fetch
  // is subject to the browser's own HTTP cache, so an `index.html` served
  // without `Cache-Control` gets heuristic freshness (~10% of its age) and can
  // come back stale for days after a deploy — whereupon the `cache.put` below
  // wrote that stale document into the shell cache too. Both Caddy configs now
  // send `no-cache` on the SPA handle, which is what makes this fetch a
  // revalidation (`tests/test_dashboard_asset_fallback.py` pins it).
  //
  // Deliberately NOT `fetch(event.request, { cache: "no-cache" })`, which
  // looks like it would force the issue from here. Per the Fetch spec a
  // non-empty `init` downgrades a request whose mode is "navigate" to
  // "same-origin", changing redirect handling on the one request that must
  // never break — and Workbox drops fetchOptions on navigations for the same
  // reason. A stale shell is recoverable (see `hardReset`, which cache-busts
  // the URL explicitly); a navigation that fails to redirect is not.
  try {
    const response = await fetch(event.request);
    // A real 200 HTML document, and nothing else — see isHtml above.
    if (response && response.ok && response.type === "basic" && isHtml(response)) {
      const copy = response.clone();
      // waitUntil, because iOS terminates the worker as soon as the response
      // is returned. An un-awaited put can be dropped, pinning the shell cache
      // to whatever write last completed — which is how a stale entry outlives
      // the fresh ones that were supposed to replace it.
      event.waitUntil(
        caches
          .open(SHELL_CACHE)
          .then((cache) => cache.put(SHELL_URL, copy))
          .catch(() => {}),
      );
    }
    return response;
  } catch {
    const cached = await caches.match(SHELL_URL, { cacheName: SHELL_CACHE });
    if (cached) return cached;
    // Offline with nothing warmed yet: let the browser render its own error
    // rather than inventing one.
    return Response.error();
  }
}

async function handleHashedAsset(event) {
  const request = event.request;
  // Cache-first is safe here and only here: the filename carries a hash of the
  // bytes, so an entry can be stale only by being unreachable.
  const cached = await caches.match(request, { cacheName: ASSET_CACHE });
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.ok) {
    const copy = response.clone();
    // waitUntil for the same reason the shell write uses it: iOS kills the
    // worker once the response is returned, and a dropped write here means the
    // asset is silently re-fetched on every load instead of being cached.
    event.waitUntil(
      caches
        .open(ASSET_CACHE)
        .then((cache) => cache.put(request, copy))
        .catch(() => {}),
    );
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Anything that isn't a plain same-origin GET is the browser's business.
  // Returning without respondWith is not the same as passing the request
  // through fetch() — it takes the worker out of the path entirely.
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch {
    return;
  }
  if (url.origin !== self.location.origin) return;
  if (BYPASS.some((prefix) => url.pathname.startsWith(prefix))) return;

  // Hashed assets are matched BEFORE the navigation branch, and the order is
  // load-bearing. A navigation whose URL is under /assets/ — opening a script
  // URL directly, an "open link" on an asset — would otherwise be treated as a
  // document, and its JavaScript body written into the shell cache under
  // /index.html. Every later offline navigation would then serve JavaScript as
  // the document. Rare, and unrecoverable without a cache purge when it lands.
  if (isHashedAsset(url)) {
    event.respondWith(handleHashedAsset(event));
    return;
  }

  // A share-target hand-off carries the shared text in the query string; it
  // must reach the app fresh, never out of a cache keyed on a different share.
  if (request.mode === "navigate") {
    event.respondWith(handleNavigation(event));
  }
  // Everything else same-origin (icons, manifest, /assets/theme1/…) falls
  // through to the network untouched. They are small, already carry sensible
  // Cache-Control from the edge, and are not worth the staleness risk.
});
