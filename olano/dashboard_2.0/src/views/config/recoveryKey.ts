// The recovery key's text form, shared by the ceremony dialog and its tests.
//
// The sealer generates 160 random bits and shows them once as RFC 4648 base32
// (upper-case letters A-Z and digits 2-7, never 0/1/8/9), 32 characters in
// eight groups of four joined by dashes. What unlocks the volume is the 32
// normalised characters: a rescue prompt takes them typed without dashes, and
// the confirm step of the ceremony sends the same normalised form, so a key
// pasted with spaces, typed in lower case or split across lines still
// confirms. Pure module (no React, no DOM): node --test imports it directly.

export const RECOVERY_KEY_LENGTH = 32;
const GROUP = 4;
const ALPHABET = /^[A-Z2-7]+$/;

/** Strip dashes and whitespace, upper-case. Never validates; see isValidRecoveryKey. */
export function normalizeRecoveryKey(text: string): string {
  return text.replace(/[-\s]/g, "").toUpperCase();
}

/** True when the normalised text is exactly 32 base32 characters. */
export function isValidRecoveryKey(text: string): boolean {
  const key = normalizeRecoveryKey(text);
  return key.length === RECOVERY_KEY_LENGTH && ALPHABET.test(key);
}

/** The display form: eight dash-separated groups of four (the sealer's own spelling). */
export function formatRecoveryKey(text: string): string {
  const key = normalizeRecoveryKey(text);
  const groups: string[] = [];
  for (let i = 0; i < key.length; i += GROUP) groups.push(key.slice(i, i + GROUP));
  return groups.join("-");
}
