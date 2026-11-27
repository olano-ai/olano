import { test } from "node:test";
import assert from "node:assert/strict";
import { landingFor, recordSignIn, recordSignOut, shouldResetRoute, type KeyValueStore } from "./signInLanding.ts";

function memStore(): KeyValueStore {
  const m = new Map<string, string>();
  return {
    getItem: (k) => m.get(k) ?? null,
    setItem: (k, v) => void m.set(k, v),
    removeItem: (k) => void m.delete(k),
  };
}

test("a first sign-in keeps the URL (a shared deep link still works)", () => {
  assert.equal(shouldResetRoute(null, "u1", false), false);
  assert.deepEqual(landingFor(memStore(), "u1"), { reset: false });
});

test("the same person after a lapsed session goes back to their page", () => {
  const s = memStore();
  recordSignIn(s, "u1");
  assert.deepEqual(landingFor(s, "u1"), { reset: false });
});

test("a different person on the same browser starts at home", () => {
  const s = memStore();
  recordSignIn(s, "u1");
  assert.deepEqual(landingFor(s, "u2"), { reset: true });
});

test("after a deliberate sign-out, the next sign-in starts at home, once", () => {
  const s = memStore();
  recordSignIn(s, "u1");
  recordSignOut(s);
  assert.deepEqual(landingFor(s, "u1"), { reset: true });
  recordSignIn(s, "u1");
  assert.deepEqual(landingFor(s, "u1"), { reset: false });
});

test("no storage (blocked site data) never resets", () => {
  assert.deepEqual(landingFor(null, "u1"), { reset: false });
});
