/**
 * Replies read aloud: in the deployment's voice when it has text-to-speech
 * set up (Config > Speech), in the phone's own voice when it does not, or
 * when the deployment's voice fails. One reading at a time, like the voice
 * notes: reading another reply, playing a voice note or starting a recording
 * stops it.
 *
 * The deployment's audio plays through the voice notes' player, so it obeys
 * the same rules (one at a time, heard with the ringer switch off). A reply
 * read once is kept for the session, so hearing it again costs nothing.
 */

import { speakableText } from "../chat/speakable.ts";
import { ApiError } from "../net/api.ts";
import { speechEngine } from "../native/speech.ts";
import { session } from "../state/session.ts";
import { createStore } from "../state/store.ts";
import { player, playVoice, stopVoice } from "./audioPlayer.ts";

export interface ReaderState {
  /** The message being read, or null. */
  key: string | null;
  phase: "idle" | "loading" | "speaking";
}

export type ReadOutcome = "read" | "nothing" | "unavailable";

export const reader = createStore<ReaderState>({ key: null, phase: "idle" });

/** How long a deployment without a voice is not asked again. */
const NO_VOICE_MS = 5 * 60 * 1000;
const noVoiceUntil = new Map<string, number>();
/** Audio already fetched, by message (the last few). */
const fetched = new Map<string, string>();
const KEEP = 12;

let generation = 0;
/** The deployment's audio is playing (rather than the phone's voice). */
let onPlayer: { key: string; started: boolean } | null = null;

const playerKey = (key: string) => `read:${key}`;

function idle(): void {
  onPlayer = null;
  reader.set({ key: null, phase: "idle" });
}

player.subscribe(() => {
  const state = player.get();
  const reading = reader.get();
  if (reading.phase !== "speaking") return;
  if (onPlayer) {
    const ours = state.key === playerKey(onPlayer.key);
    if (ours && state.playing) onPlayer.started = true;
    else if (!ours || (onPlayer.started && !state.playing)) {
      generation += 1;
      idle();
    }
  } else if (state.playing) {
    // A voice note started while the phone was reading.
    stopReading();
  }
});

function base64ToBlob(base64: string, type: string): Blob {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type });
}

/** The reply in the deployment's voice, or null when it has none (or it failed). */
async function deploymentAudio(agent: string, key: string, text: string): Promise<string | null> {
  const cached = fetched.get(key);
  if (cached) return cached;
  const api = session.get().api;
  if (!api || (noVoiceUntil.get(api.origin) ?? 0) > Date.now()) return null;
  try {
    const audio = await api.speak(agent, text);
    const url = URL.createObjectURL(base64ToBlob(audio.base64, audio.type));
    fetched.set(key, url);
    while (fetched.size > KEEP) {
      const [oldest, oldUrl] = fetched.entries().next().value as [string, string];
      fetched.delete(oldest);
      URL.revokeObjectURL(oldUrl);
    }
    return url;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 409 || error.status === 404)) {
      noVoiceUntil.set(api.origin, Date.now() + NO_VOICE_MS);
    }
    return null;
  }
}

/**
 * Reads a reply aloud. Resolves once it has started (the deployment's voice)
 * or finished (the phone's), with "unavailable" when there was no voice to
 * read with at all.
 */
export async function readAloud(agent: string, key: string, markdown: string): Promise<ReadOutcome> {
  const text = speakableText(markdown);
  if (!text) return "nothing";
  stopReading();
  stopVoice();
  const mine = ++generation;
  reader.set({ key, phase: "loading" });
  const url = await deploymentAudio(agent, key, text);
  if (mine !== generation) return "read";
  if (url) {
    onPlayer = { key, started: false };
    reader.set({ key, phase: "speaking" });
    playVoice(playerKey(key), url, 0);
    // Refused to play (no sound allowed yet): don't sit on "speaking".
    window.setTimeout(() => {
      if (mine === generation && onPlayer && !onPlayer.started) {
        generation += 1;
        idle();
      }
    }, 4000);
    return "read";
  }
  reader.set({ key, phase: "speaking" });
  try {
    await speechEngine().speak({ text });
    return "read";
  } catch {
    return "unavailable";
  } finally {
    if (mine === generation) idle();
  }
}

/** Stops whatever is being read. */
export function stopReading(): void {
  const reading = reader.get();
  if (reading.phase === "idle") return;
  generation += 1;
  const wasPlayer = onPlayer;
  idle();
  if (wasPlayer) stopVoice();
  else void speechEngine().stopSpeaking().catch(() => undefined);
}

/** Forgets fetched audio (sign out, switch deployment). */
export function resetReadAloud(): void {
  stopReading();
  for (const url of fetched.values()) URL.revokeObjectURL(url);
  fetched.clear();
  noVoiceUntil.clear();
}
