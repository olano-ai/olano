import { test } from "node:test";
import assert from "node:assert/strict";
import { parseWholeNumber, textShowsValue } from "./wholeNumber.ts";

test("an emptied field saves the fallback but is not refilled", () => {
  assert.equal(parseWholeNumber(""), 0);
  // The echo of the person's own clearing must not put "0" back.
  assert.equal(textShowsValue("", 0), true);
});

test("clearing then typing gives the typed number, not a leading zero", () => {
  assert.equal(parseWholeNumber("900"), 900);
  assert.equal(textShowsValue("900", 900), true);
});

test("never negative, never fractional, never NaN", () => {
  assert.equal(parseWholeNumber("-5"), 0);
  assert.equal(parseWholeNumber("12.7"), 12);
  assert.equal(parseWholeNumber("abc"), 0);
  assert.equal(parseWholeNumber("  42 "), 42);
});

test("an outside change of the value replaces the text", () => {
  assert.equal(textShowsValue("500", 900), false);
});

test("a non-zero fallback is what an empty field means", () => {
  assert.equal(parseWholeNumber("", 3), 3);
  assert.equal(textShowsValue("", 3, 3), true);
});
