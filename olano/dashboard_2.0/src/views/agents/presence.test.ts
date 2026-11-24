// Unit tests for the agent-list presence rules. Run with `npm run test:unit`.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  asleepShown,
  asleepToggleShown,
  bandOf,
  isAsleep,
  moveWithinBands,
  pipTone,
  presenceOf,
  splitAsleep,
  type Presence,
} from "./presence.ts";

describe("presenceOf", () => {
  it("reads a booted, unpaused agent as online", () => {
    assert.equal(presenceOf({ running: true, status: "running" }), "online");
    // An older roster row with no status at all.
    assert.equal(presenceOf({ running: true }), "online");
  });
  it("reads paused as paused even while the runner is still booted", () => {
    // The roster reports `running: true` for a paused agent that is booted.
    assert.equal(presenceOf({ running: true, paused: true, status: "running" }), "paused");
    assert.equal(presenceOf({ running: false, paused: true, status: "paused" }), "paused");
  });
  it("reads configured and stopped agents as offline", () => {
    assert.equal(presenceOf({ running: false, status: "configured" }), "offline");
    assert.equal(presenceOf({ running: false, status: "stopped" }), "offline");
    assert.equal(presenceOf({}), "offline");
  });
  it("puts a failed boot and a deployment block ahead of paused", () => {
    assert.equal(presenceOf({ running: false, paused: true, status: "error" }), "error");
    assert.equal(presenceOf({ running: false, blocked: true, status: "blocked" }), "blocked");
    assert.equal(presenceOf({ running: false, blocked: true, status: "configured" }), "blocked");
  });
  it("keeps the transitions distinct", () => {
    assert.equal(presenceOf({ running: false, status: "starting" }), "starting");
    assert.equal(presenceOf({ running: true, status: "stopping" }), "stopping");
  });
});

describe("what goes behind the fold", () => {
  it("only paused and offline", () => {
    const all: Presence[] = ["online", "starting", "stopping", "error", "blocked", "paused", "offline"];
    assert.deepEqual(all.filter(isAsleep), ["paused", "offline"]);
  });
  it("gives amber to transitions and blocks, red to failures, grey to sleepers", () => {
    assert.equal(pipTone("online"), "online");
    assert.equal(pipTone("starting"), "warn");
    assert.equal(pipTone("stopping"), "warn");
    assert.equal(pipTone("blocked"), "warn");
    assert.equal(pipTone("error"), "error");
    assert.equal(pipTone("paused"), undefined);
    assert.equal(pipTone("offline"), undefined);
  });
});

describe("splitAsleep", () => {
  const state: Record<string, Presence> = {
    a: "online",
    b: "paused",
    c: "error",
    d: "offline",
    e: "online",
  };
  const p = (n: string) => state[n];

  it("keeps each band in the reader's order", () => {
    assert.deepEqual(splitAsleep(["a", "b", "c", "d", "e"], p), {
      awake: ["a", "c", "e"],
      asleep: ["b", "d"],
    });
  });
  it("keeps the open agent and unread agents above the fold", () => {
    assert.deepEqual(splitAsleep(["a", "b", "c", "d", "e"], p, (n) => n === "d"), {
      awake: ["a", "c", "d", "e"],
      asleep: ["b"],
    });
  });
});

describe("fold guards", () => {
  it("is closed by default and opens on the stored choice", () => {
    assert.equal(asleepShown({ storedOpen: false, awakeCount: 2, filtering: false }), false);
    assert.equal(asleepShown({ storedOpen: true, awakeCount: 2, filtering: false }), true);
  });
  it("draws the rows flat when nothing is above the fold", () => {
    assert.equal(asleepShown({ storedOpen: false, awakeCount: 0, filtering: false }), true);
    assert.equal(asleepToggleShown({ asleepCount: 4, awakeCount: 0, filtering: false }), false);
  });
  it("ignores the fold while a search filter is on", () => {
    assert.equal(asleepShown({ storedOpen: false, awakeCount: 3, filtering: true }), true);
    assert.equal(asleepToggleShown({ asleepCount: 2, awakeCount: 3, filtering: true }), false);
  });
  it("draws no toggle with nothing to disclose", () => {
    assert.equal(asleepToggleShown({ asleepCount: 0, awakeCount: 3, filtering: false }), false);
    assert.equal(asleepToggleShown({ asleepCount: 2, awakeCount: 3, filtering: false }), true);
  });
});

describe("moveWithinBands", () => {
  const awake = ["a", "b", "c"];
  const asleep = ["x", "y"];

  it("moves inside a band", () => {
    assert.deepEqual(moveWithinBands(awake, asleep, "a", 2), ["b", "a", "c", "x", "y"]);
    assert.deepEqual(moveWithinBands(awake, asleep, "y", 3), ["a", "b", "c", "y", "x"]);
  });
  it("rests at the band's edge when dragged past it", () => {
    // "a" dropped among the sleepers comes to rest at the end of the awake band.
    assert.deepEqual(moveWithinBands(awake, asleep, "a", 5), ["b", "c", "a", "x", "y"]);
    // "x" dropped at the very top rests at the head of the asleep tail.
    assert.deepEqual(moveWithinBands(awake, asleep, "x", 0), ["a", "b", "c", "x", "y"]);
  });
  it("a one-step move down from a band's last row is a no-op", () => {
    assert.deepEqual(moveWithinBands(awake, asleep, "c", 2 + 2), ["a", "b", "c", "x", "y"]);
  });
  it("bandOf names the band a row is drawn in", () => {
    assert.equal(bandOf(awake, asleep, "b"), awake);
    assert.equal(bandOf(awake, asleep, "y"), asleep);
    assert.equal(bandOf(awake, asleep, "nobody"), null);
  });
});

describe("malformed roster rows", () => {
  it("never throw, and only an explicit true blocks", () => {
    const junk = { status: 7, blocked: "yes", blocked_reason: {} } as unknown as Parameters<typeof presenceOf>[0];
    assert.equal(presenceOf(junk), "offline");
  });
});
