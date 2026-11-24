// Voice input for the composer — dictate a message instead of typing it.
//
// **The mic is offered only to browsers that can dictate live.** That means the
// Web Speech API (Chrome/Edge desktop + Android, Safari macOS/iOS 14.5+), where
// recognition runs at the browser vendor, streams interim results as you talk,
// and costs nothing — no olano credits, no provider key. A browser without it
// (Firefox) gets no microphone at all rather than a button that behaves like a
// different feature: no words while you speak, a wait on release, and a bill
// per use. Until there is streaming server-side STT to back it, a silent
// upload is not the same product, and shipping it under the same icon reads as
// the mic being broken here.
//
// Server transcription (MediaRecorder → POST /api/transcribe) survives as a
// **rescue**, never as an entry point: Chrome's engine is a network service, so
// when it goes unreachable mid-utterance this finishes the recording the user
// already started instead of dropping it. Requires stt_enabled; without it the
// utterance simply reports the error.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../../core/api";

export type VoiceSupport = "live" | "none";
export type VoiceState = "idle" | "starting" | "recording" | "transcribing";
export type VoiceMode = "hold" | "toggle";
export type VoiceErrorCode = "denied" | "no-speech" | "network" | "stt-off" | "failed";

// Recording longer than this is almost certainly a stuck toggle rather than a
// message. Also keeps an upload-tier blob under the endpoint's 25 MB cap.
const MAX_RECORDING_MS = 5 * 60 * 1000;
// A tap, not a hold: flip to toggle mode instead of stopping instantly. This is
// how the button works on phones, which have no keyboard shortcut.
export const TAP_THRESHOLD_MS = 300;

type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((e: any) => void) | null;
  onerror: ((e: any) => void) | null;
  onend: (() => void) | null;
};

function recognitionCtor(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === "undefined") return null;
  const w = window as any;
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

function canRecord(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.MediaRecorder !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia
  );
}

// Web Speech wants a full BCP-47 tag; the UI locale is the bare language.
function speechLocale(lang: string): string {
  if (lang === "zh") return "zh-CN";
  if (lang === "es") return "es-ES";
  return "en-US";
}

// Preference order matters: webm/opus is what the transcribe endpoint's
// extension sniffing handles best, and mp4 is Safari's only option.
const RECORDER_MIMES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
];

function recorderMime(): string {
  for (const m of RECORDER_MIMES) {
    try {
      if (window.MediaRecorder.isTypeSupported(m)) return m;
    } catch {
      /* isTypeSupported throws on some old builds */
    }
  }
  return "";
}

function extForMime(mime: string): string {
  if (mime.includes("mp4")) return "m4a";
  if (mime.includes("ogg")) return "ogg";
  return "webm";
}

export interface UseVoiceInputOpts {
  /** Server-side STT reachable (capability flag). `undefined` while loading. */
  sttEnabled: boolean | undefined;
  /** UI locale ("en" | "zh" | "es"). */
  lang: string;
  /** Live partial text — replaces the previous interim, live tier only. */
  onInterim: (text: string) => void;
  /** Settled text to append to the draft. */
  onCommit: (text: string) => void;
  onError: (code: VoiceErrorCode) => void;
}

export interface VoiceInput {
  support: VoiceSupport;
  state: VoiceState;
  mode: VoiceMode | null;
  /** True once audio is actually being captured. */
  recording: boolean;
  start: (mode: VoiceMode) => void;
  /** Finish and keep what was said. */
  stop: () => void;
  /** Finish and throw it away (pointer cancelled, thread switched, unmount). */
  cancel: () => void;
  toggle: () => void;
  setMode: (mode: VoiceMode) => void;
}

