import assert from "node:assert/strict";
import { test } from "node:test";

import type { CortexItem } from "../net/types.ts";
import { isMoving, itemKey, itemsFor, shownKind, STAGES, stageCounts, toggleStage, withArticle, withMoving, withoutItems } from "./stages.ts";

const item = (id: string, kind: CortexItem["kind"], over: Partial<CortexItem> = {}): CortexItem =>
  ({ id, kind, title: id, status: "active", agent: "nova", updated_at: `2026-10-${10 + Number(id.slice(1))}T00:00:00Z`, ...over }) as CortexItem;

const items = [
  item("i1", "idea"),
  item("i2", "idea", { agent: "aria" }),
  item("i3", "prototype"),
  item("i4", "brainstorm"),
  item("i5", "project", { status: "discarded" }),
];

test("the stages run in the order things flow", () => {
  assert.deepEqual(
    STAGES.map((s) => s.label),
    ["Brainstorms", "Ideas", "Prototypes", "Projects"],
  );
});

test("each stage counts its live items, for one agent or everyone", () => {
  assert.deepEqual(stageCounts(items, null), { brainstorm: 1, idea: 2, prototype: 1, project: 0 });
  assert.deepEqual(stageCounts(items, "aria"), { brainstorm: 0, idea: 1, prototype: 0, project: 0 });
});

test("a stage lists its items newest first; all lists every live item", () => {
  assert.deepEqual(
    itemsFor(items, "idea", null).map((i) => i.id),
    ["i2", "i1"],
  );
  assert.deepEqual(
    itemsFor(items, "all", null).map((i) => i.id),
    ["i4", "i3", "i2", "i1"],
  );
});

test("a promoted item has left the list: its successor is the one in the next stage", () => {
  const promoted = [...items, item("i6", "idea", { status: "promoted" }), item("i7", "prototype")];
  assert.deepEqual(stageCounts(promoted, null), { brainstorm: 1, idea: 2, prototype: 2, project: 0 });
  assert.ok(!itemsFor(promoted, "all", null).some((i) => i.id === "i6"));
});

test("an item being promoted is already listed under the stage it is heading for", () => {
  const running = item("i8", "brainstorm", { job: { id: "j", kind: "promote", status: "running", detail: { to: "idea" } } });
  const queued = item("i9", "idea", { job: { id: "", kind: "promote", status: "queued" } });
  const done = item("i10", "idea", { job: { id: "j2", kind: "promote", status: "done", detail: { to: "project" } } });
  const explore = item("i11", "idea", { job: { id: "j3", kind: "explore", status: "running" } });
  assert.equal(isMoving(running), true);
  assert.equal(shownKind(running), "idea");
  // No stage named: the funnel's next one.
  assert.equal(shownKind(queued), "prototype");
  assert.equal(isMoving(done), false);
  assert.equal(shownKind(done), "idea");
  assert.equal(shownKind(explore), "idea");
  const all = [...items, running, queued];
  // i8 counts as an idea, not a brainstorm; i9 as a prototype.
  assert.deepEqual(stageCounts(all, null), { brainstorm: 1, idea: 3, prototype: 2, project: 0 });
  assert.ok(itemsFor(all, "idea", null).some((i) => i.id === "i8"));
  assert.ok(!itemsFor(all, "brainstorm", null).some((i) => i.id === "i8"));
});

test("tapping the stage shown goes back to all", () => {
  assert.equal(toggleStage("all", "idea"), "idea");
  assert.equal(toggleStage("idea", "idea"), "all");
  assert.equal(toggleStage("idea", "project"), "project");
});

test("the list is patched by key, across agents", () => {
  assert.equal(itemKey(item("i1", "idea")), "nova:i1");
  assert.equal(itemKey({ id: "x" }), ":x");
  const dropped = withoutItems(items, new Set(["nova:i1", "aria:i2"]));
  assert.deepEqual(
    dropped.map((i) => i.id),
    ["i3", "i4", "i5"],
  );
  const moving = withMoving(items, "nova:i4", "idea");
  assert.equal(shownKind(moving.find((i) => i.id === "i4")!), "idea");
  assert.equal(isMoving(moving.find((i) => i.id === "i1")!), false);
  // The original is untouched.
  assert.equal(items[3].job, undefined);
});

test("labels take their article", () => {
  assert.equal(withArticle("idea"), "an idea");
  assert.equal(withArticle("prototype"), "a prototype");
  assert.equal(withArticle("brainstorm"), "a brainstorm");
});
