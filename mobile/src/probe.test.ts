import assert from "node:assert/strict";
import { test } from "node:test";

import { describeFailure, isOlanoStatus, probeDeployment } from "./probe.ts";

const ORIGIN = "https://acme.olano.run";

/** The fields a real runtime returns (olano/runtime/routes/status.py), trimmed. */
const STATUS = {
  status: "running",
  booting: false,
  setup_complete: true,
  agents: [],
  agents_total: null,
  channels: [],
  scheduler_jobs: 0,
  uptime: "2026-10-06T10:00:00+00:00",
  capabilities: { image_gen: true },
  custody: { mode: "none" },
  runtime_version: "git-abc1234",
  gateway: "enrolled",
};

function respond(body: unknown, init: ResponseInit = {}, url = `${ORIGIN}/api/status`): typeof fetch {
  return (async () => {
    const response = new Response(typeof body === "string" ? body : JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
      ...init,
    });
    Object.defineProperty(response, "url", { value: url });
    return response;
  }) as typeof fetch;
}

const online = () => true;

test("recognises a runtime status payload", () => {
  assert.equal(isOlanoStatus(STATUS), true);
  assert.equal(isOlanoStatus({ status: "ok" }), false);
  assert.equal(isOlanoStatus({ status: "running", agents: [], capabilities: [] }), false);
  assert.equal(isOlanoStatus([]), false);
  assert.equal(isOlanoStatus(null), false);
});

test("accepts an Olano deployment", async () => {
  const result = await probeDeployment(ORIGIN, { fetchImpl: respond(STATUS), isOnline: online });
  assert.deepEqual(result, { ok: true, origin: ORIGIN });
});

test("follows the origin a redirect landed on, never downgrading to http", async () => {
  const moved = await probeDeployment("https://example.com", {
    fetchImpl: respond(STATUS, {}, "https://www.example.com/api/status"),
    isOnline: online,
  });
  assert.deepEqual(moved, { ok: true, origin: "https://www.example.com" });

  const downgraded = await probeDeployment("https://example.com", {
    fetchImpl: respond(STATUS, {}, "http://example.com/api/status"),
    isOnline: online,
  });
  assert.deepEqual(downgraded, { ok: true, origin: "https://example.com" });
});

test("a website that is not Olano", async () => {
  const html = await probeDeployment(ORIGIN, {
    fetchImpl: respond("<html></html>", { headers: { "Content-Type": "text/html" } }),
    isOnline: online,
  });
  assert.deepEqual(html, { ok: false, reason: "not_olano", status: 200 });

  const missing = await probeDeployment(ORIGIN, { fetchImpl: respond({}, { status: 404 }), isOnline: online });
  assert.deepEqual(missing, { ok: false, reason: "not_olano", status: 404 });

  const other = await probeDeployment(ORIGIN, { fetchImpl: respond({ status: "ok" }), isOnline: online });
  assert.equal(other.ok, false);
});

test("a front door with no runtime behind it", async () => {
  const result = await probeDeployment(ORIGIN, { fetchImpl: respond("", { status: 502 }), isOnline: online });
  assert.deepEqual(result, { ok: false, reason: "unavailable", status: 502 });
});

test("nothing answering, offline and timeouts", async () => {
  const refused = (async () => {
    throw new TypeError("Failed to fetch");
  }) as typeof fetch;
  assert.deepEqual(await probeDeployment(ORIGIN, { fetchImpl: refused, isOnline: online }), {
    ok: false,
    reason: "not_found",
  });
  assert.deepEqual(await probeDeployment(ORIGIN, { fetchImpl: refused, isOnline: () => false }), {
    ok: false,
    reason: "offline",
  });

  const hangs = ((_input: RequestInfo | URL, init?: RequestInit) =>
    new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })) as typeof fetch;
  assert.deepEqual(await probeDeployment(ORIGIN, { fetchImpl: hangs, isOnline: online, timeoutMs: 20 }), {
    ok: false,
    reason: "timeout",
  });
});

test("every failure has a message naming the host", () => {
  for (const reason of ["offline", "not_found", "not_olano", "unavailable", "timeout"] as const) {
    const text = describeFailure("acme.olano.run", { reason, status: 503 });
    assert.ok(text.length > 20, reason);
    if (reason !== "offline") assert.match(text, /acme\.olano\.run/, reason);
  }
});
