import { strict as assert } from "node:assert";
import { test } from "node:test";

import { jsonFrames, SseParser } from "./sse.ts";

test("emits one event per blank line", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push('data: {"a":1}\n\ndata: {"b":2}\n\n'), ['{"a":1}', '{"b":2}']);
});

test("holds a split line until it completes", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push('data: {"text":"hel'), []);
  assert.deepEqual(parser.push('lo"}\n'), []);
  assert.deepEqual(parser.push("\n"), ['{"text":"hello"}']);
});

test("treats CRLF and a CR split across reads as one line break", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push("data: x\r"), []);
  assert.deepEqual(parser.push("\n\r\n"), ["x"]);
});

test("joins multi-line data and skips comments and other fields", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push(": keep-alive\nevent: frame\nid: 3\ndata: one\ndata:two\n\n"), ["one\ntwo"]);
});

test("flush returns an unterminated final event", () => {
  const parser = new SseParser();
  parser.push('data: {"type":"done"}');
  assert.deepEqual(parser.flush(), ['{"type":"done"}']);
  assert.deepEqual(parser.flush(), []);
});

test("jsonFrames keeps objects only", () => {
  assert.deepEqual(jsonFrames(['{"a":1}', "[1]", "nope", "3", '{"b":2}']), [{ a: 1 }, { b: 2 }]);
});
