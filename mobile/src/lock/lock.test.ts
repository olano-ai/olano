import assert from "node:assert/strict";
import { test } from "node:test";

import { lockName, locksOnReturn, parseLockPrefs } from "./lock.ts";

test("stored settings, with anything odd back at off", () => {
  assert.deepEqual(parseLockPrefs(null), { enabled: false, afterSeconds: 0 });
  assert.deepEqual(parseLockPrefs('{"enabled":true,"afterSeconds":300}'), { enabled: true, afterSeconds: 300 });
  assert.deepEqual(parseLockPrefs('{"enabled":"yes","afterSeconds":42}'), { enabled: false, afterSeconds: 0 });
  assert.deepEqual(parseLockPrefs("not json"), { enabled: false, afterSeconds: 0 });
});

test("when coming back asks again", () => {
  const off = { enabled: false, afterSeconds: 0 };
  const every = { enabled: true, afterSeconds: 0 };
  const five = { enabled: true, afterSeconds: 300 };
  assert.equal(locksOnReturn(off, 0, 10_000_000), false);
  assert.equal(locksOnReturn(every, 1000, 1001), true);
  assert.equal(locksOnReturn(five, 0, 299_000), false);
  assert.equal(locksOnReturn(five, 0, 300_000), true);
  assert.equal(locksOnReturn(five, null, 0), true);
});

test("the unlock's name", () => {
  assert.equal(lockName("face", "ios"), "Face ID");
  assert.equal(lockName("fingerprint", "ios"), "Touch ID");
  assert.equal(lockName("biometric", "android"), "fingerprint or face unlock");
  assert.equal(lockName("passcode", "android"), "passcode");
});
