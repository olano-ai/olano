// Unit tests for the sidebar's bot arrangement. Run with `npm run test:unit`
// (Node's built-in runner with type stripping — no test framework in the
// bundle).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  EMPTY_ARRANGEMENT,
  createGroup,
  deleteGroup,
  homeOf,
  membersOf,
  moveInto,
  moveTo,
  nudge,
  parseArrangement,
  rankedUnpinned,
  renameGroup,
  setOfflineOpen,
  toggleFold,
  togglePin,
  type Arrangement,
} from "./botArrangement.ts";

const AGENTS = ["merli", "aurelius", "curtis", "grantie", "composio"];

function withGroup(name = "Sales"): { a: Arrangement; id: string } {
  const r = createGroup(EMPTY_ARRANGEMENT, name);
  assert.ok("next" in r);
  return { a: r.next, id: r.group.id };
}

describe("moveInto", () => {
  it("inserts a new name at the index", () => {
    assert.deepEqual(moveInto(["a", "b"], "x", 1), ["a", "x", "b"]);
  });
  it("moving down by one actually moves (the off-by-one correction)", () => {
    // The index is measured in the list still containing the row: "put a
    // after b" is index 2 in [a, b, c].
    assert.deepEqual(moveInto(["a", "b", "c"], "a", 2), ["b", "a", "c"]);
  });
  it("moving up uses the raw index", () => {
    assert.deepEqual(moveInto(["a", "b", "c"], "c", 0), ["c", "a", "b"]);
  });
  it("clamps out-of-range indices", () => {
    assert.deepEqual(moveInto(["a", "b"], "b", 99), ["a", "b"]);
    assert.deepEqual(moveInto(["a", "b"], "b", -5), ["b", "a"]);
  });
});

describe("pins", () => {
  it("round-trips", () => {
    const a1 = togglePin(EMPTY_ARRANGEMENT, "merli");
    assert.deepEqual(a1.pins, ["merli"]);
    assert.equal(homeOf(a1, "merli"), "pinned");
    const a2 = togglePin(a1, "merli");
    assert.deepEqual(a2.pins, []);
    assert.equal(homeOf(a2, "merli"), "all");
  });
  it("pinning takes a bot out of its group (one home per bot)", () => {
    const { a, id } = withGroup();
    const inGroup = moveTo(a, "merli", { group: id });
    assert.deepEqual(homeOf(inGroup, "merli"), { group: id });
    const pinned = togglePin(inGroup, "merli");
    assert.equal(homeOf(pinned, "merli"), "pinned");
    assert.deepEqual(membersOf(pinned, { group: id }, AGENTS), []);
  });
  it("only lists pinned bots that still exist", () => {
    const a = togglePin(togglePin(EMPTY_ARRANGEMENT, "ghost"), "merli");
    assert.deepEqual(membersOf(a, "pinned", AGENTS), ["merli"]);
  });
});

