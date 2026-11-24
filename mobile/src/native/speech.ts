/**
 * Speech on the phone itself (the OlanoSpeech plugin: OlanoSpeechPlugin.swift
 * and .java): live dictation into the composer, a recorded voice note turned
 * into words, and a reply read aloud in the phone's own voice.
 *
 * - iPhone: all three. Dictation and transcription run on the phone when it
 *   supports the language; transcription falls back to Apple's recognizer,
 *   dictation does not (it is offered only where it stays on the phone).
 * - Android: dictation (Android 12 and later, with the phone's on-device
 *   recognizer) and reading aloud. Its recognizer cannot be handed a
 *   recording, so voice notes go to the deployment to be transcribed.
 * - A browser (`npm run dev`, the end-to-end tests): reading aloud through the
 *   browser's speech synthesis where it has one; no dictation, no transcription.
 *
 * Every caller treats "no" as normal: a voice note the phone cannot transcribe
 * is sent as audio, and a reply the phone cannot read says so.
 */

import { Capacitor, registerPlugin } from "@capacitor/core";

export interface RecognitionStatus {
  available: boolean;
  /** Transcribed on the phone (no audio leaves it) rather than by Apple. */
  onDevice: boolean;
  permission: "granted" | "denied" | "restricted" | "prompt";
  /** The language it listens for, like "en-US". */
  language: string;
}

export interface PhoneTranscript {
  /** "" when nothing was said. */
  text: string;
  onDevice: boolean;
  language: string;
}

export interface DictationStatus {
  /** Live dictation that stays on the phone works here, for `language`. */
  available: boolean;
  /** Both the microphone and speech recognition, where the phone asks for both. */
  permission: "granted" | "denied" | "restricted" | "prompt";
  language: string;
}

/**
 * What a dictation reports while it runs, as plugin events:
 * - `dictationText` `{ text, final }`: the current segment's words (chat/dictation.ts).
 * - `dictationLevel` `{ level }`: the microphone's loudness, 0 to 1, about ten a second.
 * - `dictationEnd` `{ reason, message }`: it stopped by itself ("error" or
 *   "interrupted", a call say); never sent for `stopDictation` or `cancelDictation`.
 */
export type DictationEvent = "dictationText" | "dictationLevel" | "dictationEnd";
export interface DictationEnd {
  reason: "error" | "interrupted";
  message?: string;
}

export interface ListenerHandle {
  remove(): Promise<void>;
}

/** What the screens need from speech on the phone. */
export interface SpeechEngine {
  recognitionStatus(options?: { language?: string }): Promise<RecognitionStatus>;
  /** Rejects with `code` "denied", "unavailable", "timeout" or "failed". */
  transcribe(options: { data: string; durationMs?: number; language?: string }): Promise<PhoneTranscript>;
  /** Resolves when reading ends: `finished` is false when it was stopped. */
  speak(options: { text: string; language?: string }): Promise<{ finished: boolean }>;
  stopSpeaking(): Promise<void>;
  /** Missing on an engine that cannot dictate (a browser, an app built before dictation). */
  dictationStatus?(options?: { language?: string }): Promise<DictationStatus>;
  /** Asks for permission the first time; resolves once listening. Rejects with `code` "denied", "unavailable" or "failed". */
  startDictation?(options?: { language?: string }): Promise<{ language: string }>;
  /** Stops listening; resolves after the last words have been reported. */
  stopDictation?(): Promise<void>;
  /** Stops listening and drops what was not yet reported. */
  cancelDictation?(): Promise<void>;
  addListener?(event: DictationEvent, listener: (data: unknown) => void): Promise<ListenerHandle>;
}

function refused(message: string, code: string): Error {
  return Object.assign(new Error(message), { code });
}

/** A browser: speech synthesis if it has it, no transcription. */
class BrowserSpeech implements SpeechEngine {
  private ending: ((finished: boolean) => void) | null = null;

  async recognitionStatus(): Promise<RecognitionStatus> {
    return { available: false, onDevice: false, permission: "prompt", language: "" };
  }

  async transcribe(): Promise<PhoneTranscript> {
    throw refused("This browser can't transcribe a recording.", "unavailable");
  }

  speak({ text }: { text: string }): Promise<{ finished: boolean }> {
    const synth = typeof speechSynthesis === "undefined" ? null : speechSynthesis;
    if (!synth || typeof SpeechSynthesisUtterance === "undefined") {
      return Promise.reject(refused("This browser has no voice to read with.", "unavailable"));
    }
    this.ending?.(false);
    synth.cancel();
    return new Promise((resolve) => {
      const utterance = new SpeechSynthesisUtterance(text);
      const end = (finished: boolean) => {
        if (this.ending !== end) return;
        this.ending = null;
        resolve({ finished });
      };
      this.ending = end;
      utterance.onend = () => end(true);
      utterance.onerror = () => end(false);
      synth.speak(utterance);
    });
  }

  async stopSpeaking(): Promise<void> {
    this.ending?.(false);
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
  }
}

const OlanoSpeech = registerPlugin<SpeechEngine>("OlanoSpeech", { web: () => new BrowserSpeech() });

/**
 * A stand-in phone for the end-to-end tests, which run the app in a browser
 * (no OlanoSpeech plugin there): `window.__olanoFakeSpeech`, set by the test.
 * Never consulted in the app on a phone.
 */
function standIn(): SpeechEngine | null {
  if (Capacitor.isNativePlatform() || typeof window === "undefined") return null;
  return (window as unknown as { __olanoFakeSpeech?: SpeechEngine }).__olanoFakeSpeech ?? null;
}

/** The engine in use. */
export function speechEngine(): SpeechEngine {
  return standIn() ?? OlanoSpeech;
}

/** The plugin's rejection code ("denied", "unavailable", ...), or "failed". */
export function speechErrorCode(error: unknown): string {
  const code = (error as { code?: unknown } | null)?.code;
  return typeof code === "string" && code ? code : "failed";
}
