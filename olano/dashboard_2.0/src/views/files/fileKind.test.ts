import { test } from "node:test";
import assert from "node:assert/strict";
import { categorize, looksBinary } from "./fileKind.ts";

test("archives, Office documents and executables are binary by name", () => {
  for (const name of ["wolf-skills-export.zip", "report.DOCX", "a.tar", "lib.so", "model.safetensors", "font.woff2"]) {
    assert.equal(categorize(name).kind, "binary", name);
  }
});

test("media, PDFs and text keep their renderers", () => {
  assert.equal(categorize("shot.png").kind, "image");
  assert.equal(categorize("photo.jfif").kind, "image");
  assert.equal(categorize("clip.mp4").kind, "video");
  assert.equal(categorize("note.weba").kind, "audio");
  assert.equal(categorize("doc.pdf").kind, "pdf");
  assert.equal(categorize("README.md").kind, "markdown");
  assert.deepEqual(categorize("app.py"), { kind: "code", lang: "python" });
  assert.equal(categorize("Dockerfile").kind, "code");
  // An unknown name is read and sniffed, not assumed binary.
  assert.equal(categorize("notes.log").kind, "text");
  assert.equal(categorize("LICENSE").kind, "text");
});

test("a head the server decoded with replacement chars reads as binary", () => {
  assert.equal(looksBinary(""), false);
  assert.equal(looksBinary("PK\u0003\u0004\u0014\u0000\u0008"), true);
  assert.equal(looksBinary("��x���>�]�"), true);
  assert.equal(looksBinary("plain text\twith a tab\nand \u001b[1mANSI\u001b[0m colour\n"), false);
  // One replacement char at the very end is a head cut mid-character.
  assert.equal(looksBinary("ok�"), false);
  // A few stray bytes in a long legacy-encoded file are still text.
  assert.equal(looksBinary(`caf� and cr�me on the menu today. `.repeat(20)), false);
});
