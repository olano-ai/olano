import assert from "node:assert/strict";
import { test } from "node:test";

import type { ThreadRow } from "../net/types.ts";
import type { ChatMessage } from "./model.ts";
import { hasMore, withNewest, withOlder, type Fold } from "./pages.ts";

// One message per row, named after it: enough to see what went where.
const fold: Fold = (rows) => rows.map((r) => ({ key: r.content, role: "user", body: r.content, tools: [] }) as ChatMessage);
const rows = (from: number, to: number): ThreadRow[] =>
  Array.from({ length: to - from }, (_, i) => ({ role: "user", content: `m${from + i}` }));
const bodies = (messages: ChatMessage[]) => messages.map((m) => m.body);

test("a runtime that does not page gives the whole transcript and nothing above it", () => {
  const history = withNewest(null, { messages: rows(0, 3) }, fold);
  assert.equal(hasMore(history), false);
  assert.deepEqual(bodies(fold(history.newestRows)), ["m0", "m1", "m2"]);
});

test("scrolling up puts each earlier page on top until the first message", () => {
  let history = withNewest(null, { messages: rows(100, 150), start: 100 }, fold);
  assert.equal(hasMore(history), true);
  const step = withOlder(history, 100, { messages: rows(50, 100), start: 50 }, fold);
  assert.ok(step);
  assert.deepEqual(bodies(step.added).slice(0, 2), ["m50", "m51"]);
  history = step.history;
  const last = withOlder(history, 50, { messages: rows(0, 50), start: 0 }, fold);
  assert.ok(last);
  assert.equal(hasMore(last.history), false);
  assert.deepEqual(bodies(last.history.older).slice(0, 1), ["m0"]);
  assert.equal(last.history.older.length, 100);
});

test("a refresh keeps the earlier pages already read when the new page continues them", () => {
  let history = withNewest(null, { messages: rows(100, 150), start: 100 }, fold);
  history = withOlder(history, 100, { messages: rows(50, 100), start: 50 }, fold)!.history;
  // Ten new messages: the newest page now starts ten later.
  const refreshed = withNewest(history, { messages: rows(110, 160), start: 110 }, fold);
  assert.equal(refreshed.first, 50);
  assert.deepEqual(bodies(refreshed.older), bodies(fold(rows(50, 110))));
  // Nothing new: nothing moves, and the folded pages are the same objects.
  const same = withNewest(refreshed, { messages: rows(110, 160), start: 110 }, fold);
  assert.equal(same.older, refreshed.older);
});

test("a refresh that cannot join up starts over, and a page read for the old history is dropped", () => {
  const history = withNewest(null, { messages: rows(100, 150), start: 100 }, fold);
  const far = withNewest(history, { messages: rows(400, 450), start: 400 }, fold);
  assert.equal(far.first, 400);
  assert.deepEqual(far.older, []);
  assert.equal(withOlder(far, 100, { messages: rows(50, 100), start: 50 }, fold), null);
});

test("an answer that does not move up ends the scroll instead of asking forever", () => {
  const history = withNewest(null, { messages: rows(100, 150), start: 100 }, fold);
  const stuck = withOlder(history, 100, { messages: rows(100, 150), start: 100 }, fold);
  assert.ok(stuck);
  assert.equal(hasMore(stuck.history), false);
  assert.deepEqual(stuck.added, []);
});
