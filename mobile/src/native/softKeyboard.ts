/*
 * Whether the phone's own on-screen keyboard is up.
 *
 * Its return key starts a new line in the composer and the send button sends,
 * so a message is never sent by a thumb that only meant to break a line. A
 * hardware keyboard (an iPad's, a desktop browser) keeps Enter to send,
 * Shift+Enter for a new line. With a hardware keyboard attached iOS still
 * reports a keyboard, the short shortcut bar, so the keyboard's height tells
 * the two apart.
 */

export const SOFT_KEYBOARD_CLASS = "olano-soft-keyboard";

/** Taller than the shortcut bar over a hardware keyboard, shorter than any phone keyboard. */
export const SOFT_KEYBOARD_MIN_HEIGHT = 150;

export function isSoftKeyboard(keyboardHeight: number | undefined): boolean {
  return (keyboardHeight ?? 0) >= SOFT_KEYBOARD_MIN_HEIGHT;
}

export function softKeyboardUp(): boolean {
  return typeof document !== "undefined" && document.documentElement.classList.contains(SOFT_KEYBOARD_CLASS);
}

/** Enter (without Shift, outside an IME composition) sends only from a hardware keyboard. */
export function enterSends(e: { key: string; shiftKey: boolean; isComposing: boolean }, softKeyboard = softKeyboardUp()): boolean {
  return e.key === "Enter" && !e.shiftKey && !e.isComposing && !softKeyboard;
}
