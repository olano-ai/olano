import { test } from "node:test";
import assert from "node:assert/strict";
import { classify, INLINE_TEXT_MAX_BYTES, looksLikeText } from "./attachments.ts";

const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation";

const enc = new TextEncoder();
const text = (s: string) => enc.encode(s);
const ZIP = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x00, 0x00]);
const f = (name: string, type = "", size = 100) => ({ name, type, size });

test("office documents and PDFs are attachable, not refused", () => {
  // The composer used to toast "unsupported file type" for these.
  assert.equal(classify(f("PM Trade Blotter.xlsx", XLSX), ZIP), "document");
  assert.equal(classify(f("Decision_Report.docx", DOCX), ZIP), "document");
  assert.equal(classify(f("deck.pptx", PPTX), ZIP), "document");
  assert.equal(classify(f("old.doc", "application/msword"), ZIP), "document");
  // A PDF is a document by name even when its first block is plain ASCII.
  assert.equal(classify(f("report.pdf", "application/pdf"), text("%PDF-1.4\n1 0 obj\n")), "document");
  // A phone browser can hand over no MIME type at all; the file still goes.
  assert.equal(classify(f("archive.zip"), ZIP), "document");
});

test("any text file is text, whatever its extension or MIME type", () => {
  for (const name of ["schema.sql", "Main.kt", "Dockerfile", "Makefile", "query.graphql", "x.made-up"]) {
    assert.equal(classify(f(name, "application/octet-stream"), text("line one\nline two\n")), "file", name);
  }
  assert.equal(classify(f("notes.md"), text("# hi")), "file");
  assert.equal(classify(f("data.csv", "application/vnd.ms-excel"), text("a,b\n1,2\n")), "file");
});

test("a TypeScript file the browser calls a video is still text", () => {
  assert.equal(classify(f("app.ts", "video/mp2t"), text("export const x = 1;\n")), "file");
  // ...while a real MPEG transport stream named .ts stays a video.
  assert.equal(classify(f("clip.ts", "video/mp2t"), new Uint8Array([0x47, 0x40, 0x11, 0x10, 0x00, 0x42, 0xf0])), "video");
});

test("large text is sent to be saved whole, not pasted and cut", () => {
  assert.equal(classify(f("server.log", "text/plain", INLINE_TEXT_MAX_BYTES), text("ok\n")), "file");
  assert.equal(classify(f("server.log", "text/plain", INLINE_TEXT_MAX_BYTES + 1), text("ok\n")), "document");
});

test("media keeps its kind", () => {
  assert.equal(classify(f("a.png", "image/png"), new Uint8Array([0x89, 0x50, 0x4e, 0x47])), "image");
  assert.equal(classify(f("a.mp3", "audio/mpeg"), null), "audio");
  assert.equal(classify(f("a.mp4", "video/mp4"), null), "video");
  // Unreadable head and no type: saved as a document rather than refused.
  assert.equal(classify(f("mystery"), null), "document");
});

test("the text sniff", () => {
  assert.equal(looksLikeText(text("")), true);
  assert.equal(looksLikeText(text("naïve café")), true);
  assert.equal(looksLikeText(text("é").subarray(0, 1)), true); // cut mid-character
  assert.equal(looksLikeText(text("\u001b[31mred\u001b[0m\n")), true);
  assert.equal(looksLikeText(new Uint8Array([0x61, 0x00, 0x62])), false);
  assert.equal(looksLikeText(ZIP), false);
  assert.equal(looksLikeText(new Uint8Array([0xff, 0xfe, 0x41, 0x42])), false);
});
