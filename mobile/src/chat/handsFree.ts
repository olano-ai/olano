/**
 * Hands-free: talk, what you said goes when you stop talking, the reply is
 * read aloud, and the phone listens again. This file decides when someone has
 * finished talking. Pure, so node's test runner can import it.
 *
 * Where the phone dictates (native/dictation.ts) it listens with its own
 * recognizer and sends the words; WordsPauseDetector reads the end of
 * speech off them. Elsewhere it records a voice note, and SilenceDetector
 * reads it off the microphone's loudness (0 to 1, about ten readings a
 * second from the recorder):
 *
 * - Speech starts once the level stays above SPEAKING for a moment, so a cough
 *   or a door does not count.
 * - Speech ends after PAUSE_MS of quiet, long enough for a breath mid-sentence.
 * - Nobody says anything for WAIT_MS: listening stops (`timeout`), so a phone
 *   left on the seat does not keep its microphone open.
 * - A note never runs past MAX_MS: it is sent as it is.
 */

export const SPEAKING = 0.14;
export const QUIET = 0.07;
/** Readings above SPEAKING in a row that count as speech starting. */
export const START_READINGS = 2;
export const PAUSE_MS = 1600;
export const WAIT_MS = 15000;
export const MAX_MS = 120000;

export type Verdict = "waiting" | "speaking" | "done" | "timeout";

export class SilenceDetector {
  private startedAt: number | null = null;
  private loud = 0;
  private spoke = false;
  private quietSince: number | null = null;

  /** Feeds one loudness reading taken at `now` (milliseconds) and says where things stand. */
  feed(level: number, now: number): Verdict {
    if (this.startedAt === null) this.startedAt = now;
    if (now - this.startedAt >= MAX_MS) return this.spoke ? "done" : "timeout";
    if (!this.spoke) {
      this.loud = level >= SPEAKING ? this.loud + 1 : 0;
      if (this.loud >= START_READINGS) {
        this.spoke = true;
        this.quietSince = null;
        return "speaking";
      }
      return now - this.startedAt >= WAIT_MS ? "timeout" : "waiting";
    }
    if (level >= QUIET) {
      this.quietSince = null;
      return "speaking";
    }
    if (this.quietSince === null) this.quietSince = now;
    return now - this.quietSince >= PAUSE_MS ? "done" : "speaking";
  }

  get heardSpeech(): boolean {
    return this.spoke;
  }
}

/**
 * A recognizer revises its words for as long as someone talks and goes quiet
 * when they stop, so the words not changing for this long is the pause. A
 * little longer than PAUSE_MS: the last revision lands a beat after the sound.
 */
export const WORDS_PAUSE_MS = 1800;

/**
 * The end of speech from a live recognizer's words (the whole text so far,
 * fed about ten times a second): done once there are words and they have not
 * changed for WORDS_PAUSE_MS. Nothing heard in WAIT_MS is a timeout, and
 * MAX_MS sends what there is, as for a voice note.
 */
export class WordsPauseDetector {
  private startedAt: number | null = null;
  private last = "";
  private changedAt = 0;

  feed(words: string, now: number): Verdict {
    if (this.startedAt === null) this.startedAt = now;
    const text = words.replace(/\s+/g, " ").trim();
    if (now - this.startedAt >= MAX_MS) return text ? "done" : "timeout";
    if (!text) return now - this.startedAt >= WAIT_MS ? "timeout" : "waiting";
    if (text !== this.last) {
      this.last = text;
      this.changedAt = now;
      return "speaking";
    }
    return now - this.changedAt >= WORDS_PAUSE_MS ? "done" : "speaking";
  }
}

export type HandsFreePhase = "off" | "listening" | "sending" | "thinking" | "speaking" | "paused";

/** What the hands-free panel says in each phase. */
export function phaseText(phase: HandsFreePhase, agent: string): { title: string; hint: string } {
  switch (phase) {
    case "listening":
      return { title: "Listening", hint: "Talk, then pause. It sends when you stop." };
    case "sending":
      return { title: "Sending", hint: "" };
    case "thinking":
      return { title: `${agent} is working on it`, hint: "The answer is read aloud when it lands." };
    case "speaking":
      return { title: `${agent} is answering`, hint: "Tap to stop it and talk." };
    case "paused":
      return { title: "Paused", hint: "Tap to talk again." };
    default:
      return { title: "", hint: "" };
  }
}
