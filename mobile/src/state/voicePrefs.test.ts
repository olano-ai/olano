import { strict as assert } from "node:assert";
import { test } from "node:test";

import { DEFAULT_VOICE_PREFS, parseVoicePrefs, readsAloud } from "./voicePrefs.ts";

test("stored voice settings come back, anything else is the default", () => {
  assert.deepEqual(parseVoicePrefs(null), DEFAULT_VOICE_PREFS);
  assert.deepEqual(parseVoicePrefs("not json"), DEFAULT_VOICE_PREFS);
  assert.deepEqual(parseVoicePrefs('{"transcribeOn":"deployment","readAloud":"never"}'), {
    transcribeOn: "deployment",
    readAloud: "never",
    transcribeChosen: false,
    dictation: true,
  });
  assert.equal(parseVoicePrefs('{"dictation":false}').dictation, false);
  assert.equal(parseVoicePrefs('{"transcribeOn":"phone","transcribeChosen":true}').transcribeChosen, true);
  assert.deepEqual(parseVoicePrefs('{"transcribeOn":"mars","readAloud":"sometimes"}'), DEFAULT_VOICE_PREFS);
});

test("replies are read aloud after a voice note by default", () => {
  assert.equal(readsAloud(DEFAULT_VOICE_PREFS, true), true);
  assert.equal(readsAloud(DEFAULT_VOICE_PREFS, false), false);
  assert.equal(readsAloud({ ...DEFAULT_VOICE_PREFS, readAloud: "always" }, false), true);
  assert.equal(readsAloud({ ...DEFAULT_VOICE_PREFS, readAloud: "never" }, true), false);
});
