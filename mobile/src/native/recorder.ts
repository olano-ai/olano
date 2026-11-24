/**
 * Recording a voice note with the phone's microphone.
 *
 * The web view records through Web Audio: the microphone's stream (Capacitor
 * grants the page microphone access on both platforms once the phone has; iOS
 * asks with Info.plist's NSMicrophoneUsageDescription, Android with
 * RECORD_AUDIO) feeds a script processor that keeps the samples, brought down
 * to 16 kHz as they arrive, and measures how loud the speaker is for the live
 * waveform and the one the sent voice note is drawn with. Stopping writes the
 * samples out as a WAV file (see `chat/voice.ts` for why WAV, not the web
 * view's own recorder).
 *
 * The microphone and the audio context are shared and kept warm between
 * recordings (native/microphone.ts): on an iPhone opening them cost the first
 * words of every note. `start()` resolves once the first samples have
 * actually arrived, so a screen can show when talking counts, and `stop()`
 * waits for the samples still in flight, so the last syllable is kept.
 *
 * In a browser (`npm run dev`, the end-to-end tests) the same code records
 * from the browser's microphone, or Chromium's fake one.
 */

import {
  Downsampler,
  downsamplePeaks,
  MIN_VOICE_MS,
  VOICE_EXTENSION,
  VOICE_MIME,
  VOICE_SAMPLE_RATE,
  voiceName,
  wavBody,
  wavHeader,
  type PreparedVoice,
} from "../chat/voice.ts";
import { audioContext, canOpenMicrophone, openMicrophone, releaseMicrophone } from "./microphone.ts";

export class RecorderError extends Error {
  readonly reason: "denied" | "no_microphone" | "unsupported" | "failed";
  constructor(reason: RecorderError["reason"], message: string) {
    super(message);
    this.name = "RecorderError";
    this.reason = reason;
  }
}

export function canRecordVoice(): boolean {
  return canOpenMicrophone();
}

function toBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => {
      const result = String(reader.result || "");
      resolve(result.slice(result.indexOf(",") + 1));
    };
    reader.readAsDataURL(blob);
  });
}

/** Samples per processing buffer: about a tenth of a second at 44.1 or 48 kHz. */
const BUFFER_SIZE = 4096;
/** How long `start()` waits for the first samples before giving up on the signal. */
const FIRST_SAMPLES_MS = 1500;
/** How long `stop()` waits for the samples still in the pipeline (one buffer is ~85 ms at 48 kHz). */
const TAIL_MS = 150;

/** One recording: start, watch, then stop (to send) or cancel. */
export class VoiceRecorder {
  private stream: MediaStream | null = null;
  private ctx: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  // Held so the browser does not collect it (and stop recording) mid-note.
  private processor: ScriptProcessorNode | null = null;
  private sink: GainNode | null = null;
  private downsampler: Downsampler | null = null;
  private pcm: Int16Array[] = [];
  private samples = 0;
  private levels: number[] = [];
  private current = 0;
  private startedAt = 0;
  private finished = false;
  private heard = false;
  private onNextBuffer: (() => void) | null = null;

  /** Opens the microphone (asking the first time) and resolves once it hears. */
  async start(): Promise<void> {
    if (!canOpenMicrophone()) {
      throw new RecorderError("unsupported", "This phone can't record voice notes here.");
    }
    // Made inside the tap, before any await: iOS only lets a page start audio
    // from a gesture.
    try {
      this.ctx = audioContext();
    } catch {
      throw new RecorderError("unsupported", "This phone can't record voice notes here.");
    }
    try {
      this.stream = await openMicrophone();
    } catch (error) {
      this.release();
      const name = (error as { name?: string } | null)?.name ?? "";
      if (name === "NotAllowedError" || name === "SecurityError") {
        throw new RecorderError(
          "denied",
          "Olano isn't allowed to use the microphone. You can allow it in Settings, under Olano.",
        );
      }
      if (name === "NotFoundError" || name === "OverconstrainedError") {
        throw new RecorderError("no_microphone", "No microphone was found.");
      }
      throw new RecorderError("failed", "The microphone could not be started.");
    }
    if (this.finished) {
      // Cancelled while the phone was still asking.
      this.release();
      return;
    }
    const ctx = this.ctx;
    try {
      this.source = ctx.createMediaStreamSource(this.stream);
      this.processor = ctx.createScriptProcessor(BUFFER_SIZE, 1, 1);
      // The processor only runs while connected to the output; a silent gain
      // keeps the microphone out of the speaker.
      this.sink = ctx.createGain();
      this.sink.gain.value = 0;
    } catch {
      this.release();
      throw new RecorderError("failed", "The microphone could not be started.");
    }
    this.downsampler = new Downsampler(ctx.sampleRate, VOICE_SAMPLE_RATE);
    this.processor.onaudioprocess = (event) => this.take(event.inputBuffer.getChannelData(0));
    this.source.connect(this.processor);
    this.processor.connect(this.sink);
    this.sink.connect(ctx.destination);
    if (ctx.state !== "running") await ctx.resume().catch(() => undefined);
    // Recording counts from the first samples, not from the wiring: what the
    // timer shows is what the file holds.
    await this.nextBuffer(FIRST_SAMPLES_MS);
    if (!this.startedAt) this.startedAt = performance.now();
  }

