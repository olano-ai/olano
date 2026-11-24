// Unit tests for public/sw.js. Run with `npm run test:unit` (Node's built-in
// runner with type stripping — no test framework in the bundle).
//
// Why this file exists at all: a service worker is the one asset that cannot be
// fixed by deploying, because it decides whether the next request reaches the
// server. Its four rules are written at the top of public/sw.js; these tests
// hold three of them that are cheap to get wrong in a later edit:
//
//   * /api/** is never intercepted (chat is SSE, status is polled, responses
//     are per-user and auth-gated).
//   * The HTML shell is never served cache-first — it names the current asset
//     hashes, so a stale one strands a tab on chunks the server deleted.
//   * Only content-hashed assets are cached long-term.
//
// The worker runs as a real script under node:vm against a fake global scope,
// so what is tested is the shipped file, not a copy of its logic.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { describe, it } from "node:test";

const SW_PATH = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "public", "sw.js");
const ORIGIN = "https://box.example";

type Listeners = Record<string, ((event: unknown) => void)[]>;

interface FetchCall {
  url: string;
  init?: { cache?: string };
}

/** What the worker did, recorded across one test. */
interface Recorder {
  listeners: Listeners;
  fetches: FetchCall[];
  /** Cache name → key → stored body. Mirrors the real CacheStorage closely
   *  enough to tell "the shell was written" from "it was not". */
  caches: Map<string, Map<string, string>>;
  deletedCaches: string[];
}

/**
 * Run public/sw.js against a fake worker scope that RECORDS what it did.
 *
 * `response` builds the reply the worker's `fetch` returns, so a test can hand
 * it an HTML 200, a JavaScript 200 or a 404 and watch which of those the worker
 * is willing to store as the app shell.
 */
function loadWorker(
  response: (url: string) => Response = () => htmlResponse("<!doctype html>"),
): Recorder {
  const listeners: Listeners = {};
  const rec: Recorder = { listeners, fetches: [], caches: new Map(), deletedCaches: [] };

  const cacheFor = (name: string) => {
    let c = rec.caches.get(name);
    if (!c) rec.caches.set(name, (c = new Map()));
    return c;
  };
  const keyOf = (request: unknown): string =>
    typeof request === "string" ? request : String((request as { url?: string }).url ?? request);

  const self: Record<string, unknown> = {
    location: new URL(ORIGIN + "/sw.js"),
    addEventListener(type: string, fn: (event: unknown) => void) {
      (listeners[type] ||= []).push(fn);
    },
    registration: {
      // Present but disabled — the worker turns preload OFF (its request would
      // honour the HTTP cache, which is the staleness handleNavigation exists
      // to defeat), so this records that it asked.
      navigationPreload: { enable: async () => {}, disable: async () => {} },
      unregister: async () => true,
    },
    clients: { claim: async () => {}, matchAll: async () => [] },
    skipWaiting: async () => {},
  };

  const context = vm.createContext({
    self,
    caches: {
      keys: async () => [...rec.caches.keys()],
      delete: async (name: string) => {
        rec.deletedCaches.push(name);
        return rec.caches.delete(name);
      },
      open: async (name: string) => ({
        put: async (key: unknown, res: Response) => {
          cacheFor(name).set(keyOf(key), await res.text());
        },
        add: async () => {},
      }),
      match: async (key: unknown, opts?: { cacheName?: string }) => {
        const store = opts?.cacheName ? rec.caches.get(opts.cacheName) : undefined;
        const body = store?.get(keyOf(key));
        return body === undefined ? undefined : htmlResponse(body);
      },
    },
    fetch: async (request: unknown, init?: { cache?: string }) => {
      const url = keyOf(request);
      rec.fetches.push({ url, init });
      return response(url);
    },
    URL,
    Response,
    Request,
    Promise,
    console,
  });
  // `self` is the worker global, so its own properties have to be reachable as
  // bare identifiers too (the script says `caches`, not `self.caches`).
  vm.runInContext(readFileSync(SW_PATH, "utf8"), context);
  return rec;
}

/**
 * Build a response the worker will treat as same-origin.
 *
 * `Response.type` is a read-only getter that Node's constructor sets to
 * "default"; a real same-origin fetch in a browser reports "basic", which is
 * what sw.js checks before storing anything as the shell (it is how an opaque
 * cross-origin response is excluded). Without this stamp every caching
 * assertion here would pass vacuously, because nothing would ever be stored.
 */
function basicResponse(body: string, contentType: string, status = 200): Response {
  const res = new Response(body, { status, headers: { "Content-Type": contentType } });
  Object.defineProperty(res, "type", { value: "basic" });
  return res;
}

/** A 200 that looks like the app shell to the worker's content-type check. */
function htmlResponse(body: string): Response {
  return basicResponse(body, "text/html; charset=utf-8");
}

