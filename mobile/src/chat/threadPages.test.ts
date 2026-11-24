import assert from "node:assert/strict";
import { test } from "node:test";

import type { ThreadSummary } from "../net/types.ts";
import { mapThread, mergePages, normalizeThreadPage, withStartedThread } from "./threadPages.ts";

const at = (day: number) => `2026-10-${String(day).padStart(2, "0")}T00:00:00Z`;
const chat = (id: string, day: number): ThreadSummary => ({ id, origin: "interactive", channel: "dashboard", updated_at: at(day) });
const run = (id: string, day: number): ThreadSummary => ({ id, origin: "scheduled", channel: "scheduler", updated_at: at(day) });

test("a paged answer passes through, with its cursor", () => {
  const page = normalizeThreadPage({ threads: [chat("a", 2)], has_more: true, next_before: "123" }, { limit: 1 });
  assert.deepEqual(page, { threads: [chat("a", 2)], has_more: true, next_before: "123" });
});

test("a runtime from before paging answers with everything, read as one page", () => {
  const rows = [run("r", 9), chat("old", 1), chat("new", 5)];
  const all = normalizeThreadPage(rows, { limit: 20 });
  assert.deepEqual(all.threads.map((t) => t.id), ["r", "new", "old"]);
  assert.equal(all.has_more, false);
  const conversations = normalizeThreadPage(rows, { limit: 1, kind: "conversation" });
  assert.deepEqual(conversations.threads.map((t) => t.id), ["new", "old"]);
});

test("pages merge newest first with every thread once", () => {
  const merged = mergePages([[chat("c", 7), chat("b", 6)], [chat("b", 6), chat("a", 1)], [chat("d", 9)]]);
  assert.deepEqual(merged.map((t) => t.id), ["d", "c", "b", "a"]);
});

test("a rename or a delete lands in the page it belongs to", () => {
  const page = { threads: [chat("a", 2), chat("b", 1)], has_more: false, next_before: null };
  assert.deepEqual(mapThread(page, "a", (row) => ({ ...row, name: "Budget" })).threads[0].name, "Budget");
  assert.deepEqual(mapThread(page, "b", () => null).threads.map((t) => t.id), ["a"]);
  assert.deepEqual(mapThread(page, "zzz", () => null), page);
});

test("a conversation the phone just started becomes the agent's latest at once", () => {
  const now = new Date("2026-10-21T20:00:00Z");
  const before = { threads: [chat("older", 3)], has_more: true, next_before: "9" };
  const page = withStartedThread(before, "t-new", "  Which supplier\n ships fastest?  ", now);
  // The row the agents screen shows and opens: the new thread, titled by its message.
  assert.equal(page.threads.length, 1);
  assert.deepEqual(
    [page.threads[0].id, page.threads[0].title, page.threads[0].channel, page.threads[0].updated_at],
    ["t-new", "Which supplier ships fastest?", "mobile", now.toISOString()],
  );
  assert.equal(page.has_more, true);
  // Nothing loaded yet, and a repeat of the same thread, both give one row.
  assert.deepEqual(withStartedThread(undefined, "t", "hi", now).threads.map((t) => t.id), ["t"]);
  assert.deepEqual(withStartedThread(page, "t-new", "again", now).threads.map((t) => t.id), ["t-new"]);
});
