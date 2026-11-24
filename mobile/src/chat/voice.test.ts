import { strict as assert } from "node:assert";
import { test } from "node:test";

import { filesOf, foldTranscript, voicesOf } from "./model.ts";
import {
  Downsampler,
  downsamplePeaks,
  formatDuration,
  isVoiceMedia,
  placeholderPeaks,
  splitVoiceNotes,
  voiceAttachment,
  voiceName,
  voiceTranscriptMessage,
  wavBody,
  wavHeader,
} from "./voice.ts";

// The runtime writes an em dash in its note; spelled as an escape here.
const DASH = "\u2014";
const NOTE = `[Audio attached: voice-20261017-091500.m4a ${DASH} saved to /home/olano/agents/nova/workspace/uploads/voice-20261017-091500.m4a (audio/mp4, 38.2 KB)]`;
const GUIDANCE =
  "[Note: the attached file(s) above are saved in your workspace but are NOT shown to you inline. Decide from the user's message what to do with them: to view or analyze an image, call a vision tool such as image_describe with the saved path; to publish, send, or process a file, pass the saved path to the relevant tool; if the user only wants the file kept, no tool call is needed.]";

test("a voice note is named for when it was recorded, as WAV", () => {
  assert.equal(voiceName(new Date(2026, 9, 17, 9, 15, 0), ".wav"), "voice-20261017-091500.wav");
});

test("microphone audio comes down to 16 kHz, one sample per three at 48 kHz", () => {
  const down = new Downsampler(48000, 16000);
  // A second of 48 kHz arriving in uneven buffers.
  const out: number[] = [];
  let made = 0;
  for (const size of [4096, 4096, 4096, 4096, 4096, 4096, 4096, 4096, 4096, 4096, 4096, 2944]) {
    const buffer = new Float32Array(size).fill(0.5);
    made += size;
    out.push(...down.push(buffer));
  }
  out.push(...down.flush());
  assert.equal(made, 48000);
  assert.equal(out.length, 16000);
  assert.ok(out.every((v) => v === Math.round(0.5 * 0x7fff)));
});

test("44.1 kHz comes down to 16 kHz too, and samples never overflow 16 bits", () => {
  const down = new Downsampler(44100, 16000);
  const loud = new Float32Array(44100);
  for (let i = 0; i < loud.length; i++) loud[i] = i % 2 ? 1.7 : -1.7;
  const out = [...down.push(loud), ...down.flush()];
  assert.ok(Math.abs(out.length - 16000) <= 1, String(out.length));
  assert.ok(out.every((v) => v >= -32768 && v <= 32767));
});

test("the WAV header says 16-bit mono PCM at 16 kHz, little-endian", () => {
  const header = wavHeader(16000);
  const view = new DataView(header.buffer);
  const text = (from: number) => String.fromCharCode(...header.slice(from, from + 4));
  assert.equal(header.length, 44);
  assert.equal(text(0), "RIFF");
  assert.equal(view.getUint32(4, true), 36 + 32000);
  assert.equal(text(8), "WAVE");
  assert.equal(text(12), "fmt ");
  assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint32(28, true), 32000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(text(36), "data");
  assert.equal(view.getUint32(40, true), 32000);

  const body = wavBody([Int16Array.of(1, -2), Int16Array.of(0x1234)]);
  assert.deepEqual([...body], [1, 0, 0xfe, 0xff, 0x34, 0x12]);
});

test("a voice note travels as the runtime's audio attachment, without codec parameters", () => {
  const wire = voiceAttachment({
    name: "voice-1.webm",
    mime: "audio/webm;codecs=opus",
    data: "AAAA",
    bytes: 3,
    durationMs: 1200,
    peaks: [],
    url: "blob:x",
  });
  assert.deepEqual(wire, { kind: "audio", name: "voice-1.webm", mime: "audio/webm", data: "AAAA" });
});

test("durations read like a phone's", () => {
  assert.equal(formatDuration(0), "0:00");
  assert.equal(formatDuration(7400), "0:07");
  assert.equal(formatDuration(65_000), "1:05");
  assert.equal(formatDuration(750_000), "12:30");
  assert.equal(formatDuration(Number.NaN), "0:00");
});

