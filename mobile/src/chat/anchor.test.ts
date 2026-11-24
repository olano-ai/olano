import assert from "node:assert/strict";
import { test } from "node:test";

import { anchoredScrollTop, distanceFromBottom, scrollIsSettled } from "./anchor.ts";

// A 2000px conversation in an 800px window, scrolled to the bottom.
const atBottom = { scrollTop: 1200, scrollHeight: 2000, clientHeight: 800 };

test("the keyboard opening keeps the newest message above it", () => {
  // The window shrinks by the keyboard; scrollTop is left where it was.
  const box = { ...atBottom, clientHeight: 500 };
  assert.equal(anchoredScrollTop(box, "viewport", true, 0), 1500);
});

test("someone reading further up keeps their distance from the bottom", () => {
  const before = { ...atBottom, scrollTop: 700 };
  const fromBottom = distanceFromBottom(before);
  assert.equal(fromBottom, 500);
  const box = { ...before, clientHeight: 500 };
  assert.equal(anchoredScrollTop(box, "viewport", false, fromBottom), 1000);
});

test("the composer growing (more bottom padding) lifts the conversation by as much", () => {
  const box = { ...atBottom, scrollHeight: 2040 };
  assert.equal(anchoredScrollTop(box, "viewport", true, 0), 1240);
});

test("the keyboard closing never scrolls past the top", () => {
  const box = { scrollTop: 100, scrollHeight: 900, clientHeight: 800 };
  assert.equal(anchoredScrollTop(box, "viewport", false, 400), 0);
});

test("a reply's buttons appearing are followed only from the bottom", () => {
  const grown = { ...atBottom, scrollHeight: 2060 };
  assert.equal(anchoredScrollTop(grown, "messages", true, 0), 1260);
  assert.equal(anchoredScrollTop({ ...grown, scrollTop: 400 }, "messages", false, 860), null);
});

test("already in place: nothing to do", () => {
  assert.equal(anchoredScrollTop(atBottom, "messages", true, 0), null);
  assert.equal(anchoredScrollTop(atBottom, "viewport", false, 0), null);
});

test("a scroll taken mid-resize is not the person's position", () => {
  // The box the anchor answered for was 886px; the keyboard has already made
  // it 516px, and a scroll event from the earlier clamp arrives now.
  assert.equal(scrollIsSettled(516, 886), false);
  assert.equal(scrollIsSettled(886, 886), true);
  // Before the anchor has measured anything, scrolls count as usual.
  assert.equal(scrollIsSettled(852, 0), true);
});
