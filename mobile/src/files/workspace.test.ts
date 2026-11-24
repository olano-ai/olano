import assert from "node:assert/strict";
import { test } from "node:test";

import {
  baseName,
  fileKind,
  folderRoute,
  freeName,
  joinPath,
  parentOf,
  rawUrl,
  sortEntries,
} from "./workspace.ts";

test("folders come first, names read naturally", () => {
  const sorted = sortEntries([
    { name: "report 10.md", type: "file" as const },
    { name: "zeta", type: "directory" as const },
    { name: "report 2.md", type: "file" as const },
    { name: "Alpha", type: "directory" as const },
  ]);
  assert.deepEqual(
    sorted.map((e) => e.name),
    ["Alpha", "zeta", "report 2.md", "report 10.md"],
  );
});

test("paths join and split without stray slashes", () => {
  assert.equal(joinPath("", "notes.md"), "notes.md");
  assert.equal(joinPath("reports/", "/q3.pdf"), "reports/q3.pdf");
  assert.equal(parentOf("reports/2026/q3.pdf"), "reports/2026");
  assert.equal(parentOf("notes.md"), "");
  assert.equal(baseName("reports/2026"), "2026");
  assert.equal(baseName(""), "");
});

test("a folder is one route segment", () => {
  assert.equal(folderRoute("/settings/files/nova", ""), "/settings/files/nova");
  assert.equal(folderRoute("/chats/nova/_files", "client files/Q3 & Q4/"), "/chats/nova/_files/client%20files%2FQ3%20%26%20Q4");
  assert.equal(decodeURIComponent(folderRoute("/x", "a/b").split("/").pop()!), "a/b");
});

test("raw bytes are read from the agent's workspace", () => {
  assert.equal(rawUrl("nova", "reports/Q3 plan.pdf"), "/api/files-raw/reports/Q3%20plan.pdf?agent=nova");
});

test("what a file opens as", () => {
  assert.equal(fileKind("photo.JPG"), "image");
  assert.equal(fileKind("memo.m4a"), "audio");
  assert.equal(fileKind("clip.mov"), "video");
  assert.equal(fileKind("plan.pdf"), "document");
  assert.equal(fileKind("Makefile"), "file");
});

test("a free name beside existing files", () => {
  assert.equal(freeName("notes.md", ["other.md"]), "notes.md");
  assert.equal(freeName("notes.md", ["notes.md", "notes 2.md"]), "notes 3.md");
  assert.equal(freeName("README", ["README"]), "README 2");
});
