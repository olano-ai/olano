import { strict as assert } from "node:assert";
import { test } from "node:test";

import { clipText, decodeText, fileView, formatSize, readableText, shareName } from "./files.ts";

test("files open by what they are", () => {
  assert.deepEqual(fileView("2026-10-17-daily-run.md", "text/markdown"), { view: "markdown", lang: "" });
  assert.deepEqual(fileView("data.json", "application/json"), { view: "text", lang: "json" });
  assert.deepEqual(fileView("leads.csv", ""), { view: "text", lang: "" });
  assert.deepEqual(fileView("scrape.py", "text/x-python"), { view: "text", lang: "python" });
  assert.deepEqual(fileView("notes", "text/plain"), { view: "text", lang: "" });
  assert.deepEqual(fileView("report.pdf", "application/pdf"), { view: "pdf", lang: "" });
  assert.deepEqual(fileView("plan.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"), {
    view: "other",
    lang: "",
  });
  assert.deepEqual(fileView("archive.zip", "application/zip"), { view: "other", lang: "" });
});

test("sizes read like a phone shows them", () => {
  assert.equal(formatSize(820), "820 B");
  assert.equal(formatSize(12 * 1024), "12 KB");
  assert.equal(formatSize(3.4 * 1024 * 1024), "3.4 MB");
  assert.equal(formatSize(undefined), "");
});

test("JSON is laid out to read, and anything else is left alone", () => {
  assert.equal(readableText('{"a":1,"b":[2,3]}', "json"), '{\n  "a": 1,\n  "b": [\n    2,\n    3\n  ]\n}');
  assert.equal(readableText("{not json", "json"), "{not json");
  assert.equal(readableText('{"a":1}', ""), '{"a":1}');
});

test("long text is cut with a flag; names are safe to write", () => {
  assert.deepEqual(clipText("abcdef", 4), { text: "abcd", clipped: true });
  assert.deepEqual(clipText("abc", 4), { text: "abc", clipped: false });
  assert.equal(shareName("reports/2026 plan (final).docx"), "2026 plan _final_.docx");
  assert.equal(shareName("../../.env"), "env");
  assert.equal(shareName(""), "file");
});

test("text arrives as base64 and decodes as UTF-8", () => {
  const encoded = Buffer.from("Café, 10 € a unit", "utf-8").toString("base64");
  assert.equal(decodeText(encoded), "Café, 10 € a unit");
});
