import { test } from "node:test";
import assert from "node:assert/strict";
import {
  clearNavGuard,
  confirmNavigation,
  hasNavGuard,
  isGuardedLinkClick,
  setNavGuard,
} from "./navGuard.ts";

const plainClick = {
  href: "http://app.test/agents",
  target: "",
  download: "",
  origin: "http://app.test",
  currentOrigin: "http://app.test",
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  altKey: false,
  button: 0,
};

test("no guard: navigation proceeds", async () => {
  clearNavGuard();
  assert.equal(hasNavGuard(), false);
  assert.equal(await confirmNavigation(), true);
});

test("the guard's answer is the answer", async () => {
  const off = setNavGuard(async () => false);
  assert.equal(hasNavGuard(), true);
  assert.equal(await confirmNavigation(), false);
  off();
  assert.equal(hasNavGuard(), false);
  assert.equal(await confirmNavigation(), true);
});

test("a guard that throws never traps the user", async () => {
  const off = setNavGuard(async () => {
    throw new Error("boom");
  });
  assert.equal(await confirmNavigation(), true);
  off();
});

test("unregistering only clears the guard that was registered", () => {
  // A page unmounting late must not wipe the guard a newer page installed.
  const offOld = setNavGuard(async () => false);
  const offNew = setNavGuard(async () => true);
  offOld();
  assert.equal(hasNavGuard(), true);
  offNew();
  assert.equal(hasNavGuard(), false);
});

test("only a plain left-click on a same-origin link counts", () => {
  assert.equal(isGuardedLinkClick(plainClick), true);
  // A new tab leaves nothing behind.
  assert.equal(isGuardedLinkClick({ ...plainClick, metaKey: true }), false);
  assert.equal(isGuardedLinkClick({ ...plainClick, ctrlKey: true }), false);
  assert.equal(isGuardedLinkClick({ ...plainClick, shiftKey: true }), false);
  assert.equal(isGuardedLinkClick({ ...plainClick, button: 1 }), false);
  assert.equal(isGuardedLinkClick({ ...plainClick, target: "_blank" }), false);
  assert.equal(isGuardedLinkClick({ ...plainClick, download: "x.txt" }), false);
  // External origins are beforeunload's territory.
  assert.equal(isGuardedLinkClick({ ...plainClick, origin: "https://docs.olano.ai" }), false);
  assert.equal(isGuardedLinkClick({ ...plainClick, href: "" }), false);
});
