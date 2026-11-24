import assert from "node:assert/strict";
import { test } from "node:test";

import { fileNameOf, mimeOf } from "./shareNames.ts";

test("a shared file keeps its own name, decoded", () => {
  assert.equal(fileNameOf("file:///private/var/mobile/Containers/Data/Application/X/Documents/Inbox/Q3%20plan.pdf"), "Q3 plan.pdf");
  assert.equal(fileNameOf("file:///a/b/notes.txt?x=1"), "notes.txt");
  assert.equal(fileNameOf("file:///a/b/bad%E0.txt"), "bad%E0.txt");
  assert.equal(fileNameOf("file:///"), "shared file");
});

test("the type comes from the extension, and an unknown one is plain bytes", () => {
  assert.equal(mimeOf("Q3 plan.PDF"), "application/pdf");
  assert.equal(mimeOf("budget.xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
  assert.equal(mimeOf("README"), "application/octet-stream");
  assert.equal(mimeOf("archive.xyz"), "application/octet-stream");
});
