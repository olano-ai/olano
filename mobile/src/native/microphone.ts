/**
 * The microphone, opened once and kept ready.
 *
 * On an iPhone, opening the microphone is the slow part of recording a voice
 * note: `getUserMedia` starts the web view's capture process and switches the
 * phone's audio session, which takes several hundred milliseconds the first
 * time and a good part of that every time after. A recorder that opened it
 * on each press lost the first words of every note to that wait. So the
 * stream is opened once and shared: a recording takes it, and when the
 * recording ends the microphone stays open for `KEEP_WARM_MS` so the next
 * note starts at once, then closes (the phone's microphone indicator goes
 * with it). The app going to the background closes it immediately.
 *
 * One `AudioContext` is shared the same way: creating one per recording
 * costs another start of the audio hardware. It is created inside the first
 * tap (iOS only lets a page start audio from a gesture) and resumed on every
 * use.
 */

import { KeepWarm } from "../chat/keepWarm.ts";

/** How long the microphone stays open after a recording, for the next one. */
export const KEEP_WARM_MS = 20_000;

type AudioContextCtor = typeof AudioContext;

function audioContextCtor(): AudioContextCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { AudioContext?: AudioContextCtor; webkitAudioContext?: AudioContextCtor };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

export function canOpenMicrophone(): boolean {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && !!audioContextCtor();
}

const CONSTRAINTS: MediaStreamConstraints = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
};

let stream: MediaStream | null = null;
let opening: Promise<MediaStream> | null = null;
let context: AudioContext | null = null;

function live(current: MediaStream | null): current is MediaStream {
  return !!current && current.getAudioTracks().some((track) => track.readyState === "live");
}

function closeStream(): void {
  for (const track of stream?.getTracks() ?? []) track.stop();
  stream = null;
}

const warm = new KeepWarm(closeStream, KEEP_WARM_MS);

/** The shared audio context, made now (call it inside the tap) and running. */
export function audioContext(): AudioContext {
  const Ctor = audioContextCtor();
  if (!Ctor) throw new Error("No audio context");
  if (!context || context.state === "closed") context = new Ctor();
  if (context.state !== "running") void context.resume().catch(() => undefined);
  return context;
}

/**
 * The open microphone: the one already open, or a newly opened one. Throws
 * what `getUserMedia` throws. The caller keeps it until `releaseMicrophone`.
 */
export async function openMicrophone(): Promise<MediaStream> {
  warm.hold();
  if (live(stream)) return stream;
  closeStream();
  if (!opening) {
    opening = navigator.mediaDevices.getUserMedia(CONSTRAINTS).finally(() => {
      opening = null;
    });
  }
  const opened = await opening;
  // A second caller raced the first: both get the same stream.
  if (stream !== opened) {
    closeStream();
    stream = opened;
  }
  // The phone took it away (a call, another app): the next open starts over.
  for (const track of opened.getAudioTracks()) {
    track.onended = () => {
      if (stream === opened) stream = null;
    };
  }
  warm.hold();
  return opened;
}

/** A recording ended: the microphone stays ready a while, then closes. */
export function releaseMicrophone(): void {
  warm.releaseSoon();
}

/** Closes the microphone now (the app went away, or it is not wanted any more). */
export function closeMicrophone(): void {
  warm.releaseNow();
}

/** Opens the microphone ahead of a recording, so the recording starts at once. Never throws. */
export function warmMicrophone(): void {
  if (!canOpenMicrophone()) return;
  openMicrophone()
    .then(() => releaseMicrophone())
    .catch(() => undefined);
}

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) closeMicrophone();
  });
}
