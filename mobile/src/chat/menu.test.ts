import { strict as assert } from "node:assert";
import { test } from "node:test";

import { conversationMenu, type MenuContext } from "./menu.ts";

const base: MenuContext = { hasThread: true, modeLabel: "Auto", canAddAgent: true, handsFreeOn: false, showFiles: true };

const actions = (ctx: MenuContext) => conversationMenu(ctx).map((s) => [s.title, s.rows.map((r) => r.action)]);

test("rows are grouped: conversation, agent, more", () => {
  assert.deepEqual(actions(base), [
    ["Conversation", ["new", "mode", "rename", "compact"]],
    ["Agent", ["conversations", "files", "scheduled", "info"]],
    ["More", ["handsFree", "addAgent"]],
  ]);
});

test("the current mode is shown as the row's value", () => {
  const mode = conversationMenu(base)[0].rows.find((r) => r.action === "mode");
  assert.equal(mode?.value, "Auto");
});

test("a new conversation has nothing to rename, compact or turn into a group", () => {
  assert.deepEqual(actions({ ...base, hasThread: false }), [
    ["Conversation", ["new", "mode"]],
    ["Agent", ["conversations", "files", "scheduled", "info"]],
    ["More", ["handsFree"]],
  ]);
});

test("hidden or unavailable rows leave, and an empty section goes with them", () => {
  const sections = conversationMenu({ ...base, showFiles: false, handsFreeOn: true, canAddAgent: false });
  assert.deepEqual(
    sections.map((s) => s.title),
    ["Conversation", "Agent"],
  );
  assert.ok(!sections[1].rows.some((r) => r.action === "files"));
});

test("only navigation rows carry a chevron", () => {
  for (const section of conversationMenu(base)) {
    for (const row of section.rows) {
      const navigates = ["new", "conversations", "files", "scheduled", "info"].includes(row.action);
      assert.equal(row.opens === "screen", navigates, row.action);
    }
  }
});
