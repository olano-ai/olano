import { strict as assert } from "node:assert";
import { test } from "node:test";

import { createFolder, EMPTY_ARRANGEMENT, moveTo, toggleFold, togglePin, type Arrangement } from "../shared/dashboard.ts";
import { chatSections, folderNameProblem, moveTargets, onlyAllAgents } from "./arrangement.ts";

const ROSTER = ["atlas", "jane", "maria", "nova", "zed"];

function withFolder(a: Arrangement, name: string): { a: Arrangement; id: string } {
  const made = createFolder(a, name);
  if ("error" in made) throw new Error(made.error);
  return { a: made.next, id: made.group.id };
}

test("with nothing arranged, every agent is under All agents, alphabetically, with no heading needed", () => {
  const sections = chatSections(EMPTY_ARRANGEMENT, ["nova", "atlas", "jane"]);
  assert.equal(sections.length, 1);
  assert.equal(sections[0].kind, "all");
  assert.deepEqual(sections[0].agents, ["atlas", "jane", "nova"]);
  assert.ok(onlyAllAgents(sections));
});

test("pinned agents come first in pin order, and leave All agents", () => {
  let a = togglePin(EMPTY_ARRANGEMENT, "nova");
  a = togglePin(a, "atlas");
  const sections = chatSections(a, ROSTER);
  assert.deepEqual(
    sections.map((s) => [s.id, s.agents]),
    [
      ["pinned", ["nova", "atlas"]],
      ["all", ["jane", "maria", "zed"]],
    ],
  );
  assert.ok(!onlyAllAgents(sections));
});

test("a folder sits between Pinned and All agents, and shows even while empty", () => {
  const { a: made, id } = withFolder(togglePin(EMPTY_ARRANGEMENT, "jane"), "Sales");
  assert.deepEqual(
    chatSections(made, ROSTER).map((s) => [s.title, s.agents]),
    [
      ["Pinned", ["jane"]],
      ["Sales", []],
      ["All agents", ["atlas", "maria", "nova", "zed"]],
    ],
  );
  const moved = moveTo(moveTo(made, "zed", { group: id }), "maria", { group: id });
  const sections = chatSections(moved, ROSTER);
  assert.deepEqual(sections[1].agents, ["maria", "zed"]);
  assert.deepEqual(sections[2].agents, ["atlas", "nova"]);
});

test("one home per agent: pinning an agent takes it out of its folder", () => {
  const { a: made, id } = withFolder(EMPTY_ARRANGEMENT, "Ops");
  const inFolder = moveTo(made, "atlas", { group: id });
  const pinned = togglePin(inFolder, "atlas");
  const sections = chatSections(pinned, ROSTER);
  assert.deepEqual(sections.find((s) => s.id === "pinned")?.agents, ["atlas"]);
  assert.deepEqual(sections.find((s) => s.id === id)?.agents, []);
});

test("an agent that is gone from the roster disappears from every section", () => {
  const a = togglePin(EMPTY_ARRANGEMENT, "ghost");
  assert.deepEqual(chatSections(a, ["atlas"]).map((s) => s.id), ["all"]);
});

test("folded sections stay folded, except while searching", () => {
  const a = toggleFold(togglePin(EMPTY_ARRANGEMENT, "jane"), "pinned");
  assert.equal(chatSections(a, ROSTER)[0].folded, true);
  assert.equal(chatSections(a, ROSTER, () => true)[0].folded, false);
});

test("searching keeps only matches, and only sections holding one", () => {
  const { a: made, id } = withFolder(togglePin(EMPTY_ARRANGEMENT, "jane"), "Empty one");
  const second = withFolder(made, "Research");
  const a = moveTo(second.a, "nova", { group: second.id });
  const sections = chatSections(a, ROSTER, (n) => n.startsWith("n") || n.startsWith("m"));
  assert.deepEqual(
    sections.map((s) => [s.title, s.agents]),
    [
      ["Research", ["nova"]],
      ["All agents", ["maria"]],
    ],
  );
  assert.ok(!sections.some((s) => s.id === id));
});

test("move targets list Pinned, each folder and All agents, marking where the agent is", () => {
  const { a, id } = withFolder(EMPTY_ARRANGEMENT, "Sales");
  const inSales = moveTo(a, "atlas", { group: id });
  assert.deepEqual(
    moveTargets(inSales, "atlas").map((t) => [t.label, t.current]),
    [
      ["Pinned", false],
      ["Sales", true],
      ["All agents", false],
    ],
  );
  assert.deepEqual(
    moveTargets(inSales, "jane").map((t) => t.current),
    [false, false, true],
  );
});

test("a refused folder name is explained in words", () => {
  assert.match(folderNameProblem("empty"), /name/);
  assert.match(folderNameProblem("duplicate"), /already/);
  const { a } = withFolder(EMPTY_ARRANGEMENT, "Sales");
  const again = createFolder(a, "  sales ");
  assert.ok("error" in again && again.error === "duplicate");
});
