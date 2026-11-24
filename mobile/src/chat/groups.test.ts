import { strict as assert } from "node:assert";
import { test } from "node:test";

import type { GroupMessage } from "../net/types.ts";
import {
  addresseeLabel,
  addressees,
  authorLabel,
  completeMention,
  defaultGroupName,
  groupSubtitle,
  mentionedMembers,
  mentionQuery,
} from "./groups.ts";
import { groupItems, startsRun } from "./groupView.ts";

const AGENTS = [
  { name: "nova", display_name: "Nova" },
  { name: "aria", display_name: "" },
  { name: "atlas" },
];

function row(id: number, author: string, kind: GroupMessage["author_kind"], body: string, extra: Partial<GroupMessage> = {}): GroupMessage {
  return { id, group_id: 1, ts: "2026-10-17T08:00:00Z", author, author_kind: kind, addressee: "", body, ...extra };
}

test("@names pick who answers, in the order named; nobody named means the first member", () => {
  const members = ["nova", "aria", "atlas"];
  assert.deepEqual(mentionedMembers("@ARIA and @nova, then @aria again", members), ["aria", "nova"]);
  assert.deepEqual(mentionedMembers("over to you, @atlas.", members), ["atlas"]);
  assert.deepEqual(mentionedMembers("mail me at bob@nova.example", members), []);
  assert.deepEqual(addressees("what do you think?", members), ["nova"]);
  assert.deepEqual(addressees("@atlas?", members), ["atlas"]);
});

test("typing @ suggests members and completes the name", () => {
  assert.equal(mentionQuery("hey @ar"), "ar");
  assert.equal(mentionQuery("@"), "");
  assert.equal(mentionQuery("mail bob@ar"), null);
  assert.equal(mentionQuery("@aria done"), null);
  assert.equal(completeMention("hey @ar", "aria"), "hey @aria ");
});

test("authors read as people see them", () => {
  assert.equal(authorLabel(row(1, "nova", "agent", "x"), AGENTS), "Nova");
  assert.equal(authorLabel(row(1, "aria", "agent", "x"), AGENTS), "aria");
  assert.equal(authorLabel(row(1, "human-sam", "human", "x", { mine: true }), AGENTS), "You");
  assert.equal(authorLabel(row(1, "human-sam", "human", "x"), AGENTS), "sam");
});

test("a group's default name and its row's subtitle", () => {
  assert.equal(defaultGroupName(["nova", "aria"], AGENTS), "Nova and aria");
  assert.equal(defaultGroupName(["nova", "aria", "atlas"], AGENTS), "Nova, aria and atlas");
  assert.equal(defaultGroupName(["nova", "aria", "atlas", "x"], AGENTS), "Nova, aria and 2 others");
  assert.equal(groupSubtitle({ members: ["nova", "aria"] }, null, AGENTS), "Nova, aria");
  assert.equal(groupSubtitle({ members: ["nova"] }, row(2, "nova", "agent", "Three\n suppliers"), AGENTS), "Nova: Three suppliers");
  assert.equal(addresseeLabel("aria,nova", AGENTS), "To aria and Nova");
});

test("the screen's lines: yours on the right, each agent's under its name, the reply being written last", () => {
  const items = groupItems(
    [
      row(1, "human-sam", "human", "@aria hi", { mine: true, addressee: "aria" }),
      row(2, "aria", "agent", "Hello"),
      row(3, "nova", "agent", "Me too"),
      row(4, "", "system", "atlas joined"),
    ],
    [{ key: "p1", body: "next?", addressees: ["nova"], ts: 5 }],
    { author: "nova", body: "Work", segments: [], tools: [], subs: [] },
    { 2: [{ id: "c1", name: "web_search", args: "{}", hasResult: true } as never] },
  );
  assert.deepEqual(
    items.map((i) => [i.message.role, i.author, i.to.join(",")]),
    [
      ["user", "human-sam", "aria"],
      ["assistant", "aria", ""],
      ["assistant", "nova", ""],
      ["system", "", ""],
      ["user", "", "nova"],
      ["assistant", "nova", ""],
    ],
  );
  assert.equal(items[1].message.tools.length, 1);
  assert.equal(items[4].message.sending, true);
  assert.equal(items[5].message.streaming, true);
  assert.ok(startsRun(items[2], items[1]), "a different agent shows its name");
  assert.ok(!startsRun({ ...items[2], author: "aria" }, items[1]), "the same agent again does not");
});