/** A same-origin 200 that is emphatically NOT a document. */
function scriptResponse(): Response {
  return basicResponse("export default 1", "application/javascript");
}

/** Drive one fetch event to completion and return the response it produced. */
async function dispatchFetch(
  rec: Recorder,
  url: string,
  init: { method?: string; mode?: string } = {},
): Promise<Response | undefined> {
  const handler = rec.listeners.fetch?.[0];
  assert.ok(handler, "sw.js registered no fetch listener");
  let promise: Promise<Response> | undefined;
  const waits: Promise<unknown>[] = [];
  handler({
    request: { url, method: init.method ?? "GET", mode: init.mode ?? "no-cors" },
    preloadResponse: Promise.resolve(undefined),
    respondWith(p: Promise<Response>) {
      promise = p;
    },
    waitUntil(p: Promise<unknown>) {
      waits.push(p);
    },
  });
  const response = promise ? await promise : undefined;
  // The shell write rides waitUntil, so a test that asserts on the cache has to
  // let it settle — exactly as the browser does before killing the worker.
  await Promise.all(waits);
  return response;
}

/** Deliver a message event and settle whatever it kicked off. */
async function dispatchMessage(rec: Recorder, data: unknown, port?: { postMessage(v: unknown): void }) {
  const handler = rec.listeners.message?.[0];
  assert.ok(handler, "sw.js registered no message listener");
  const waits: Promise<unknown>[] = [];
  handler({ data, ports: port ? [port] : [], waitUntil: (p: Promise<unknown>) => waits.push(p) });
  await Promise.all(waits);
}

/** Dispatch a fetch event and report whether the worker claimed the request. */
function intercepts(
  rec: Recorder,
  url: string,
  init: { method?: string; mode?: string } = {},
): boolean {
  const handler = rec.listeners.fetch?.[0];
  assert.ok(handler, "sw.js registered no fetch listener");
  let claimed = false;
  handler({
    request: { url, method: init.method ?? "GET", mode: init.mode ?? "no-cors" },
    preloadResponse: Promise.resolve(undefined),
    respondWith(promise: Promise<unknown>) {
      claimed = true;
      // The handler runs regardless; swallow it so an unstubbed cache call in
      // the response path cannot fail the assertion about routing.
      void Promise.resolve(promise).catch(() => {});
    },
    waitUntil() {},
  });
  return claimed;
}

describe("service worker routing", () => {
  // These are asserted as NAVIGATIONS on purpose. A sub-resource GET to /api is
  // already ignored by the conservative fall-through at the end of the handler,
  // so asserting that proves nothing about BYPASS — the list is load-bearing
  // exactly where the request would otherwise be claimed, which is a top-level
  // navigation. Those are real: opening a file link (/api/files-raw/…), landing
  // on an OAuth metadata URL, or visiting an agent-published page. Serving any
  // of them the SPA shell out of cache would replace someone else's document
  // with the dashboard.
  it("never claims a navigation to the runtime API", () => {
    const sw = loadWorker();
    for (const path of ["/api/status", "/api/chat/stream", "/api/files-raw/abc.png"]) {
      assert.equal(intercepts(sw, ORIGIN + path, { mode: "navigate" }), false, path);
    }
  });

  it("never claims a navigation to the other self-authenticating ingress paths", () => {
    const sw = loadWorker();
    for (const path of ["/pages/my-page/", "/.well-known/oauth-authorization-server"]) {
      assert.equal(intercepts(sw, ORIGIN + path, { mode: "navigate" }), false, path);
    }
  });

  it("claims navigations so the shell has an offline fallback", () => {
    const sw = loadWorker();
    assert.equal(intercepts(sw, ORIGIN + "/chat", { mode: "navigate" }), true);
  });

  it("leaves every other same-origin sub-resource on the network", () => {
    // The conservative default: anything not a navigation and not a hashed
    // asset falls through untouched. Pinned so a later "just cache the icons
    // too" edit has to be deliberate rather than incidental.
    const sw = loadWorker();
    for (const path of ["/api/status", "/manifest.json", "/icon-192.png", "/favicon.svg"]) {
      assert.equal(intercepts(sw, ORIGIN + path), false, path);
    }
  });

  it("claims content-hashed assets but not the hand-managed theme subtree", () => {
    const sw = loadWorker();
    assert.equal(intercepts(sw, ORIGIN + "/assets/index-GV0pmqCx.js"), true);
    // theme1/ is not content-hashed, so a cache-first entry there would be
    // stale rather than merely unreachable.
    assert.equal(intercepts(sw, ORIGIN + "/assets/theme1/skin.css"), false);
  });

  it("ignores non-GET requests and other origins", () => {
    const sw = loadWorker();
    assert.equal(intercepts(sw, ORIGIN + "/chat", { method: "POST", mode: "navigate" }), false);
    assert.equal(intercepts(sw, "https://fonts.googleapis.com/css2?family=Inter"), false);
  });
});

