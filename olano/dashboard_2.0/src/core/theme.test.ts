import { test } from "node:test";
import assert from "node:assert/strict";

// Minimal browser stand-ins: theme.ts writes localStorage and <html data-theme>.
const store = new Map<string, string>();
(globalThis as unknown as { localStorage: Storage }).localStorage = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
  key: () => null,
  length: 0,
} as Storage;
(globalThis as unknown as { document: unknown }).document = { documentElement: { dataset: {} as Record<string, string> } };

const { getTheme, setTheme, cycleTheme, subscribeTheme, THEMES } = await import("./theme.ts");

test("every subscriber hears a change made anywhere", () => {
  const seenA: string[] = [];
  const seenB: string[] = [];
  const offA = subscribeTheme(() => seenA.push(getTheme()));
  const offB = subscribeTheme(() => seenB.push(getTheme()));
  setTheme("midnight"); // e.g. the account menu's select
  assert.deepEqual(seenA, ["midnight"]);
  assert.deepEqual(seenB, ["midnight"]);
  assert.equal(store.get("lc-theme"), "midnight");
  offA();
  offB();
});

test("the sidebar cycle advances from the theme another surface set", () => {
  setTheme("light");
  const next = cycleTheme();
  assert.equal(next, THEMES[(THEMES.indexOf("light") + 1) % THEMES.length]);
  assert.equal(getTheme(), next);
});

test("an unsubscribed listener is not called", () => {
  let calls = 0;
  const off = subscribeTheme(() => calls++);
  off();
  setTheme("dark");
  assert.equal(calls, 0);
});
