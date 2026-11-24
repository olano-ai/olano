import { strict as assert } from "node:assert";
import { test } from "node:test";

import { SPEAK_MAX_CHARS, speakableText } from "./speakable.ts";

test("markdown marks are not read out", () => {
  assert.equal(speakableText("## Plan\n\n**Bold** and *soft* and `code` and ~~gone~~."), "Plan\n\nBold and soft and code and gone.");
  assert.equal(speakableText("> quoted line"), "quoted line");
  assert.equal(speakableText("- one\n- two\n* three\n1. first\n2) second"), "one\ntwo\nthree\n1. first\n2. second");
  assert.equal(speakableText("- [x] done\n- [ ] open"), "done\nopen");
});

test("words with underscores and stars inside them keep them", () => {
  assert.equal(speakableText("Set max_tokens to 5*3."), "Set max_tokens to 5*3.");
});

test("a link is read as its text, an address as its site", () => {
  assert.equal(speakableText("See [the report](https://example.com/r?a=(1)) now."), "See the report now.");
  assert.equal(speakableText("Open https://www.example.com/a/b?c=1 please"), "Open example.com please");
  assert.equal(speakableText("At <https://docs.olano.ai/x>."), "At docs.olano.ai.");
  assert.equal(speakableText("![chart](https://x.test/c.png) Sales rose."), "Sales rose.");
});

test("code blocks are skipped, tables read as their cells", () => {
  const md = "Run this:\n\n```bash\nrm -rf build\n```\n\nThen:\n\n| Item | Price |\n|---|---:|\n| Tea | 4.10 |";
  assert.equal(speakableText(md), "Run this:\n\nThen:\n\nItem, Price\nTea, 4.10");
});

test("html tags and rules say nothing", () => {
  assert.equal(speakableText("Line<br>next\n\n---\n\nEnd"), "Line next\n\nEnd");
});

test("nothing to say is an empty string", () => {
  assert.equal(speakableText(""), "");
  assert.equal(speakableText("```\nonly code\n```"), "");
});

test("a long reply is cut at the last sentence that fits", () => {
  const sentence = "This sentence is exactly fifty characters long ok. ";
  const text = speakableText(sentence.repeat(100));
  assert.ok(text.length <= SPEAK_MAX_CHARS);
  assert.ok(text.endsWith("ok."), text.slice(-20));
});
