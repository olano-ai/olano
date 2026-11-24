import assert from "node:assert/strict";
import { test } from "node:test";

import type { ThreadSummary } from "../net/types.ts";
import {
  CACHE_PREFIX,
  clearCached,
  MAX_PERSISTED_THREADS,
  persistable,
  readCached,
  slimThreads,
  writeCached,
  type CacheStore,
} from "./persisted.ts";

function memoryStore(): CacheStore & { items: Map<string, string> } {
  const items = new Map<string, string>();
  return {
    items,
    getItem: (k) => items.get(k) ?? null,
    setItem: (k, v) => void items.set(k, v),
    removeItem: (k) => void items.delete(k),
    key: (i) => [...items.keys()][i] ?? null,
    get length() {
      return items.size;
    },
  };
}

const at = (day: number) => `2026-10-${String(day).padStart(2, "0")}T00:00:00Z`;

test("only the agent list and the thread lists are kept", () => {
  assert.equal(persistable("agents"), true);
  assert.equal(persistable("threads:aurelius"), true);
  assert.equal(persistable("threads:"), false);
  assert.equal(persistable("catalog:providers"), false);
  assert.equal(persistable("notifications"), false);
});

test("a kept copy reads back under the same deployment and person only", () => {
  const store = memoryStore();
  writeCached(store, "https://a|u1", "agents", [{ name: "aurelius" }], 1234);
  assert.deepEqual(readCached(store, "https://a|u1", "agents"), { data: [{ name: "aurelius" }], loadedAt: 1234 });
  assert.equal(readCached(store, "https://a|u2", "agents"), null);
  assert.equal(readCached(store, "https://b|u1", "agents"), null);
  assert.equal(readCached(store, "", "agents"), null);
});

test("a thread list keeps its newest rows, the conversation a tap opens, and no live dot", () => {
  const scheduled: ThreadSummary[] = Array.from({ length: MAX_PERSISTED_THREADS + 10 }, (_, i) => ({
    id: `scheduled_aurelius_abcd1234@${i}`,
    origin: "scheduled",
    updated_at: at(20 + (i % 9)),
  }));
  const chat: ThreadSummary = { id: "talk", origin: "interactive", channel: "dashboard", updated_at: at(1), running: true };
  const kept = slimThreads([...scheduled, chat]);
  assert.equal(kept.length, MAX_PERSISTED_THREADS + 1);
  const last = kept[kept.length - 1];
  assert.equal(last.id, "talk");
  assert.equal("running" in last, false);
});

test("a broken or full store costs the copy, never an exception", () => {
  const store = memoryStore();
  store.items.set(`${CACHE_PREFIX}s\u0000agents`, "{not json");
  assert.equal(readCached(store, "s", "agents"), null);
  const full: CacheStore = {
    ...store,
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
  };
  assert.doesNotThrow(() => writeCached(full, "s", "agents", [], 1));
});

test("signing out drops every kept copy and nothing else", () => {
  const store = memoryStore();
  writeCached(store, "s1", "agents", [], 1);
  writeCached(store, "s2", "threads:x", [], 1);
  store.setItem("olano.theme", "dark");
  clearCached(store);
  assert.deepEqual([...store.items.keys()], ["olano.theme"]);
});

test("a kept page keeps its cursor and trims its rows like a list", () => {
  const store = memoryStore();
  const rows: ThreadSummary[] = Array.from({ length: MAX_PERSISTED_THREADS + 5 }, (_, i) => ({
    id: `t${i}`,
    origin: "interactive",
    channel: "dashboard",
    updated_at: at(1 + (i % 28)),
    running: true,
  }));
  writeCached(store, "s", "threads:aurelius", { threads: rows, has_more: true, next_before: "99" }, 1);
  const back = readCached<{ threads: ThreadSummary[]; has_more: boolean; next_before: string }>(store, "s", "threads:aurelius");
  assert.ok(back);
  assert.equal(back.data.threads.length, MAX_PERSISTED_THREADS);
  assert.equal(back.data.has_more, true);
  assert.equal(back.data.next_before, "99");
  assert.equal("running" in back.data.threads[0], false);
});