describe("ordering", () => {
  it("is alphabetical until someone arranges it", () => {
    assert.deepEqual(rankedUnpinned(EMPTY_ARRANGEMENT, AGENTS), [
      "aurelius", "composio", "curtis", "grantie", "merli",
    ]);
  });
  it("a bot the ranking has never seen sorts after the ranked ones", () => {
    const a = { ...EMPTY_ARRANGEMENT, order: ["merli", "curtis"] };
    assert.deepEqual(rankedUnpinned(a, AGENTS), ["merli", "curtis", "aurelius", "composio", "grantie"]);
  });
  it("All bots excludes pinned and grouped bots", () => {
    const { a, id } = withGroup();
    const b = moveTo(togglePin(a, "merli"), "curtis", { group: id });
    assert.deepEqual(membersOf(b, "all", AGENTS), ["aurelius", "composio", "grantie"]);
    assert.deepEqual(membersOf(b, { group: id }, AGENTS), ["curtis"]);
  });
  it("nudge moves among section siblings and keeps other sections' ranks", () => {
    const { a, id } = withGroup();
    // Group holds aurelius + curtis + merli; composio + grantie stay under All.
    let b = moveTo(moveTo(moveTo(a, "aurelius", { group: id }), "curtis", { group: id }), "merli", { group: id });
    assert.deepEqual(membersOf(b, { group: id }, AGENTS), ["aurelius", "curtis", "merli"]);
    b = nudge(b, "merli", -1, AGENTS);
    assert.deepEqual(membersOf(b, { group: id }, AGENTS), ["aurelius", "merli", "curtis"]);
    b = nudge(b, "merli", -1, AGENTS);
    assert.deepEqual(membersOf(b, { group: id }, AGENTS), ["merli", "aurelius", "curtis"]);
    // The top can't go higher.
    assert.deepEqual(membersOf(nudge(b, "merli", -1, AGENTS), { group: id }, AGENTS), ["merli", "aurelius", "curtis"]);
    // And the other section never noticed.
    assert.deepEqual(membersOf(b, "all", AGENTS), ["composio", "grantie"]);
    b = nudge(b, "curtis", -1, AGENTS);
    assert.deepEqual(membersOf(b, { group: id }, AGENTS), ["merli", "curtis", "aurelius"]);
    b = nudge(b, "merli", 1, AGENTS);
    assert.deepEqual(membersOf(b, { group: id }, AGENTS), ["curtis", "merli", "aurelius"]);
  });
  it("nudge within pins moves in pin order", () => {
    let a = togglePin(togglePin(togglePin(EMPTY_ARRANGEMENT, "a"), "b"), "c");
    a = nudge(a, "c", -1, AGENTS);
    assert.deepEqual(a.pins, ["a", "c", "b"]);
    a = nudge(a, "a", 1, AGENTS);
    assert.deepEqual(a.pins, ["c", "a", "b"]);
  });
});

describe("groups", () => {
  it("create trims, refuses empty and duplicate names", () => {
    const r = createGroup(EMPTY_ARRANGEMENT, "  Sales  ");
    assert.ok("next" in r);
    assert.equal(r.group.name, "Sales");
    assert.deepEqual(createGroup(r.next, "   "), { error: "empty" });
    assert.deepEqual(createGroup(r.next, "sales"), { error: "duplicate" });
  });
  it("rename persists and keeps the id", () => {
    const { a, id } = withGroup();
    const r = renameGroup(a, id, "Support");
    assert.ok("next" in r);
    assert.deepEqual(r.next.groups, [{ id, name: "Support" }]);
    assert.deepEqual(renameGroup(a, "nope", "x"), { error: "missing" });
    assert.deepEqual(renameGroup(a, id, ""), { error: "empty" });
    // Renaming to its own name is not a duplicate.
    assert.ok("next" in renameGroup(a, id, "Sales"));
  });
  it("delete refuses a group that still holds a bot", () => {
    const { a, id } = withGroup();
    const b = moveTo(a, "merli", { group: id });
    assert.deepEqual(deleteGroup(b, id, AGENTS), { error: "not_empty" });
    const c = moveTo(b, "merli", "all");
    const r = deleteGroup(c, id, AGENTS);
    assert.ok("next" in r);
    assert.deepEqual(r.next.groups, []);
  });
  it("a stale assignment for a bot that no longer exists does not lock the group", () => {
    const { a, id } = withGroup();
    const b = moveTo(a, "ghost", { group: id });
    const r = deleteGroup(b, id, AGENTS);
    assert.ok("next" in r);
    assert.deepEqual(r.next.assign, {});
  });
  it("the reserved sections cannot be deleted", () => {
    assert.deepEqual(deleteGroup(EMPTY_ARRANGEMENT, "pinned", AGENTS), { error: "reserved" });
    assert.deepEqual(deleteGroup(EMPTY_ARRANGEMENT, "all", AGENTS), { error: "reserved" });
    assert.deepEqual(deleteGroup(EMPTY_ARRANGEMENT, "chats", AGENTS), { error: "reserved" });
    assert.deepEqual(deleteGroup(EMPTY_ARRANGEMENT, "nope", AGENTS), { error: "missing" });
  });
  it("deleting a group forgets its fold", () => {
    const { a, id } = withGroup();
    const r = deleteGroup(toggleFold(a, id), id, AGENTS);
    assert.ok("next" in r);
    assert.deepEqual(r.next.folded, []);
  });
  it("moveTo an unknown group is a no-op", () => {
    assert.equal(moveTo(EMPTY_ARRANGEMENT, "merli", { group: "nope" }), EMPTY_ARRANGEMENT);
  });
  it("an assignment to a deleted group reads as All bots", () => {
    const a = { ...EMPTY_ARRANGEMENT, assign: { merli: "gone" } };
    assert.equal(homeOf(a, "merli"), "all");
    assert.ok(membersOf(a, "all", AGENTS).includes("merli"));
  });
});

