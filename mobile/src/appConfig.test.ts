import assert from "node:assert/strict";
import { test } from "node:test";

import {
  APP_CONFIG_KEY,
  APP_CONFIG_URL,
  BUNDLED_CONFIG,
  FRESH_MS,
  compareVersions,
  currentAppConfig,
  demoOrigin,
  needsUpdate,
  parseAppConfig,
  refreshAppConfig,
} from "./appConfig.ts";

function memoryStore() {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
  };
}

const SERVED = {
  version: 1,
  demo: {
    enabled: true,
    url: "https://demo.olano.run",
    label: "Try the demo",
    accountHint: "A demo account of your own.",
  },
  minSupportedVersion: "1.0.0",
};

function serving(body: unknown, status = 200) {
  const calls: string[] = [];
  const fetchText = async (url: string) => {
    calls.push(url);
    return { status, body: typeof body === "string" ? body : JSON.stringify(body) };
  };
  return { calls, fetchText };
}

test("the bundled config offers the demo, so a first launch offline still shows it", () => {
  assert.equal(BUNDLED_CONFIG.demo.enabled, true);
  assert.equal(demoOrigin(BUNDLED_CONFIG.demo.url), "https://demo.olano.run");
  assert.equal(BUNDLED_CONFIG.minSupportedVersion, null);
});

test("a served config is read, unknown fields ignored", () => {
  const config = parseAppConfig({ ...SERVED, features: { secret: true } });
  assert.deepEqual(config, {
    demo: { enabled: true, url: "https://demo.olano.run", label: "Try the demo", accountHint: "A demo account of your own." },
    minSupportedVersion: "1.0.0",
  });
  // Nothing beyond the demo entry and the version floor survives parsing:
  // the config cannot carry a feature switch.
  assert.deepEqual(Object.keys(config ?? {}).sort(), ["demo", "minSupportedVersion"]);
});

test("demo.enabled false hides the entry", () => {
  const config = parseAppConfig({ demo: { ...SERVED.demo, enabled: false } });
  assert.equal(config?.demo.enabled, false);
});

test("the demo may only point at an https origin on Olano's own domains", () => {
  assert.equal(demoOrigin("https://demo.olano.run"), "https://demo.olano.run");
  assert.equal(demoOrigin("https://demo.olano.run/"), "https://demo.olano.run");
  assert.equal(demoOrigin("https://try.olano.ai"), "https://try.olano.ai");
  assert.equal(demoOrigin("http://demo.olano.run"), null);
  assert.equal(demoOrigin("https://demo.olano.run.evil.example"), null);
  assert.equal(demoOrigin("https://evilolano.run"), null);
  assert.equal(demoOrigin("https://demo.olano.run/chat"), null);
  assert.equal(demoOrigin("https://user:pw@demo.olano.run"), null);
  assert.equal(demoOrigin("javascript:alert(1)"), null);
  assert.equal(demoOrigin(42), null);
});

test("a wrong demo field turns the entry off instead of guessing", () => {
  assert.equal(parseAppConfig({ demo: { ...SERVED.demo, url: "https://example.com" } })?.demo.enabled, false);
  assert.equal(parseAppConfig({ demo: { ...SERVED.demo, label: "" } })?.demo.enabled, false);
  assert.equal(parseAppConfig({ demo: { ...SERVED.demo, label: "x".repeat(41) } })?.demo.enabled, false);
  assert.equal(parseAppConfig({ demo: { ...SERVED.demo, enabled: "yes" } })?.demo.enabled, false);
  // Omitted texts fall back to the bundled wording.
  const minimal = parseAppConfig({ demo: { enabled: true, url: "https://demo.olano.run" } });
  assert.equal(minimal?.demo.enabled, true);
  assert.equal(minimal?.demo.label, BUNDLED_CONFIG.demo.label);
});

test("something that is not a config is refused", () => {
  assert.equal(parseAppConfig(null), null);
  assert.equal(parseAppConfig([]), null);
  assert.equal(parseAppConfig("demo"), null);
  assert.equal(parseAppConfig({ demo: "on" }), null);
  // No demo block at all: the entry is off.
  assert.equal(parseAppConfig({})?.demo.enabled, false);
});

test("a malformed version floor is ignored", () => {
  assert.equal(parseAppConfig({ ...SERVED, minSupportedVersion: "latest" })?.minSupportedVersion, null);
  assert.equal(parseAppConfig({ ...SERVED, minSupportedVersion: " 1.2 " })?.minSupportedVersion, "1.2");
});

