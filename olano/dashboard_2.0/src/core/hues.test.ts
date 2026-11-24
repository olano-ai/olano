import assert from "node:assert/strict";
import test from "node:test";

import { HUE_SLOTS, hueSlotFor, hueVarFor } from "./hues.ts";

test("a name always maps into the ramp", () => {
  const names = [
    "kaki-ingest",
    "sqlite",
    "scheduler",
    "observability",
    "telegram",
    "actions",
    "multi-user",
    "cost",
    "",
    "  ",
    "a".repeat(400),
    "ünïcødé-läbel",
    "🙂",
  ];
  for (const name of names) {
    assert.ok(HUE_SLOTS.includes(hueSlotFor(name)), `${name} fell outside the ramp`);
    assert.equal(hueVarFor(name), `var(--hue-${hueSlotFor(name)})`);
  }
});

test("the mapping is stable across calls", () => {
  for (const name of ["kaki-ingest", "scheduler", "cost"]) {
    assert.equal(hueSlotFor(name), hueSlotFor(name));
  }
});

test("the ramp spreads names rather than piling them on one slot", () => {
  // A modulus that collapses (e.g. accumulating without a prime bound) shows up
  // here as a handful of slots carrying everything.
  const used = new Set<string>();
  for (let i = 0; i < 200; i++) used.add(hueSlotFor(`label-${i}`));
  assert.ok(used.size >= 10, `only ${used.size} of ${HUE_SLOTS.length} slots used`);
});

test("every slot is reachable", () => {
  const used = new Set<string>();
  for (let i = 0; i < 5000; i++) used.add(hueSlotFor(`x${i}`));
  assert.equal(used.size, HUE_SLOTS.length);
});
