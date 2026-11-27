// The text in a whole-number field and the number it stands for are two
// things. A field bound straight to the number (`value={n}`, `onChange` →
// `Number(text) || 0`) cannot be emptied: clearing it writes 0, React puts
// "0" back, and typing "900" next to it reads "0900". The field keeps the
// text the person typed; the form keeps the number this module derives.
//
// Pure so node's --test can drive it.

/** The number a field's text stands for: digits only, never negative.
 *  Empty (or nothing usable yet) is `fallback`, which is what gets saved. */
export function parseWholeNumber(text: string, fallback = 0): number {
  const trimmed = text.trim();
  if (!trimmed) return fallback;
  const n = Math.floor(Number(trimmed));
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, n);
}

/** Whether `text` already shows `value`, so an outside change of the value
 *  (a reload, a reset) should replace the text and an echo of the person's
 *  own typing should not. An empty field shows the fallback. */
export function textShowsValue(text: string, value: number, fallback = 0): boolean {
  return parseWholeNumber(text, fallback) === value;
}