test("refresh fetches, keeps the answer and serves it next launch", async () => {
  const store = memoryStore();
  const { calls, fetchText } = serving({ ...SERVED, demo: { ...SERVED.demo, enabled: false } });
  const config = await refreshAppConfig({ fetchText, store, now: 1_000 });
  assert.deepEqual(calls, [APP_CONFIG_URL]);
  assert.equal(config.demo.enabled, false);
  assert.ok(store.data.has(APP_CONFIG_KEY));
  assert.equal(currentAppConfig(store).demo.enabled, false);
});

test("a fresh copy is not fetched again; a stale one is", async () => {
  const store = memoryStore();
  await refreshAppConfig({ fetchText: serving(SERVED).fetchText, store, now: 1_000 });
  const again = serving(SERVED);
  await refreshAppConfig({ fetchText: again.fetchText, store, now: 1_000 + FRESH_MS - 1 });
  assert.equal(again.calls.length, 0);
  await refreshAppConfig({ fetchText: again.fetchText, store, now: 1_000 + FRESH_MS });
  assert.equal(again.calls.length, 1);
  // force ignores the window.
  await refreshAppConfig({ fetchText: again.fetchText, store, now: 1_000 + FRESH_MS, force: true });
  assert.equal(again.calls.length, 2);
});

test("a clock that went backwards refetches rather than trusting the copy forever", async () => {
  const store = memoryStore();
  await refreshAppConfig({ fetchText: serving(SERVED).fetchText, store, now: 10_000_000 });
  const again = serving(SERVED);
  await refreshAppConfig({ fetchText: again.fetchText, store, now: 5_000 });
  assert.equal(again.calls.length, 1);
});

test("offline, an error status or a broken body keeps the last good copy", async () => {
  const store = memoryStore();
  await refreshAppConfig({ fetchText: serving({ ...SERVED, demo: { ...SERVED.demo, enabled: false } }).fetchText, store, now: 1 });
  const later = 1 + FRESH_MS;
  const failing = [
    async () => {
      throw new Error("offline");
    },
    serving("{}", 503).fetchText,
    serving("<html>", 200).fetchText,
    serving({ demo: "on" }, 200).fetchText,
  ];
  for (const fetchText of failing) {
    const config = await refreshAppConfig({ fetchText, store, now: later });
    assert.equal(config.demo.enabled, false, "kept copy, not the bundled default");
  }
});

test("with nothing kept, a failure falls back to the bundled config", async () => {
  const config = await refreshAppConfig({
    fetchText: async () => {
      throw new Error("timeout");
    },
    store: memoryStore(),
    now: 1,
  });
  assert.deepEqual(config, BUNDLED_CONFIG);
});

test("a store that throws or holds junk is survived", async () => {
  const broken = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
  };
  assert.deepEqual(currentAppConfig(broken), BUNDLED_CONFIG);
  const config = await refreshAppConfig({ fetchText: serving(SERVED).fetchText, store: broken, now: 1 });
  assert.equal(config.demo.enabled, true);
  const junk = memoryStore();
  junk.data.set(APP_CONFIG_KEY, "{not json");
  assert.deepEqual(currentAppConfig(junk), BUNDLED_CONFIG);
  junk.data.set(APP_CONFIG_KEY, JSON.stringify({ config: SERVED }));
  assert.deepEqual(currentAppConfig(junk), BUNDLED_CONFIG, "no fetchedAt: not a copy this code wrote");
});

test("versions compare numerically", () => {
  assert.ok(compareVersions("1.0.0", "1.0.1") < 0);
  assert.ok(compareVersions("1.10.0", "1.9.9") > 0);
  assert.equal(compareVersions("1.2", "1.2.0"), 0);
});

test("an app older than the floor needs an update; no floor, a dev build or a newer app does not", () => {
  const floor = { ...BUNDLED_CONFIG, minSupportedVersion: "1.2.0" };
  assert.equal(needsUpdate("1.1.9", floor), true);
  assert.equal(needsUpdate("1.2.0", floor), false);
  assert.equal(needsUpdate("2.0.0", floor), false);
  assert.equal(needsUpdate("dev", floor), false);
  assert.equal(needsUpdate("1.0.0", BUNDLED_CONFIG), false);
});