export function useVoiceInput(opts: UseVoiceInputOpts): VoiceInput {
  const { sttEnabled, lang, onInterim, onCommit, onError } = opts;

  const [state, setState] = useState<VoiceState>("idle");
  const [mode, setModeState] = useState<VoiceMode | null>(null);

  // Callbacks live in a ref so the long-lived recognition/recorder handlers
  // always see the current closure without being torn down mid-utterance.
  const cb = useRef({ onInterim, onCommit, onError });
  useEffect(() => {
    cb.current = { onInterim, onCommit, onError };
  }, [onInterim, onCommit, onError]);

  const recRef = useRef<SpeechRecognitionLike | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const interimRef = useRef("");
  const modeRef = useRef<VoiceMode | null>(null);
  // Set when the caller cancels, so the recorder's onstop discards instead of
  // uploading, and a live-tier restart doesn't fire.
  const discardRef = useRef(false);
  const stoppingRef = useRef(false);
  const restartsRef = useRef(0);
  const capTimerRef = useRef<number | null>(null);
  const langRef = useRef(lang);
  useEffect(() => {
    langRef.current = lang;
  }, [lang]);

  const hasLive = useMemo(() => recognitionCtor() !== null, []);
  const hasRecorder = useMemo(() => canRecord(), []);
  // Live dictation is the whole offer, so it alone decides whether there is a
  // mic. Server transcription can only rescue an utterance already in flight
  // (see the header), which is why it is not part of this answer.
  const support: VoiceSupport = hasLive ? "live" : "none";
  const canRescue = hasRecorder && !!sttEnabled;

  const releaseStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, []);

  const clearCap = useCallback(() => {
    if (capTimerRef.current !== null) {
      window.clearTimeout(capTimerRef.current);
      capTimerRef.current = null;
    }
  }, []);

  const finish = useCallback(() => {
    clearCap();
    modeRef.current = null;
    setModeState(null);
    setState("idle");
  }, [clearCap]);

  // ── Upload tier ──────────────────────────────────────────────────────────

  const startUpload = useCallback(
    async (nextMode: VoiceMode) => {
      if (!hasRecorder || !sttEnabled) {
        cb.current.onError("failed");
        finish();
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        // The user let go (or cancelled) while the permission prompt was up.
        if (discardRef.current) {
          stream.getTracks().forEach((t) => t.stop());
          finish();
          return;
        }
        streamRef.current = stream;
        const mime = recorderMime();
        const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
        recorderRef.current = rec;
        chunksRef.current = [];
        rec.ondataavailable = (e) => {
          if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
        };
        rec.onstop = () => {
          releaseStream();
          const parts = chunksRef.current;
          chunksRef.current = [];
          recorderRef.current = null;
          if (discardRef.current || parts.length === 0) {
            finish();
            return;
          }
          const type = rec.mimeType || mime || "audio/webm";
          const blob = new Blob(parts, { type });
          void uploadBlob(blob, type);
        };
        rec.start();
        modeRef.current = nextMode;
        setModeState(nextMode);
        setState("recording");
        capTimerRef.current = window.setTimeout(() => stopRef.current(), MAX_RECORDING_MS);
      } catch (err) {
        releaseStream();
        const name = (err as { name?: string })?.name || "";
        cb.current.onError(
          name === "NotAllowedError" || name === "SecurityError" ? "denied" : "failed",
        );
        finish();
      }
    },
    [finish, hasRecorder, releaseStream, sttEnabled],
  );

  const uploadBlob = useCallback(
    async (blob: Blob, type: string) => {
      setState("transcribing");
      try {
        const form = new FormData();
        form.append("file", blob, `recording.${extForMime(type)}`);
        const lc = langRef.current;
        if (lc) form.append("language", lc);
        const res = await api.postForm<{ text?: string }>("/api/transcribe", form);
        const text = (res?.text || "").trim();
        if (text) cb.current.onCommit(text);
        else cb.current.onError("no-speech");
      } catch (err) {
        const detail = err instanceof ApiError ? err.detail : "";
        if (detail === "stt_disabled" || detail === "stt_not_configured") {
          cb.current.onError("stt-off");
        } else if (detail === "missing_file") {
          cb.current.onError("no-speech");
        } else {
          cb.current.onError("failed");
        }
      } finally {
        finish();
      }
    },
    [finish],
  );

  // ── Live tier ────────────────────────────────────────────────────────────

  // The engine died on an utterance the user already started. Finish it
  // server-side if we can; otherwise say so rather than dropping it silently.
  const rescue = useCallback(
    (nextMode: VoiceMode, code: VoiceErrorCode) => {
      if (canRescue && !discardRef.current) {
        void startUpload(nextMode);
        return;
      }
      cb.current.onError(code);
      finish();
    },
    [canRescue, finish, startUpload],
  );

  const startLive = useCallback(
    (nextMode: VoiceMode) => {
      const Ctor = recognitionCtor();
      if (!Ctor) {
        rescue(nextMode, "failed");
        return;
      }
      let rec: SpeechRecognitionLike;
      try {
        rec = new Ctor();
      } catch {
        rescue(nextMode, "failed");
        return;
      }
      rec.lang = speechLocale(langRef.current);
      rec.continuous = true;
      rec.interimResults = true;

      rec.onresult = (e: any) => {
        let interim = "";
        let settled = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i];
          const chunk = r[0]?.transcript || "";
          if (r.isFinal) settled += chunk;
          else interim += chunk;
        }
        // Finals harden as you keep talking, the way macOS dictation does —
        // don't wait for the whole utterance to end.
        if (settled.trim()) {
          interimRef.current = "";
          cb.current.onInterim("");
          cb.current.onCommit(settled.trim());
        }
        if (interim !== interimRef.current) {
          interimRef.current = interim;
          cb.current.onInterim(interim);
        }
      };

      rec.onerror = (e: any) => {
        const kind = String(e?.error || "");
        if (kind === "no-speech") return; // a pause, not a failure
        if (kind === "aborted") return; // our own stop()/cancel()
        // The browser's recognition service is unreachable or refused. If this
        // box can transcribe server-side, switch tiers rather than telling the
        // user voice input is broken.
        if (kind === "network" || kind === "service-not-allowed") {
          const m = modeRef.current || "toggle";
          try {
            rec.abort();
          } catch {
            /* already dead */
          }
          recRef.current = null;
          rescue(m, kind === "network" ? "network" : "denied");
          return;
        } else if (kind === "not-allowed") {
          cb.current.onError("denied");
        } else {
          cb.current.onError("failed");
        }
        recRef.current = null;
        interimRef.current = "";
        cb.current.onInterim("");
        finish();
      };

      rec.onend = () => {
        // Chrome ends a session after ~60s and iOS Safari ends it on silence.
        // In toggle mode the user never asked to stop, so pick it back up —
        // bounded, so a permanently failing engine can't spin.
        if (
          !stoppingRef.current &&
          !discardRef.current &&
          modeRef.current === "toggle" &&
          restartsRef.current < 50
        ) {
          restartsRef.current += 1;
          try {
            rec.start();
            return;
          } catch {
            /* fall through to settle */
          }
        }
        recRef.current = null;
        const tail = interimRef.current.trim();
        interimRef.current = "";
        cb.current.onInterim("");
        if (tail && !discardRef.current) cb.current.onCommit(tail);
        finish();
      };

      try {
        rec.start();
      } catch {
        // Safari throws if a previous session is still winding down.
        rescue(nextMode, "failed");
        return;
      }
      recRef.current = rec;
      restartsRef.current = 0;
      modeRef.current = nextMode;
      setModeState(nextMode);
      setState("recording");
      capTimerRef.current = window.setTimeout(() => stopRef.current(), MAX_RECORDING_MS);
    },
    [finish, rescue],
  );

  // ── Controls ─────────────────────────────────────────────────────────────

  const start = useCallback(
    (nextMode: VoiceMode) => {
      if (state !== "idle" || support === "none") return;
      discardRef.current = false;
      stoppingRef.current = false;
      interimRef.current = "";
      setState("starting");
      startLive(nextMode);
    },
    [startLive, state, support],
  );

  const stop = useCallback(() => {
    clearCap();
    stoppingRef.current = true;
    if (recRef.current) {
      try {
        recRef.current.stop(); // onend commits the trailing interim
      } catch {
        recRef.current = null;
        finish();
      }
      return;
    }
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      try {
        recorderRef.current.stop(); // onstop uploads
      } catch {
        releaseStream();
        finish();
      }
      return;
    }
    if (state === "starting") discardRef.current = true;
    else if (state !== "transcribing") finish();
  }, [clearCap, finish, releaseStream, state]);

  // stop() is referenced by timers created before it exists; go through a ref.
  const stopRef = useRef(stop);
  useEffect(() => {
    stopRef.current = stop;
  }, [stop]);

  const cancel = useCallback(() => {
    clearCap();
    discardRef.current = true;
    stoppingRef.current = true;
    interimRef.current = "";
    cb.current.onInterim("");
    if (recRef.current) {
      try {
        recRef.current.abort();
      } catch {
        /* already gone */
      }
      recRef.current = null;
    }
    if (recorderRef.current) {
      try {
        if (recorderRef.current.state !== "inactive") recorderRef.current.stop();
      } catch {
        /* already gone */
      }
      recorderRef.current = null;
    }
    chunksRef.current = [];
    releaseStream();
    finish();
  }, [clearCap, finish, releaseStream]);

  const toggle = useCallback(() => {
    if (state === "recording" || state === "starting") stop();
    else if (state === "idle") start("toggle");
  }, [start, state, stop]);

  const setMode = useCallback((next: VoiceMode) => {
    modeRef.current = next;
    setModeState(next);
  }, []);

  // iOS Safari kills recognition when the page goes to the background; settle
  // what we have rather than leaving the UI stuck in "Listening…".
  useEffect(() => {
    if (state !== "recording") return;
    function onHide() {
      if (document.visibilityState === "hidden") stopRef.current();
    }
    document.addEventListener("visibilitychange", onHide);
    return () => document.removeEventListener("visibilitychange", onHide);
  }, [state]);

  // Leaving the view must always release the mic.
  const cancelRef = useRef(cancel);
  useEffect(() => {
    cancelRef.current = cancel;
  }, [cancel]);
  useEffect(() => () => cancelRef.current(), []);

  return {
    support,
    state,
    mode,
    recording: state === "recording" || state === "starting",
    start,
    stop,
    cancel,
    toggle,
    setMode,
  };
}
