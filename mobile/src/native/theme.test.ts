import { strict as assert } from "node:assert";
import { test } from "node:test";

import { describePreference, resolveDark, resolvePalette, THEME_CHOICES } from "./theme.ts";

test("System follows the phone; Light and Dark override it", () => {
  assert.equal(resolveDark("system", true), true);
  assert.equal(resolveDark("system", false), false);
  assert.equal(resolveDark("dark", false), true);
  assert.equal(resolveDark("light", true), false);
});

test("Volt is a dark palette of its own, never what the phone picks", () => {
  assert.equal(resolvePalette("volt", false), "volt");
  assert.equal(resolvePalette("volt", true), "volt");
  // It rides the dark palette: status bar, keyboard and every dark rule follow.
  assert.equal(resolveDark("volt", false), true);
  // The phone's own setting can only ever yield the two platform palettes.
  assert.equal(resolvePalette("system", true), "dark");
  assert.equal(resolvePalette("system", false), "light");
  assert.equal(resolvePalette("light", true), "light");
  assert.equal(resolvePalette("dark", false), "dark");
});

test("every choice the segment offers has a footnote, and the e2e suite's two are verbatim", () => {
  for (const choice of THEME_CHOICES) assert.ok(describePreference(choice.value).length > 0, choice.value);
  assert.equal(describePreference("system"), "Follows your phone's light or dark setting.");
  assert.equal(describePreference("dark"), "Always dark, whatever your phone is set to.");
  assert.match(describePreference("volt"), /chartreuse/);
});
