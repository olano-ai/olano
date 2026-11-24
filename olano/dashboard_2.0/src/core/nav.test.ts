// Unit tests for the console sidebar's More order. Run with `npm run test:unit`
// (Node's built-in runner with type stripping — no test framework in the
// bundle).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { DEFAULT_MORE_HEAD, placeItem, resolveMoreOrder } from "./navOrder.ts";

// A gated roster in NAV order: the four head tabs are deliberately NOT
// adjacent, so "head first" is visibly a reordering and not a coincidence.
const AVAILABLE = [
  "/overview",
  "/insights",
  "/agents",
  "/cortex",
  "/scheduler",
  "/issues",
  "/calendar",
  "/files",
];

describe("resolveMoreOrder", () => {
  it("ships the four head tabs first, then NAV order, and persists nothing", () => {
    const r = resolveMoreOrder(AVAILABLE, undefined);
    assert.deepEqual(r.order, [
      ...DEFAULT_MORE_HEAD,
      "/agents",
      "/scheduler",
      "/calendar",
      "/files",
    ]);
    assert.equal(r.persist, null);
    // Empty arrays read the same as nothing saved.
    assert.deepEqual(resolveMoreOrder(AVAILABLE, { primary: [], more: [] }), r);
  });

  it("skips a head tab this human cannot see without persisting the default", () => {
    const r = resolveMoreOrder(
      AVAILABLE.filter((to) => to !== "/cortex"),
      undefined,
    );
    assert.deepEqual(r.order, ["/overview", "/insights", "/issues", "/agents", "/scheduler", "/calendar", "/files"]);
    assert.equal(r.persist, null);
  });

  it("folds a saved Primary into the head of More, once", () => {
    // The old default split, as most saved prefs hold it.
    const saved = {
      primary: ["/overview", "/insights", "/cortex", "/issues"],
      more: ["/agents", "/scheduler", "/calendar", "/files"],
    };
    const r = resolveMoreOrder(AVAILABLE, saved);
    assert.deepEqual(r.order, [...DEFAULT_MORE_HEAD, "/agents", "/scheduler", "/calendar", "/files"]);
    assert.deepEqual(r.persist, { primary: [], more: r.order });

    // The write-back is the fixed point: loading it again folds nothing.
    const again = resolveMoreOrder(AVAILABLE, r.persist!);
    assert.deepEqual(again.order, r.order);
    assert.equal(again.persist, null);
  });

  it("puts what the human kept on the top shelf right after the head", () => {
    const saved = { primary: ["/scheduler", "/overview"], more: ["/files", "/agents", "/insights"] };
    const r = resolveMoreOrder(AVAILABLE, saved);
    // Head in shipped order, then the rest of Primary, then More as saved,
    // then anything never saved (a freshly shipped tab) at the end.
    assert.deepEqual(r.order, [
      "/overview",
      "/insights",
      "/cortex",
      "/issues",
      "/scheduler",
      "/files",
      "/agents",
      "/calendar",
    ]);
    // The write-back carries only what was saved (plus the head) — a tab that
    // was never saved keeps surfacing by being appended, not by being stored.
    assert.deepEqual(r.persist, {
      primary: [],
      more: ["/overview", "/insights", "/cortex", "/issues", "/scheduler", "/files", "/agents"],
    });
  });

  it("leaves a one-group split as the human arranged it, appending new tabs", () => {
    const saved = { primary: [], more: ["/files", "/cortex", "/overview"] };
    const r = resolveMoreOrder(AVAILABLE, saved);
    assert.deepEqual(r.order, [
      "/files",
      "/cortex",
      "/overview",
      "/insights",
      "/agents",
      "/scheduler",
      "/issues",
      "/calendar",
    ]);
    assert.equal(r.persist, null);
  });

  it("keeps a saved path this human cannot see in the write-back, not the order", () => {
    // A member's roster has no /vault; an admin's does. The fold must not throw
    // the entry away for the day they are an admin again.
    const saved = { primary: ["/overview"], more: ["/vault", "/files"] };
    const r = resolveMoreOrder(AVAILABLE, saved);
    assert.ok(!r.order.includes("/vault"));
    assert.ok(r.persist!.more.includes("/vault"));
    assert.equal(r.order.at(-1), "/calendar");
  });

  it("dedupes a path that a corrupt save lists twice", () => {
    const r = resolveMoreOrder(AVAILABLE, { primary: ["/files"], more: ["/files", "/files"] });
    assert.equal(r.order.filter((to) => to === "/files").length, 1);
    assert.equal(r.persist!.more.filter((to) => to === "/files").length, 1);
  });
});

describe("placeItem with an always-empty Primary", () => {
  // How the console's one-group nav drives the shared two-group primitive.
  it("reorders within More and leaves Primary empty", () => {
    const r = placeItem({ primary: [], more: ["a", "b", "c"] }, "c", "more", "a");
    assert.deepEqual(r, { primary: [], more: ["c", "a", "b"] });
    const end = placeItem({ primary: [], more: ["a", "b", "c"] }, "a", "more", null);
    assert.deepEqual(end, { primary: [], more: ["b", "c", "a"] });
  });
});
