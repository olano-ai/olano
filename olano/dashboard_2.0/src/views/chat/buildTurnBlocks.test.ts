// Unit tests for the reply block order. Run with `npm run test:unit`.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { ToolCall } from "./sessionStore";
import { buildTurnBlocks, type TurnBlock } from "./buildTurnBlocks.ts";

function tool(id: string, name = "read_file"): ToolCall {
  return { id, name, args: "{}", hasResult: true };
}
const A = tool("a");
const B = tool("b");
const C = tool("c");

// Compact shape for assertions: "T:<text>" and "tools:<ids>".
function shape(blocks: TurnBlock[]): string[] {
  return blocks.map((b) => (b.kind === "text" ? `T:${b.text}` : `tools:${b.tools.map((t) => t.id).join(",")}`));
}

describe("buildTurnBlocks", () => {
  it("no tools, no segments: just the body", () => {
    assert.deepEqual(shape(buildTurnBlocks([], undefined, "hello")), ["T:hello"]);
  });

  it("no tools, one segment: just the text", () => {
    assert.deepEqual(shape(buildTurnBlocks([], [{ text: "hello", after: null }], "hello")), ["T:hello"]);
  });

  it("nothing at all renders nothing", () => {
    assert.deepEqual(buildTurnBlocks([], undefined, ""), []);
    assert.deepEqual(buildTurnBlocks([], [], ""), []);
  });

  it("without segments: every tool first, then the body (today's layout)", () => {
    assert.deepEqual(shape(buildTurnBlocks([A, B], undefined, "done")), ["tools:a,b", "T:done"]);
  });

  it("tools first then text, from segments", () => {
    const segs = [{ text: "done", after: "b" }];
    assert.deepEqual(shape(buildTurnBlocks([A, B], segs, "done")), ["tools:a,b", "T:done"]);
  });

  it("text, tools, text", () => {
    const segs = [
      { text: "Let me look.", after: null },
      { text: "\n\nFound it.", after: "a" },
    ];
    const body = "Let me look.\n\nFound it.";
    assert.deepEqual(shape(buildTurnBlocks([A], segs, body)), ["T:Let me look.", "tools:a", "T:\n\nFound it."]);
  });

  it("splits the tool list at each boundary", () => {
    const segs = [
      { text: "one", after: null },
      { text: "two", after: "a" },
      { text: "three", after: "c" },
    ];
    assert.deepEqual(shape(buildTurnBlocks([A, B, C], segs, "onetwothree")), [
      "T:one",
      "tools:a",
      "T:two",
      "tools:b,c",
      "T:three",
    ]);
  });

  it("an unknown id flushes every pending tool — nothing is dropped", () => {
    const segs = [
      { text: "one", after: null },
      { text: "two", after: "zzz" },
    ];
    assert.deepEqual(shape(buildTurnBlocks([A, B], segs, "onetwo")), ["T:one", "tools:a,b", "T:two"]);
  });

  it("an empty-string id (a frame with no id) behaves like an unknown one", () => {
    const segs = [{ text: "two", after: "" }];
    assert.deepEqual(shape(buildTurnBlocks([A], segs, "two")), ["tools:a", "T:two"]);
  });

  it("tools after the last segment close the turn", () => {
    const segs = [{ text: "Looking…", after: null }];
    assert.deepEqual(shape(buildTurnBlocks([A, B], segs, "Looking…")), ["T:Looking…", "tools:a,b"]);
  });

  it("a whitespace-only segment draws nothing but still places its tools", () => {
    const segs = [
      { text: "one", after: null },
      { text: "\n\n", after: "a" },
    ];
    assert.deepEqual(shape(buildTurnBlocks([A, B], segs, "one\n\n")), ["T:one", "tools:a", "tools:b"]);
  });

  it("a whitespace-only body with no segments draws no text block", () => {
    assert.deepEqual(shape(buildTurnBlocks([A], undefined, "  \n")), ["tools:a"]);
  });

  it("adjacent text spans with no tool between them join into one block", () => {
    const segs = [
      { text: "one", after: null },
      { text: "\n\ntwo", after: "a" },
      { text: "\n\nthree", after: "a" },
    ];
    assert.deepEqual(shape(buildTurnBlocks([A], segs, "one\n\ntwo\n\nthree")), [
      "T:one",
      "tools:a",
      "T:\n\ntwo\n\nthree",
    ]);
  });

  it("segments that do not add up to the body fall back to the body", () => {
    // e.g. an error tail appended to the body only
    const segs = [{ text: "one", after: null }];
    assert.deepEqual(shape(buildTurnBlocks([A], segs, "one\n\n**Error:** boom")), [
      "tools:a",
      "T:one\n\n**Error:** boom",
    ]);
  });

  it("whitespace differences between segments and body are tolerated", () => {
    const segs = [
      { text: "one", after: null },
      { text: "two", after: "a" },
    ];
    assert.deepEqual(shape(buildTurnBlocks([A], segs, "one\n\ntwo")), ["T:one", "tools:a", "T:two"]);
  });

  it("never mutates its inputs", () => {
    const tools = [A, B];
    const segs = [{ text: "x", after: "a" }];
    buildTurnBlocks(tools, segs, "x");
    assert.deepEqual(tools, [A, B]);
    assert.deepEqual(segs, [{ text: "x", after: "a" }]);
  });
});