test("a waveform is the loudest moment of each slice, scaled so quiet speech still has a shape", () => {
  const peaks = downsamplePeaks([0, 0.1, 0.05, 0.2, 0.1, 0], 3);
  assert.equal(peaks.length, 3);
  assert.equal(Math.max(...peaks), 1);
  assert.ok(peaks.every((p) => p >= 0.12 && p <= 1));
  assert.deepEqual(downsamplePeaks([], 4), [0.12, 0.12, 0.12, 0.12]);
  // Fewer samples than bars: each bar still has a value.
  assert.equal(downsamplePeaks([0.5, 1], 6).length, 6);
});

test("a recording that cannot be decoded gets a stable placeholder waveform", () => {
  const a = placeholderPeaks("/api/files-raw/uploads/voice-1.webm");
  assert.deepEqual(a, placeholderPeaks("/api/files-raw/uploads/voice-1.webm"));
  assert.notDeepEqual(a, placeholderPeaks("/api/files-raw/uploads/voice-2.webm"));
  assert.ok(a.every((p) => p > 0 && p <= 1));
});

test("audio, and a voice recording saved as WebM, are voice notes; other video is not", () => {
  assert.equal(isVoiceMedia({ kind: "audio", name: "reply.mp3" }), true);
  assert.equal(isVoiceMedia({ kind: "video", name: "voice-20261017-091500.webm" }), true);
  assert.equal(isVoiceMedia({ kind: "video", name: "site-walkthrough.webm" }), false);
  assert.equal(isVoiceMedia({ kind: "image", name: "voice-1.png" }), false);
});

test("the transcript comes out of the runtime's notes; a failed transcription is flagged", () => {
  const heard = splitVoiceNotes(`${NOTE}\n\n[Voice message transcript] Please check the label\n\n${GUIDANCE}`);
  assert.deepEqual(heard.transcripts, ["Please check the label"]);
  assert.equal(heard.untranscribed, false);
  assert.ok(!heard.text.includes("Voice message transcript"));

  const off = splitVoiceNotes(
    `${NOTE}\n\n[A voice message was received but speech-to-text is disabled on this server. Tell the sender: "couldn't transcribe, please resend as text". (Operator: enable it in the dashboard Config → Speech section or config.yaml → speech.stt.)]`,
  );
  assert.deepEqual(off.transcripts, []);
  assert.equal(off.untranscribed, true);
  assert.equal(splitVoiceNotes("plain words").text, "plain words");
});

test("a reloaded voice message is the recording and its transcript, not the notes", () => {
  const [message] = foldTranscript([
    {
      role: "user",
      content: `${NOTE}\n\n[Voice message transcript] Please check the label\n\n${GUIDANCE}`,
      media: [
        {
          kind: "audio",
          name: "voice-20261017-091500.m4a",
          mime: "audio/mp4",
          url: "/api/files-raw/uploads/voice-20261017-091500.m4a?agent=nova",
          path: "uploads/voice-20261017-091500.m4a",
        },
      ],
    },
  ]);
  assert.equal(message.body, "");
  assert.deepEqual(message.transcripts, ["Please check the label"]);
  assert.deepEqual(
    voicesOf(message).map((v) => v.url),
    ["/api/files-raw/uploads/voice-20261017-091500.m4a?agent=nova"],
  );
  assert.deepEqual(filesOf(message), []);
});

test("words the phone heard go as the runtime's own transcript line, read back the same way", () => {
  const message = voiceTranscriptMessage("  Order two more\npallets  ");
  assert.equal(message, "[Voice message transcript] Order two more pallets");
  const heard = splitVoiceNotes(message);
  assert.deepEqual(heard.transcripts, ["Order two more pallets"]);
  assert.equal(heard.text, "");
  const [reloaded] = foldTranscript([{ role: "user", content: message }]);
  assert.equal(reloaded.body, "");
  assert.deepEqual(reloaded.transcripts, ["Order two more pallets"]);
});
