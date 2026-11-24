/**
 * This phone's voice settings (Settings > Voice), kept on the device like the
 * theme: they are about this phone (its recognizer, its speaker), not about
 * the person's account.
 */

import { createStore, useStore } from "./store.ts";

/** Where a voice note becomes words: on the phone (fast, the audio stays here) or on the deployment. */
export type TranscribeOn = "phone" | "deployment";
/** When a reply is read aloud without asking: after a voice note (the default), after every message, or never. */
export type ReadAloud = "voice" | "always" | "never";

export interface VoicePrefs {
  transcribeOn: TranscribeOn;
  readAloud: ReadAloud;
  /**
   * Set when the person flips "Transcribe on this phone" themselves. Until
   * then the deployment's default applies (`appLayout.transcribeOn`): every
   * setting used to be saved together, so a stored `transcribeOn` alone does
   * not mean anyone chose it.
   */
  transcribeChosen: boolean;
  /**
   * Tapping the microphone dictates (the words appear in the box as they are
   * said, worked out on the phone) where the phone can; holding it records a
   * voice note either way. Off, a tap records a voice note as before.
   */
  dictation: boolean;
}

export const DEFAULT_VOICE_PREFS: VoicePrefs = { transcribeOn: "phone", readAloud: "voice", transcribeChosen: false, dictation: true };

const KEY = "olano.voice";

/** Settings as stored, with anything unknown or missing back at its default. */
export function parseVoicePrefs(raw: string | null): VoicePrefs {
  let value: Record<string, unknown> = {};
  try {
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    if (parsed && typeof parsed === "object") value = parsed as Record<string, unknown>;
  } catch {
    // Unreadable: the defaults.
  }
  const transcribeOn = value.transcribeOn === "deployment" ? "deployment" : "phone";
  const readAloud = value.readAloud === "always" || value.readAloud === "never" ? value.readAloud : "voice";
  return { transcribeOn, readAloud, transcribeChosen: value.transcribeChosen === true, dictation: value.dictation !== false };
}

function read(): VoicePrefs {
  try {
    return parseVoicePrefs(localStorage.getItem(KEY));
  } catch {
    return DEFAULT_VOICE_PREFS;
  }
}

export const voicePrefs = createStore<VoicePrefs>(read());

export function setVoicePrefs(change: Partial<VoicePrefs>): void {
  voicePrefs.set((prefs) => ({ ...prefs, ...change }));
  try {
    localStorage.setItem(KEY, JSON.stringify(voicePrefs.get()));
  } catch {
    // Not remembered past this session; it still applies now.
  }
}

export function useVoicePrefs(): VoicePrefs {
  return useStore(voicePrefs);
}

/** Whether a finished reply is read aloud without asking. */
export function readsAloud(prefs: VoicePrefs, askedByVoice: boolean): boolean {
  return prefs.readAloud === "always" || (prefs.readAloud === "voice" && askedByVoice);
}
