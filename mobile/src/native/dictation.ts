/**
 * One live dictation at a time, over the OlanoSpeech plugin (native/speech.ts):
 * start it, hear its words and loudness as they come, stop it for the final
 * text or cancel it. The screens never touch the plugin's events directly.
 */

import { speechEngine, speechErrorCode, type DictationEnd, type DictationStatus, type ListenerHandle } from "./speech.ts";
import { DictationText } from "../chat/dictation.ts";

export interface DictationHandlers {
  /** Everything heard so far, after every update. */
  onText(text: string): void;
  onLevel(level: number): void;
  /** It stopped by itself (an error, a phone call); `text` is what was heard. */
  onEnd(text: string, end: DictationEnd): void;
}

let status: Promise<DictationStatus | null> | null = null;

/** What the phone can do, asked once per launch (and again after a refusal). */
export function dictationStatus(): Promise<DictationStatus | null> {
  status ??= (async () => {
    const engine = speechEngine();
    if (!engine.dictationStatus) return null;
    try {
      return await engine.dictationStatus();
    } catch {
      // An app built before dictation, or a browser.
      return null;
    }
  })();
  return status;
}

/** Live dictation can be offered: the phone supports it and nobody turned the permission off. */
export async function dictationAvailable(): Promise<boolean> {
  const s = await dictationStatus();
  return !!s && s.available && s.permission !== "denied" && s.permission !== "restricted";
}

/** Asked again next time: the permission may have changed in the phone's Settings. */
export function forgetDictationStatus(): void {
  status = null;
}

export class DictationError extends Error {
  readonly code: string;
  constructor(message: string, code: string) {
    super(message);
    this.code = code;
  }
}

const MESSAGES: Record<string, string> = {
  denied: "Olano isn't allowed to use the microphone or speech recognition. Allow both in your phone's Settings, or hold the microphone to record a voice note.",
  unavailable: "Dictation isn't available for your language on this phone. Hold the microphone to record a voice note instead.",
};

export class LiveDictation {
  private readonly transcript = new DictationText();
  private readonly handles: ListenerHandle[] = [];
  private live = false;

  constructor(private readonly handlers: DictationHandlers) {}

  get text(): string {
    return this.transcript.text;
  }

  /** Listening once this resolves; rejects with a DictationError the person can read. */
  async start(): Promise<void> {
    const engine = speechEngine();
    if (!engine.startDictation || !engine.addListener) throw new DictationError(MESSAGES.unavailable, "unavailable");
    this.live = true;
    try {
      this.handles.push(
        await engine.addListener("dictationText", (data) => {
          const update = data as { text?: unknown; final?: unknown };
          if (!this.live) return;
          this.transcript.feed({ text: String(update.text ?? ""), final: update.final === true });
          this.handlers.onText(this.transcript.text);
        }),
        await engine.addListener("dictationLevel", (data) => {
          if (this.live) this.handlers.onLevel(Number((data as { level?: unknown }).level) || 0);
        }),
        await engine.addListener("dictationEnd", (data) => {
          if (!this.live) return;
          this.live = false;
          void this.release();
          const end = data as Partial<DictationEnd>;
          this.handlers.onEnd(this.transcript.text, { reason: end.reason === "interrupted" ? "interrupted" : "error", message: end.message });
        }),
      );
      await engine.startDictation();
    } catch (error) {
      this.live = false;
      await this.release();
      const code = speechErrorCode(error);
      if (code === "denied") forgetDictationStatus();
      throw new DictationError(MESSAGES[code] ?? "Dictation could not start. Try again, or hold the microphone to record a voice note.", code);
    }
  }

  /** Stops listening; resolves with everything heard, the last words included. */
  async stop(): Promise<string> {
    if (this.live) {
      try {
        await speechEngine().stopDictation?.();
      } catch {
        // Whatever was heard is still here.
      }
    }
    this.live = false;
    await this.release();
    return this.transcript.text;
  }

  async cancel(): Promise<void> {
    const was = this.live;
    this.live = false;
    await this.release();
    if (was) await speechEngine().cancelDictation?.().catch(() => undefined);
  }

  private async release(): Promise<void> {
    const handles = this.handles.splice(0);
    await Promise.all(handles.map((h) => h.remove().catch(() => undefined)));
  }
}
