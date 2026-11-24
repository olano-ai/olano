import assert from "node:assert/strict";
import { test } from "node:test";

import { rosterKey, rosterOf, talkAgentOf } from "./roster.ts";

test("the roster says each agent's display name and falls back to its handle", () => {
  const roster = rosterOf([
    { name: "aurelius", display_name: "Aurelius" },
    { name: "jane", display_name: "  " },
    { name: "hannah" },
  ]);
  assert.deepEqual(roster, [
    { name: "aurelius", display: "Aurelius" },
    { name: "jane", display: "jane" },
    { name: "hannah", display: "hannah" },
  ]);
});

test("an agent appears once, in the app's order, and an empty handle is left out", () => {
  const roster = rosterOf([{ name: "jane" }, { name: "" }, { name: "jane", display_name: "Jane" }, { name: "nova" }]);
  assert.deepEqual(
    roster.map((entry) => entry.name),
    ["jane", "nova"],
  );
});

test("the chosen default agent comes first; one that is not there changes nothing", () => {
  const agents = [{ name: "jane" }, { name: "nova" }, { name: "hannah" }];
  assert.deepEqual(
    rosterOf(agents, "hannah").map((entry) => entry.name),
    ["hannah", "jane", "nova"],
  );
  assert.deepEqual(
    rosterOf(agents, "jane").map((entry) => entry.name),
    ["jane", "nova", "hannah"],
  );
  assert.deepEqual(
    rosterOf(agents, "nobody").map((entry) => entry.name),
    ["jane", "nova", "hannah"],
  );
  assert.deepEqual(
    rosterOf(agents, "").map((entry) => entry.name),
    ["jane", "nova", "hannah"],
  );
});

test("the key changes with any name or display name, and not otherwise", () => {
  const a = rosterKey(rosterOf([{ name: "jane", display_name: "Jane" }]));
  const b = rosterKey(rosterOf([{ name: "jane", display_name: "Jane " }]));
  const c = rosterKey(rosterOf([{ name: "jane", display_name: "Jane D" }]));
  assert.equal(a, b);
  assert.notEqual(a, c);
  assert.notEqual(rosterKey(rosterOf([{ name: "a" }, { name: "b" }])), rosterKey(rosterOf([{ name: "b" }, { name: "a" }])));
});

test("a talk request names an agent or nothing", () => {
  assert.equal(talkAgentOf(" aurelius "), "aurelius");
  assert.equal(talkAgentOf(""), null);
  assert.equal(talkAgentOf(null), null);
  assert.equal(talkAgentOf(42), null);
});
