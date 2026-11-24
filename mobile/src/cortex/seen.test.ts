import assert from "node:assert/strict";
import { test } from "node:test";

import { remember, SEEN_LIMIT } from "./seen.ts";

test("an opened item goes to the end, once", () => {
  assert.deepEqual(remember([], "nova:a"), ["nova:a"]);
  assert.deepEqual(remember(["nova:a", "nova:b"], "nova:c"), ["nova:a", "nova:b", "nova:c"]);
  assert.deepEqual(remember(["nova:a", "nova:b"], "nova:a"), ["nova:b", "nova:a"]);
});

test("the memory is bounded, oldest first to go", () => {
  const full = Array.from({ length: SEEN_LIMIT }, (_, i) => `k${i}`);
  const next = remember(full, "new");
  assert.equal(next.length, SEEN_LIMIT);
  assert.equal(next[0], "k1");
  assert.equal(next[next.length - 1], "new");
  assert.deepEqual(remember(["a", "b", "c"], "d", 2), ["c", "d"]);
});