// ── the stale-build trap ─────────────────────────────────────────────────────
// These hold the fixes for the defect where an installed app could serve a
// dead build forever and its own Reload button did nothing. Each one failed
// before the fix; see the comments in public/sw.js for the mechanism.

describe("service worker staleness", () => {
  it("never serves the cached shell while the network is answering", async () => {
    // Rule 2, and the half the worker controls. The cached shell is an OFFLINE
    // fallback only — if it could satisfy a navigation while online, a deploy
    // would be invisible to an installed app forever.
    //
    // (The worker deliberately does not pass `{ cache: "no-cache" }` here: a
    // non-empty init downgrades a navigate-mode request to same-origin. Making
    // the HTTP cache revalidate is the server's `Cache-Control: no-cache`,
    // pinned in tests/test_dashboard_asset_fallback.py.)
    const sw = loadWorker();
    await dispatchFetch(sw, ORIGIN + "/chat", { mode: "navigate" });
    assert.ok(sw.caches.get("olano-shell-v2")?.has("/index.html"), "precondition: shell cached");

    const before = sw.fetches.length;
    await dispatchFetch(sw, ORIGIN + "/agents", { mode: "navigate" });
    assert.equal(
      sw.fetches.length,
      before + 1,
      "a second navigation must go to the network, not be answered from the shell cache",
    );
  });

  it("falls back to the cached shell only when the network fails", async () => {
    let online = true;
    const sw = loadWorker(() => {
      if (!online) throw new Error("offline");
      return htmlResponse("<!doctype html><title>cached</title>");
    });
    await dispatchFetch(sw, ORIGIN + "/chat", { mode: "navigate" });

    online = false;
    const response = await dispatchFetch(sw, ORIGIN + "/agents", { mode: "navigate" });
    assert.ok(response, "offline navigation produced no response at all");
    assert.match(await response.text(), /cached/, "the offline fallback must serve the warmed shell");
  });

  it("stores an HTML document as the shell, and refuses anything else", async () => {
    const html = loadWorker(() => htmlResponse("<!doctype html><title>app</title>"));
    await dispatchFetch(html, ORIGIN + "/chat", { mode: "navigate" });
    assert.ok(
      html.caches.get("olano-shell-v2")?.has("/index.html"),
      "a real HTML 200 should refresh the offline shell",
    );

    // A 200 that is not a document must never become the shell: an error page,
    // an auth redirect body, or — the catastrophic one — JavaScript, which
    // would then be served AS the document to every later offline navigation.
    const js = loadWorker(scriptResponse);
    await dispatchFetch(js, ORIGIN + "/chat", { mode: "navigate" });
    assert.equal(
      js.caches.get("olano-shell-v2")?.has("/index.html"),
      undefined,
      "a non-HTML 200 must not be stored as the app shell",
    );
  });

  it("treats a navigation to a hashed asset as an asset, never as the document", async () => {
    // Ordering: isHashedAsset is matched before the navigate branch. Opening a
    // script URL directly would otherwise write its JavaScript body into the
    // shell cache under /index.html.
    const sw = loadWorker(scriptResponse);
    const url = ORIGIN + "/assets/index-abc123.js";
    await dispatchFetch(sw, url, { mode: "navigate" });

    // It must never land in the shell cache. (The isHtml guard also blocks
    // that, so this alone would pass with the branches in either order — it is
    // asserted because it is the consequence that matters.)
    assert.equal(
      sw.caches.get("olano-shell-v2")?.has("/index.html"),
      undefined,
      "a navigation under /assets/ must not be able to overwrite the shell",
    );
    // THIS is the ordering assertion: handled as an asset, it is stored in the
    // asset cache. Handled as a document it would not be, whatever else the
    // document path decided to do with it.
    assert.ok(
      sw.caches.get("olano-assets-v2")?.has(url),
      "a hashed asset must be matched before the navigate branch, and cached as an asset",
    );
  });

  it("purges every cache on HARD_RESET and confirms it, so the page can reload", async () => {
    // core/pwa.ts `hardReset()` waits for this reply before reloading. Without
    // the purge the reload is answered from the same caches that just failed —
    // which is why the in-app Reload button appeared to do nothing.
    const sw = loadWorker();
    await dispatchFetch(sw, ORIGIN + "/chat", { mode: "navigate" });
    assert.ok(sw.caches.size > 0, "precondition: something is cached");

    const replies: unknown[] = [];
    await dispatchMessage(sw, { type: "HARD_RESET" }, { postMessage: (v) => replies.push(v) });

    assert.equal(sw.caches.size, 0, "HARD_RESET must leave no cache behind");
    // Shape, not deepEqual: the reply object is constructed inside the vm
    // realm, so its prototype differs and deepStrictEqual rejects it.
    assert.equal(replies.length, 1, "the page waits for exactly one confirmation");
    assert.equal((replies[0] as { ok?: boolean }).ok, true);
  });
});
