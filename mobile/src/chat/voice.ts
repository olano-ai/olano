/**
 * Voice notes in a conversation: the recording format (WAV), the waveform a
 * voice note is drawn with, and the transcript the runtime writes for it.
 *
 * A voice note travels like a photo: `attachments` on `POST /api/chat/stream`,
 * here `{kind: "audio", name, mime, data}`. The runtime saves it in the
 * agent's `uploads/`, transcribes it when speech-to-text is on, and writes
 * both into the message for the agent: "[Audio attached: <name> <dash> saved
 * to <path> (...)]" and "[Voice message transcript] <words>" (or a note that
 * it could not transcribe). A reloaded message gets that text back with the
 * recording as a media item; `splitVoiceNotes` takes the transcript out for
 * the voice note to show and leaves the rest to `stripAttachmentNotes`.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

import type { MediaRef } from "../shared/dashboard.ts";

/** A voice note in a message: one this phone just recorded, or one on the deployment. */
export interface MessageVoice {
  key: string;
  name: string;
  /** Played directly (an object URL of a recording this phone made). */
  src?: string;
  /** Fetched with the session (a deployment path like /api/files-raw/...). */
  url?: string;
  durationMs?: number;
  /** Waveform bar heights, 0 to 1. */
  peaks?: number[];
}

/** A recording ready to send. */
export interface PreparedVoice {
  name: string;
  mime: string;
  /** Base64 of the recording. */
  data: string;
  bytes: number;
  durationMs: number;
  peaks: number[];
  /** An object URL of the recording, for the sent message. */
  url: string;
}

export function voiceAttachment(voice: PreparedVoice): { kind: "audio"; name: string; mime: string; data: string } {
  return { kind: "audio", name: voice.name, mime: voice.mime.split(";")[0].trim(), data: voice.data };
}

/**
 * Longest voice note, after which recording stops and the note is sent. Five
 * minutes of 16 kHz WAV is 9.6 MB, inside the deployment's 10 MB per-file cap.
 */
export const MAX_VOICE_MS = 5 * 60 * 1000;
/** Shorter than this is a slip of the finger, not a message. */
export const MIN_VOICE_MS = 700;
/** Bars in a voice note's waveform. */
export const WAVE_BARS = 40;

/**
 * Voice notes are recorded as WAV: 16-bit mono PCM at 16 kHz, the rate
 * speech-to-text models work at. Every speech-to-text service reads WAV and
 * every phone plays it. A web view's own recorder would hand back a format
 * per platform, and an iPhone's (fragmented MP4) is one transcription
 * services refuse, which is how the first iPhone voice notes arrived
 * "not transcribed".
 */
export const VOICE_SAMPLE_RATE = 16000;
export const VOICE_MIME = "audio/wav";
export const VOICE_EXTENSION = ".wav";

/**
 * Brings microphone audio (usually 44.1 or 48 kHz floats) down to 16 kHz
 * 16-bit samples as it arrives, a buffer at a time, so a five-minute note
 * holds 9.6 MB rather than 58 MB of floats. Each output sample is the mean
 * of the input samples it covers, which also filters out what 16 kHz cannot
 * carry.
 */
export class Downsampler {
  private readonly step: number;
  private sum = 0;
  private count = 0;
  /** Input samples left until the next output sample is due. */
  private due: number;

  constructor(inputRate: number, outputRate = VOICE_SAMPLE_RATE) {
    this.step = Math.max(1, inputRate / outputRate);
    this.due = this.step;
  }

  push(input: Float32Array): Int16Array {
    const out = new Int16Array(Math.ceil(input.length / this.step) + 1);
    let n = 0;
    for (let i = 0; i < input.length; i++) {
      this.sum += input[i];
      this.count += 1;
      this.due -= 1;
      if (this.due <= 0) {
        out[n++] = toInt16(this.sum / this.count);
        this.sum = 0;
        this.count = 0;
        this.due += this.step;
      }
    }
    return out.subarray(0, n);
  }

  /** What is left over at the end of the recording. */
  flush(): Int16Array {
    if (!this.count) return new Int16Array(0);
    const last = toInt16(this.sum / this.count);
    this.sum = 0;
    this.count = 0;
    return Int16Array.of(last);
  }
}

function toInt16(sample: number): number {
  const clamped = Math.max(-1, Math.min(1, sample));
  return Math.round(clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff);
}

