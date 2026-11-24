import { test } from "node:test";
import assert from "node:assert/strict";

import {
  formatRecoveryKey,
  isValidRecoveryKey,
  normalizeRecoveryKey,
  RECOVERY_KEY_LENGTH,
} from "./recoveryKey.ts";

const KEY = "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U";  // gitleaks:allow

test("the sealer's dashed spelling round-trips through normalise and format", () => {
  assert.equal(KEY.length, RECOVERY_KEY_LENGTH);
  const dashed = formatRecoveryKey(KEY);
  assert.equal(dashed, "MFRG-GZDF-MZTW-Q2LK-NNWG-23TP-OBYX-E43U");
  assert.equal(normalizeRecoveryKey(dashed), KEY);
  assert.equal(formatRecoveryKey(normalizeRecoveryKey(dashed)), dashed);
});

test("careless typing still confirms: lower case, spaces, tabs and line breaks", () => {
  const typed = " mfrg gzdf\tmztw-q2lk\nnnwg 23tp obyx e43u ";
  assert.equal(isValidRecoveryKey(typed), true);
  assert.equal(normalizeRecoveryKey(typed), "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U");
});

test("a key that is too short, too long or outside base32 is refused", () => {
  const good = "MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U";
  assert.equal(isValidRecoveryKey(good), true);
  assert.equal(isValidRecoveryKey(good.slice(0, 31)), false);
  assert.equal(isValidRecoveryKey(good + "A"), false);
  // 0, 1, 8 and 9 are not in the RFC 4648 alphabet: a typo, not a key.
  for (const bad of ["0", "1", "8", "9", "!"]) {
    assert.equal(isValidRecoveryKey(bad + good.slice(1)), false);
  }
  assert.equal(isValidRecoveryKey(""), false);
});

test("formatting never invents characters", () => {
  assert.equal(formatRecoveryKey(""), "");
  assert.equal(formatRecoveryKey("abc"), "ABC");
  assert.equal(formatRecoveryKey("abcde"), "ABCD-E");
});