describe("folds", () => {
  it("toggle round-trips for reserved and group ids", () => {
    const a = toggleFold(EMPTY_ARRANGEMENT, "pinned");
    assert.deepEqual(a.folded, ["pinned"]);
    assert.deepEqual(toggleFold(a, "pinned").folded, []);
  });
});

describe("parseArrangement", () => {
  it("reads junk as empty", () => {
    assert.deepEqual(parseArrangement(null), EMPTY_ARRANGEMENT);
    assert.deepEqual(parseArrangement("nope"), EMPTY_ARRANGEMENT);
    assert.deepEqual(parseArrangement({ pins: "x", groups: [1, { id: 2 }], assign: { a: 1 } }), EMPTY_ARRANGEMENT);
  });
  it("keeps only well-formed fields", () => {
    const a = parseArrangement({
      pins: ["a", 3], order: ["b"], groups: [{ id: "g1", name: "S", extra: 1 }],
      assign: { a: "g1", b: null }, folded: ["g1"],
    });
    assert.deepEqual(a, {
      pins: ["a"], order: ["b"], groups: [{ id: "g1", name: "S" }], assign: { a: "g1" }, folded: ["g1"],
      offlineOpen: false,
    });
  });
  it("opens the offline tail only on an explicit true", () => {
    // Saved before the flag existed, or junk: the default, closed.
    assert.equal(parseArrangement({ pins: [] }).offlineOpen, false);
    assert.equal(parseArrangement({ offlineOpen: "yes" }).offlineOpen, false);
    assert.equal(parseArrangement({ offlineOpen: true }).offlineOpen, true);
  });
});

describe("offline tail", () => {
  it("starts closed and toggles", () => {
    assert.equal(EMPTY_ARRANGEMENT.offlineOpen, false);
    const open = setOfflineOpen(EMPTY_ARRANGEMENT, true);
    assert.equal(open.offlineOpen, true);
    assert.equal(setOfflineOpen(open, false).offlineOpen, false);
    // No change, no new object: the hook skips the storage write on identity.
    assert.equal(setOfflineOpen(open, true), open);
  });
  it("nudge steps inside the band it is given, not past a hidden row", () => {
    // Drawn as: awake [aurelius, grantie], asleep tail [composio, curtis, merli].
    // Moving grantie up must swap it with aurelius even though composio and
    // curtis sit between them in the shared ranking.
    const band = ["aurelius", "grantie"];
    const a = nudge(EMPTY_ARRANGEMENT, "grantie", -1, AGENTS, band);
    const ranked = rankedUnpinned(a, AGENTS);
    assert.ok(ranked.indexOf("grantie") < ranked.indexOf("aurelius"));
    // The top of its band: nothing above it to swap with.
    assert.equal(nudge(a, "grantie", -1, AGENTS, ["grantie", "aurelius"]), a);
  });
});