  private take(input: Float32Array): void {
    if (this.finished || !this.downsampler) return;
    if (!this.heard) {
      this.heard = true;
      this.startedAt = performance.now();
    }
    let sum = 0;
    for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
    this.current = Math.min(1, Math.sqrt(sum / (input.length || 1)) * 5);
    this.levels.push(this.current);
    const out = this.downsampler.push(input);
    if (out.length) {
      this.pcm.push(out);
      this.samples += out.length;
    }
    const waiting = this.onNextBuffer;
    this.onNextBuffer = null;
    waiting?.();
  }

  /** Resolves on the next buffer of samples, or after `ms`. */
  private nextBuffer(ms: number): Promise<void> {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (this.onNextBuffer === done) this.onNextBuffer = null;
        resolve();
      }, ms);
      const done = () => {
        clearTimeout(timer);
        resolve();
      };
      this.onNextBuffer = done;
    });
  }

  get recording(): boolean {
    return !!this.processor && !this.finished;
  }

  /** Samples have arrived: talking counts from here. */
  get started(): boolean {
    return this.heard && !this.finished;
  }

  /** How loud the speaker is now, 0 to 1. */
  level(): number {
    return this.current;
  }

  /** The last `count` loudness readings, oldest first, for the live waveform. */
  recent(count: number): number[] {
    const out = this.levels.slice(-count);
    while (out.length < count) out.unshift(0);
    return out;
  }

  elapsedMs(): number {
    return this.startedAt ? performance.now() - this.startedAt : 0;
  }

  /** Stops and hands back the recording, or null when it was too short to be a message. */
  async stop(): Promise<PreparedVoice | null> {
    if (!this.processor || this.finished) {
      this.cancel();
      return null;
    }
    // The last buffer is still on its way when the finger lifts: wait for it.
    if (this.heard) await this.nextBuffer(TAIL_MS);
    this.finished = true;
    const rest = this.downsampler?.flush();
    if (rest?.length) {
      this.pcm.push(rest);
      this.samples += rest.length;
    }
    this.release();
    const durationMs = (this.samples / VOICE_SAMPLE_RATE) * 1000;
    if (durationMs < MIN_VOICE_MS) return null;
    const body = wavBody(this.pcm);
    const blob = new Blob([wavHeader(this.samples), body], { type: VOICE_MIME });
    this.pcm = [];
    return {
      name: voiceName(new Date(), VOICE_EXTENSION),
      mime: VOICE_MIME,
      data: await toBase64(blob),
      bytes: blob.size,
      durationMs,
      peaks: downsamplePeaks(this.levels),
      url: URL.createObjectURL(blob),
    };
  }

  /** Throws the recording away. */
  cancel(): void {
    this.finished = true;
    this.pcm = [];
    this.release();
  }

  private release(): void {
    const waiting = this.onNextBuffer;
    this.onNextBuffer = null;
    waiting?.();
    if (this.processor) this.processor.onaudioprocess = null;
    for (const node of [this.source, this.processor, this.sink]) {
      try {
        node?.disconnect();
      } catch {
        // Never connected.
      }
    }
    this.source = null;
    this.processor = null;
    this.sink = null;
    // The microphone and the context are shared: handed back, kept warm for
    // the next note, closed by microphone.ts when nobody records for a while.
    if (this.stream) releaseMicrophone();
    this.stream = null;
    this.ctx = null;
  }
}
