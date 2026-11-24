import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { countsByAgent, nextActiveAfterKill, tabsFor } from "./sessionIndex.ts";

const list = [
  { id: "t1", agent: "merli" },
  { id: "t2", agent: "scout" },
  { id: "t3", agent: "merli" },
  { id: "t4", agent: "merli" },
];

describe("tabsFor", () => {
  it("keeps only one agent's sessions, in order", () => {
    assert.deepEqual(tabsFor(list, "merli").map((s) => s.id), ["t1", "t3", "t4"]);
    assert.deepEqual(tabsFor(list, "scout").map((s) => s.id), ["t2"]);
    assert.deepEqual(tabsFor(list, "nobody"), []);
  });
});

describe("countsByAgent", () => {
  it("counts every tab per agent, whatever its state", () => {
    assert.deepEqual(countsByAgent(list), { merli: 3, scout: 1 });
    assert.deepEqual(countsByAgent([]), {});
  });
});

describe("nextActiveAfterKill", () => {
  const ids = ["a", "b", "c"];
  it("activates the neighbour that slides into the closed slot", () => {
    assert.equal(nextActiveAfterKill(ids, 0), "b");
    assert.equal(nextActiveAfterKill(ids, 1), "c");
  });
  it("clamps to the last tab when the last one was closed", () => {
    assert.equal(nextActiveAfterKill(ids, 2), "b");
  });
  it("returns an empty id when the agent has no tab left — never respawns", () => {
    assert.equal(nextActiveAfterKill(["only"], 0), "");
    assert.equal(nextActiveAfterKill([], 0), "");
  });
});
