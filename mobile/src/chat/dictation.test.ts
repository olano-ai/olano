import assert from "node:assert/strict";
import { test } from "node:test";

import { DictationText, QUIET_LEVEL, waveform, withDictation } from "./dictation.ts";

test("a segment's guesses replace each other, a final one is kept", () => {
  const t = new DictationText();
  t.feed({ text: "I scream", final: false });
  assert.equal(t.text, "I scream");
  t.feed({ text: "Ice cream for", final: false });
  assert.equal(t.text, "Ice cream for");
  t.feed({ text: "Ice cream for everyone.", final: true });
  assert.equal(t.text, "Ice cream for everyone.");
  t.feed({ text: "Thanks", final: false });
  assert.equal(t.text, "Ice cream for everyone. Thanks");
});

test("a silent restart (the guess collapses to a new first word) keeps what was said", () => {
  const t = new DictationText();
  t.feed({ text: "Check the Stripe pricing", final: false });
  t.feed({ text: "Make", final: false });
  assert.equal(t.text, "Check the Stripe pricing Make");
  t.feed({ text: "Make sure", final: false });
  assert.equal(t.text, "Check the Stripe pricing Make sure");
});

test("a revision that only shortens a little is still a revision", () => {
  const t = new DictationText();
  t.feed({ text: "Send it to the team", final: false });
  t.feed({ text: "Send it to the tea", final: false });
  assert.equal(t.text, "Send it to the tea");
  // Shorter than half, but the start of what was there: the recognizer backing up.
  t.feed({ text: "Send", final: false });
  assert.equal(t.text, "Send");
});

test("an empty final segment adds nothing", () => {
  const t = new DictationText();
  t.feed({ text: "Hello", final: true });
  t.feed({ text: "  ", final: true });
  assert.equal(t.text, "Hello");
});

test("dictated words join what was typed", () => {
  assert.equal(withDictation("", "hello there"), "hello there");
  assert.equal(withDictation("Quick note:", " about pricing "), "Quick note: about pricing");
  assert.equal(withDictation("Quick note: ", "about pricing"), "Quick note: about pricing");
  assert.equal(withDictation("Line one\n", "line two"), "Line one\nline two");
  assert.equal(withDictation("Unchanged ", "  "), "Unchanged ");
});

test("the waveform fills from the right, quiet as dots", () => {
  assert.deepEqual(waveform([0.5], 3), [null, null, 0.5]);
  assert.deepEqual(waveform([QUIET_LEVEL / 2, 0.9, 2, -1], 4), [null, 0.9, 1, null]);
  assert.deepEqual(waveform([0.13], 1), [0.2]);
  assert.equal(waveform(Array(50).fill(0.5), 40).length, 40);
});
