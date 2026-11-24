/**
 * The one audio player behind every voice note: starting one pauses whichever
 * was playing, as in any messenger. Voice notes read its state (which note,
 * where it is, how fast) from a store, updated every animation frame while
 * playing so the waveform's progress moves smoothly rather than in the
 * quarter-second steps `timeupdate` gives.
 *
 * Durations come from the voice note (measured while recording, or decoded
 * from the file): a WebM recording from MediaRecorder reports no duration
 * until it has played to the end.
 */

import { createStore } from "../state/store.ts";

export const RATES = [1, 1.5, 2];

export interface PlayerState {
  key: string | null;
  playing: boolean;
  /** Milliseconds into the note. */
  position: number;
  durationMs: number;
  rate: number;
}

export const player = createStore<PlayerState>({ key: null, playing: false, position: 0, durationMs: 0, rate: 1 });

let audio: HTMLAudioElement | null = null;
let frame = 0;

function element(): HTMLAudioElement {
  if (!audio) {
    audio = new Audio();
    audio.preload = "auto";
    audio.setAttribute("playsinline", "");
    audio.addEventListener("ended", () => {
      cancelAnimationFrame(frame);
      player.set((s) => ({ ...s, playing: false, position: 0 }));
    });
    audio.addEventListener("pause", () => player.set((s) => ({ ...s, playing: false })));
    audio.addEventListener("play", () => {
      player.set((s) => ({ ...s, playing: true }));
      tick();
    });
  }
  return audio;
}

function tick(): void {
  cancelAnimationFrame(frame);
  const el = audio;
  if (!el || el.paused) return;
  player.set((s) => ({ ...s, position: el.currentTime * 1000 }));
  frame = requestAnimationFrame(tick);
}

function load(key: string, src: string, durationMs: number): HTMLAudioElement {
  const el = element();
  const state = player.get();
  if (state.key !== key) {
    el.pause();
    el.src = src;
    player.set({ ...state, key, playing: false, position: 0, durationMs });
  } else if (durationMs && durationMs !== state.durationMs) {
    player.set({ ...state, durationMs });
  }
  el.playbackRate = player.get().rate;
  return el;
}

/** Plays a voice note from where it was left (or from the start), pausing any other. */
export function playVoice(key: string, src: string, durationMs: number): void {
  const el = load(key, src, durationMs);
  void el.play().catch(() => player.set((s) => ({ ...s, playing: false })));
}

export function pauseVoice(): void {
  audio?.pause();
}

/** Jumps to a point in a voice note (0 to 1), playing it if it was playing. */
export function seekVoice(key: string, src: string, durationMs: number, fraction: number): void {
  const el = load(key, src, durationMs);
  const ms = Math.max(0, Math.min(1, fraction)) * (durationMs || el.duration * 1000 || 0);
  try {
    el.currentTime = ms / 1000;
  } catch {
    // Not seekable before its data arrives; the position still shows.
  }
  player.set((s) => ({ ...s, position: ms }));
}

/** 1x, 1.5x, 2x, and round again. */
export function cycleRate(): void {
  const next = RATES[(RATES.indexOf(player.get().rate) + 1) % RATES.length];
  if (audio) audio.playbackRate = next;
  player.set((s) => ({ ...s, rate: next }));
}

/** Stops whatever plays (recording a new note, leaving the app). */
export function stopVoice(): void {
  audio?.pause();
}
