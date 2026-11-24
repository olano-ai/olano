import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_MS, PAUSE_MS, SilenceDetector, WAIT_MS, WORDS_PAUSE_MS, WordsPauseDetector, phaseText } from "./handsFree.ts";

/** Feeds readings 100 ms apart and returns the verdicts. */
function run(detector: SilenceDetector, levels: number[], start = 0): string[] {
  return levels.map((level, i) => detector.feed(level, start + i * 100));
}

test("a sentence followed by a pause is done", () => {
  const d = new SilenceDetector();
  const quiet = Array.from({ length: PAUSE_MS / 100 + 1 }, () => 0.01);
  const verdicts = run(d, [0.01, 0.3, 0.4, 0.5, 0.3, ...quiet]);
  assert.equal(verdicts[2], "speaking");
  assert.equal(verdicts.at(-1), "done");
  assert.equal(d.heardSpeech, true);
});

test("a single loud reading is not speech", () => {
  const d = new SilenceDetector();
  assert.deepEqual(run(d, [0.5, 0.01, 0.01]), ["waiting", "waiting", "waiting"]);
});

test("a breath mid-sentence does not end it", () => {
  const d = new SilenceDetector();
  const verdicts = run(d, [0.3, 0.3, 0.01, 0.01, 0.01, 0.01, 0.3, 0.3]);
  assert.ok(!verdicts.includes("done"));
});

test("silence from the start times out", () => {
  const d = new SilenceDetector();
  assert.equal(d.feed(0, 0), "waiting");
  assert.equal(d.feed(0, WAIT_MS - 1), "waiting");
  assert.equal(d.feed(0, WAIT_MS), "timeout");
});

test("a note never runs past the limit", () => {
  const d = new SilenceDetector();
  run(d, [0.3, 0.3, 0.3]);
  assert.equal(d.feed(0.3, MAX_MS), "done");
});

test("the panel's words", () => {
  assert.equal(phaseText("thinking", "Nova").title, "Nova is working on it");
  assert.equal(phaseText("off", "Nova").title, "");
});

test("words that stop changing are the end of a sentence", () => {
  const d = new WordsPauseDetector();
  assert.equal(d.feed("", 0), "waiting");
  assert.equal(d.feed("What is", 300), "speaking");
  assert.equal(d.feed("What is the cheapest", 900), "speaking");
  assert.equal(d.feed("What is the cheapest", 900 + WORDS_PAUSE_MS - 100), "speaking");
  assert.equal(d.feed("What is the cheapest", 900 + WORDS_PAUSE_MS), "done");
});

test("a revision restarts the pause, spacing alone does not", () => {
  const d = new WordsPauseDetector();
  d.feed("Send it", 0);
  assert.equal(d.feed("Send it to Anna", WORDS_PAUSE_MS - 100), "speaking");
  assert.equal(d.feed("Send it to Anna ", 2 * WORDS_PAUSE_MS - 200), "speaking");
  assert.equal(d.feed("Send  it to Anna", 2 * WORDS_PAUSE_MS - 100), "done");
});

test("no words is a timeout, and a long talk is sent as it is", () => {
  const quiet = new WordsPauseDetector();
  quiet.feed("", 0);
  assert.equal(quiet.feed("", WAIT_MS - 1), "waiting");
  assert.equal(quiet.feed("", WAIT_MS), "timeout");
  const long = new WordsPauseDetector();
  long.feed("", 0);
  for (let t = 100; t < MAX_MS; t += 1000) long.feed(`word ${t}`, t);
  assert.equal(long.feed("still going", MAX_MS), "done");
});