/** The 44-byte header of a 16-bit mono PCM WAV file holding `samples` samples. */
export function wavHeader(samples: number, sampleRate = VOICE_SAMPLE_RATE): Uint8Array<ArrayBuffer> {
  const bytes = samples * 2;
  const buffer = new ArrayBuffer(44);
  const header = new DataView(buffer);
  const text = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++) header.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, "RIFF");
  header.setUint32(4, 36 + bytes, true);
  text(8, "WAVE");
  text(12, "fmt ");
  header.setUint32(16, 16, true); // fmt chunk size
  header.setUint16(20, 1, true); // PCM
  header.setUint16(22, 1, true); // mono
  header.setUint32(24, sampleRate, true);
  header.setUint32(28, sampleRate * 2, true); // bytes per second
  header.setUint16(32, 2, true); // bytes per sample frame
  header.setUint16(34, 16, true); // bits per sample
  text(36, "data");
  header.setUint32(40, bytes, true);
  return new Uint8Array(buffer);
}

/**
 * The samples as WAV bytes, little-endian whatever the platform (every phone
 * is little-endian already, so this is a copy, never a conversion).
 */
export function wavBody(chunks: Int16Array[]): Uint8Array<ArrayBuffer> {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const buffer = new ArrayBuffer(total * 2);
  const out = new DataView(buffer);
  let offset = 0;
  for (const chunk of chunks) {
    for (let i = 0; i < chunk.length; i++, offset += 2) out.setInt16(offset, chunk[i], true);
  }
  return new Uint8Array(buffer);
}

/** A voice note's file name: when it was recorded. */
export function voiceName(date: Date, extension: string): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
  return `voice-${stamp}${extension}`;
}

/** "0:07", "1:05", "12:30". */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) ms = 0;
  const total = Math.round(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/**
 * `bars` heights between 0.12 and 1 from loudness samples (any count, any
 * scale): each bar is the loudest sample in its slice, scaled to the loudest
 * overall, so a quiet recording still draws a shape.
 */
export function downsamplePeaks(levels: number[], bars = WAVE_BARS): number[] {
  const floor = 0.12;
  if (!levels.length) return new Array(bars).fill(floor);
  const out: number[] = [];
  for (let i = 0; i < bars; i++) {
    const start = Math.floor((i * levels.length) / bars);
    const end = Math.max(start + 1, Math.floor(((i + 1) * levels.length) / bars));
    let peak = 0;
    for (let j = start; j < end && j < levels.length; j++) peak = Math.max(peak, Math.abs(levels[j]));
    out.push(peak);
  }
  const loudest = Math.max(...out);
  return out.map((p) => (loudest > 0 ? floor + (1 - floor) * (p / loudest) : floor));
}

/** A stable, natural-looking waveform for a recording that could not be decoded. */
export function placeholderPeaks(seed: string, bars = WAVE_BARS): number[] {
  let h = 2166136261;
  for (const ch of seed) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  const out: number[] = [];
  for (let i = 0; i < bars; i++) {
    h = Math.imul(h ^ (h >>> 13), 1274126177);
    const noise = ((h >>> 0) % 1000) / 1000;
    // Speech swells and fades: a gentle envelope under the noise.
    const envelope = 0.55 + 0.45 * Math.sin((Math.PI * (i + 0.5)) / bars);
    out.push(0.15 + 0.85 * envelope * (0.35 + 0.65 * noise));
  }
  return out;
}

/** A media item that is a voice note: any audio, and a voice recording saved as WebM (which the runtime lists as video). */
export function isVoiceMedia(item: Pick<MediaRef, "kind" | "name">): boolean {
  if (item.kind === "audio") return true;
  return item.kind === "video" && /^voice-[\w-]*\.(webm|m4a|mp4|ogg)$/i.test(item.name || "");
}

const TRANSCRIPT = /^\[Voice message transcript\]\s*(.*)$/;

/**
 * The message a voice note becomes once the phone has its words: the line
 * the runtime itself writes for a transcribed voice note, so the agent, a
 * reloaded conversation and the dashboard all read it as one. On one line,
 * because that is how the transcript is read back.
 */
export function voiceTranscriptMessage(words: string): string {
  return `[Voice message transcript] ${words.replace(/\s+/g, " ").trim()}`;
}
const NOT_TRANSCRIBED = /^\[A voice message was received but .*\]$/;

/**
 * Takes the runtime's voice notes out of a message: the transcript of each
 * recording, and whether one could not be transcribed. The rest of the text
 * comes back unchanged.
 */
export function splitVoiceNotes(text: string): { text: string; transcripts: string[]; untranscribed: boolean } {
  if (!text || !text.includes("[")) return { text, transcripts: [], untranscribed: false };
  const transcripts: string[] = [];
  let untranscribed = false;
  const kept: string[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    const match = TRANSCRIPT.exec(trimmed);
    if (match) {
      if (match[1].trim()) transcripts.push(match[1].trim());
      continue;
    }
    if (NOT_TRANSCRIBED.test(trimmed)) {
      untranscribed = true;
      continue;
    }
    kept.push(line);
  }
  return { text: kept.join("\n"), transcripts, untranscribed };
}
