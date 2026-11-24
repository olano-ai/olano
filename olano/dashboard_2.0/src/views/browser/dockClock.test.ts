import { strict as assert } from "node:assert";
import { test } from "node:test";

import { clockText } from "./dockClock.ts";

test("the human-check countdown reads as minutes and seconds", () => {
  assert.equal(clockText(245), "4:05");
  assert.equal(clockText(59.9), "0:59");
  assert.equal(clockText(-3), "0:00");
});
