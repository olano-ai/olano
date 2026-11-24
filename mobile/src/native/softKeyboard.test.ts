import { test } from "node:test";
import assert from "node:assert/strict";

import { enterSends, isSoftKeyboard } from "./softKeyboard.ts";

const enter = { key: "Enter", shiftKey: false, isComposing: false };

test("the phone's keyboard is told apart from the shortcut bar over a hardware keyboard", () => {
  assert.equal(isSoftKeyboard(336), true);
  assert.equal(isSoftKeyboard(55), false);
  assert.equal(isSoftKeyboard(undefined), false);
});

test("return on the phone's keyboard starts a new line", () => {
  assert.equal(enterSends(enter, true), false);
});

test("Enter on a hardware keyboard sends, Shift+Enter and IME composition do not", () => {
  assert.equal(enterSends(enter, false), true);
  assert.equal(enterSends({ ...enter, shiftKey: true }, false), false);
  assert.equal(enterSends({ ...enter, isComposing: true }, false), false);
  assert.equal(enterSends({ ...enter, key: "a" }, false), false);
});
