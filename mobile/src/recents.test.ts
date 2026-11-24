import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_RECENTS, RECENTS_KEY, describeWhen, forgetDeployment, loadRecents, rememberDeployment } from "./recents.ts";

function memoryStore(initial?: string) {
  const data = new Map<string, string>();
  if (initial !== undefined) data.set(RECENTS_KEY, initial);
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
  };
}

test("newest first, no duplicates, at most five", () => {
  const store = memoryStore();
  for (let i = 0; i < 7; i++) rememberDeployment(`https://d${i}.olano.run`, 1000 + i, store);
  rememberDeployment("https://d3.olano.run", 5000, store);
  const list = loadRecents(store);
  assert.equal(list.length, MAX_RECENTS);
  assert.deepEqual(
    list.map((item) => item.host),
    ["d3.olano.run", "d6.olano.run", "d5.olano.run", "d4.olano.run", "d2.olano.run"],
  );
});

test("forgetting removes one entry", () => {
  const store = memoryStore();
  rememberDeployment("https://a.olano.run", 1, store);
  rememberDeployment("https://b.olano.run", 2, store);
  assert.deepEqual(
    forgetDeployment("https://b.olano.run", store).map((item) => item.origin),
    ["https://a.olano.run"],
  );
});

test("a damaged or missing store reads as empty", () => {
  assert.deepEqual(loadRecents(memoryStore("{not json")), []);
  assert.deepEqual(loadRecents(memoryStore('{"a":1}')), []);
  assert.deepEqual(loadRecents(memoryStore('[{"origin":"javascript:alert(1)"}, 3, null]')), []);
  assert.deepEqual(loadRecents(null), []);
  const throwing = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
  };
  assert.deepEqual(loadRecents(throwing), []);
  assert.doesNotThrow(() => rememberDeployment("https://a.olano.run", 1, throwing));
});

test("relative times", () => {
  const now = Date.UTC(2026, 9, 6, 12);
  assert.equal(describeWhen(now - 10_000, now), "just now");
  assert.match(describeWhen(now - 5 * 60_000, now), /5 minutes ago/);
  assert.match(describeWhen(now - 26 * 3600_000, now), /yesterday|1 day ago/);
  assert.equal(describeWhen(0, now), "");
});
