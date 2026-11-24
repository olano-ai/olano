import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { DRAFT_STRINGS, itemDraft } from "./draft.ts";

test("frames the item and leaves room for the person", () => {
  const draft = itemDraft({
    kind: "project",
    title: "investing-trading",
    path: "projects/investing-trading",
    abs_path: "/home/olano/agents/aurelius/workspace/projects/investing-trading",
  });
  assert.equal(
    draft,
    'The user wants to chat about this project: "investing-trading".\n' +
      "Artifacts (full workspace path):\n/home/olano/agents/aurelius/workspace/projects/investing-trading\n\nUser: ",
  );
});

test("an item without a folder still names itself", () => {
  assert.equal(itemDraft({ kind: "idea", title: "Bundle shipping" }), 'The user wants to chat about this idea: "Bundle shipping".\n\nUser: ');
});

test("the wording is the dashboard's", () => {
  const source = readFileSync(new URL("../../../olano/dashboard_2.0/src/views/cortex/strings.ts", import.meta.url), "utf8");
  const english = source.slice(source.indexOf("en: {"), source.indexOf("zh: {"));
  for (const [key, value] of Object.entries(DRAFT_STRINGS)) {
    const at = english.indexOf(`"${key}":`);
    assert.ok(at >= 0, `${key} is missing from the dashboard's strings`);
    const rest = english.slice(at + key.length + 3).trimStart();
    const quote = rest[0];
    const literal = rest.slice(0, rest.indexOf(`${quote},`, 1) + 1);
    const dashboard = new Function(`return ${literal}`)() as string;
    assert.equal(value, dashboard, key);
  }
});
