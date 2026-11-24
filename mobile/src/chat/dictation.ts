/**
 * Live dictation: the words appear in the composer while the person speaks,
 * worked out on the phone by its own speech recognizer, and what is sent is
 * the text, never the recording. This file is the part that needs no phone:
 * folding the recognizer's running guesses into one text, adding it to what
 * was already typed, and shaping the waveform. Pure, so `node --test` covers it.
 *
 * The recognizer reports in segments. Within one, every update replaces the
 * last (a later guess can rewrite earlier words, "I scream" becoming "ice
 * cream"); a `final` update closes it and the next words start a new one.
 * Both platforms restart their recognizer after a pause, which is what a new
 * segment is. iOS can also start over without saying so, after a long pause:
 * the guess suddenly shrinks to the first new word. That shape (a long guess
 * replaced by one under half its length) closes the old segment rather than
 * losing it.
 */

export interface DictationUpdate {
  /** The current segment's words so far. */
  text: string;
  /** The segment is over: these are its words for good. */
  final: boolean;
}

/** A guess at least this long that shrinks below half is a silent restart, not a revision. */
const RESTART_MIN_CHARS = 12;

/** Longest dictation the composer keeps listening for. */
export const MAX_DICTATION_MS = 5 * 60 * 1000;

function tidy(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

export class DictationText {
  private done: string[] = [];
  private current = "";

  feed(update: DictationUpdate): void {
    const text = tidy(update.text);
    if (!update.final && this.current.length >= RESTART_MIN_CHARS && text.length * 2 < this.current.length && !this.current.startsWith(text)) {
      this.done.push(this.current);
    }
    if (update.final) {
      if (text) this.done.push(text);
      this.current = "";
    } else {
      this.current = text;
    }
  }

  /** Everything heard so far, segments joined. */
  get text(): string {
    return tidy([...this.done, this.current].join(" "));
  }
}

/** The dictated words after what was already in the box, on the same line. */
export function withDictation(draft: string, dictated: string): string {
  const said = tidy(dictated);
  if (!said) return draft;
  const typed = draft.replace(/\s+$/, "");
  if (!typed) return said;
  return /\n$/.test(draft) ? `${draft}${said}` : `${typed} ${said}`;
}

/** Below this a reading draws as a dot: the room is quiet. */
export const QUIET_LEVEL = 0.12;

/**
 * The newest `count` loudness readings (0 to 1) as the waveform draws them,
 * oldest first, padded with quiet on the left so the line fills from the
 * right as it starts. `null` is a dot; a number is a bar's height (0 to 1).
 */
export function waveform(levels: readonly number[], count: number): (number | null)[] {
  const recent = levels.slice(-count);
  const shown: (number | null)[] = recent.map((level) => {
    const clamped = Math.max(0, Math.min(1, level));
    return clamped < QUIET_LEVEL ? null : Math.max(0.2, clamped);
  });
  while (shown.length < count) shown.unshift(null);
  return shown;
}
