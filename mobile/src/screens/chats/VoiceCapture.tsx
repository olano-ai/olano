/**
 * Recording a voice note from the composer, the way messengers do it:
 *
 * - **Hold** the microphone and talk; let go to send. Slide left to throw the
 *   recording away, or up to lock it so it keeps going without a finger on it.
 * - **Tap** the microphone to record hands-free from the start: a bin to
 *   throw it away and a send button. Where the phone can dictate
 *   (Dictation.tsx), a tap dictates instead, and a voice note starts only once
 *   the press has lasted longer than a tap.
 *
 * The microphone button stays the same element through the whole gesture (it
 * holds the pointer), and becomes the send button once the recording is
 * locked, so this file gives the composer both the button's behaviour and the
 * strip that replaces the message box while recording.
 */

import { IonIcon, IonSpinner } from "@ionic/react";
import { arrowUp, chevronBack, chevronUp, lockClosed, mic, trashOutline } from "ionicons/icons";
import { useCallback, useEffect, useRef, useState } from "react";

import { formatDuration, MAX_VOICE_MS, type PreparedVoice } from "../../chat/voice.ts";
import { haptic } from "../../native/haptics.ts";
import { RecorderError, VoiceRecorder } from "../../native/recorder.ts";
import { stopVoice } from "../../ui/audioPlayer.ts";
import { stopReading } from "../../ui/readAloud.ts";

/** Held for less than this, the microphone was tapped: record hands-free. */
const TAP_MS = 300;
/** How far left a held recording slides before it is thrown away. */
const CANCEL_PX = 100;
/** How far up a held recording slides before it locks. */
const LOCK_PX = 70;

export type CaptureState = "idle" | "holding" | "locked" | "finishing";

export interface VoiceCapture {
  state: CaptureState;
  /** A press that may still turn out to be a tap (dictation is offered). */
  pressing: boolean;
  /** A tap dictates rather than recording. */
  tapDictates: boolean;
  /** The microphone is open and hearing: talking counts from here. */
  started: boolean;
  /** How far the finger has slid, for the hints to follow it. */
  drag: { dx: number; dy: number };
  elapsedMs: number;
  levels: number[];
  press(x: number, y: number): void;
  move(x: number, y: number): void;
  release(): void;
  /** The locked recording's send button (ignores the click that ends the tap which started it). */
  tapSend(): void;
  /** Recording from the keyboard: hands-free from the start. */
  startHandsFree(): void;
  finish(): void;
  cancel(): void;
}

export function useVoiceCapture(
  onSend: (voice: PreparedVoice) => void,
  onProblem: (message: string) => void,
  /** Given: a tap calls this (live dictation) and only a longer press records. */
  onTap?: () => void,
): VoiceCapture {
  const [state, setStateRaw] = useState<CaptureState>("idle");
  const stateRef = useRef<CaptureState>("idle");
  const setState = (next: CaptureState) => {
    stateRef.current = next;
    setStateRaw(next);
  };
  const recorder = useRef<VoiceRecorder | null>(null);
  const pressRef = useRef({ x: 0, y: 0, t: 0, down: false });
  const [drag, setDrag] = useState({ dx: 0, dy: 0 });
  const [, setTick] = useState(0);
  // When a tap turned into a hands-free recording: the same tap's click must not send it.
  const lockedAt = useRef(0);
  // The moment the microphone started hearing, marked once with a haptic.
  const heard = useRef(false);
  // While a press may still be a tap: the timer that turns it into a recording.
  const pending = useRef<number | undefined>(undefined);
  const [pressing, setPressing] = useState(false);
  const clearPending = () => {
    window.clearTimeout(pending.current);
    pending.current = undefined;
    setPressing(false);
  };

  const cancel = useCallback(() => {
    clearPending();
    if (stateRef.current === "idle") return;
    recorder.current?.cancel();
    recorder.current = null;
    pressRef.current.down = false;
    setDrag({ dx: 0, dy: 0 });
    setState("idle");
    void haptic("light");
  }, []);

  const finish = useCallback(async () => {
    const current = recorder.current;
    if (!current || stateRef.current === "finishing") return;
    pressRef.current.down = false;
    setState("finishing");
    const voice = await current.stop();
    if (recorder.current === current) recorder.current = null;
    setDrag({ dx: 0, dy: 0 });
    setState("idle");
    if (voice) {
      void haptic("light");
      onSend(voice);
    } else {
      onProblem(
        onTap
          ? "Too short to send. Hold the microphone while you talk to record a voice note, or tap it to dictate."
          : "Too short to send. Hold the microphone while you talk, or tap it to record hands-free.",
      );
    }
  }, [onProblem, onSend, onTap]);

  const begin = useCallback(
    (x: number, y: number, at: number) => {
      if (stateRef.current !== "idle") return;
      stopVoice();
      stopReading();
      void haptic("medium");
      pressRef.current = { x, y, t: at, down: true };
      const current = new VoiceRecorder();
      recorder.current = current;
      heard.current = false;
      setDrag({ dx: 0, dy: 0 });
      setState("holding");
      current
        .start()
        .then(() => {
          // Hearing now: a tick under the finger says so, so nobody talks into
          // a microphone that is still opening.
          if (recorder.current === current && current.started && !heard.current) {
            heard.current = true;
            void haptic("light");
            setTick((t) => t + 1);
          }
        })
        .catch((error) => {
          if (recorder.current !== current) return;
          recorder.current = null;
          pressRef.current.down = false;
          setState("idle");
          onProblem(error instanceof RecorderError ? error.message : "The microphone could not be started.");
        });
    },
    [onProblem],
  );

  const press = useCallback(
    (x: number, y: number) => {
      if (stateRef.current !== "idle" || pending.current !== undefined) return;
      const at = performance.now();
      if (!onTap) {
        begin(x, y, at);
        return;
      }
      // Maybe a tap: the recording waits until the press outlasts one.
      pressRef.current = { x, y, t: at, down: true };
      setPressing(true);
      pending.current = window.setTimeout(() => {
        pending.current = undefined;
        setPressing(false);
        if (pressRef.current.down) begin(x, y, at);
      }, TAP_MS);
    },
    [begin, onTap],
  );

  const move = useCallback(
    (x: number, y: number) => {
      if (stateRef.current !== "holding" || !pressRef.current.down) return;
      const dx = Math.min(0, x - pressRef.current.x);
      const dy = Math.min(0, y - pressRef.current.y);
      if (dx < -CANCEL_PX) {
        cancel();
        return;
      }
      if (dy < -LOCK_PX) {
        pressRef.current.down = false;
        setDrag({ dx: 0, dy: 0 });
        setState("locked");
        void haptic("light");
        return;
      }
      setDrag({ dx, dy });
    },
    [cancel],
  );

  const release = useCallback(() => {
    if (!pressRef.current.down) return;
    pressRef.current.down = false;
    if (pending.current !== undefined) {
      clearPending();
      onTap?.();
      return;
    }
    if (stateRef.current !== "holding") return;
    const held = performance.now() - pressRef.current.t;
    // Where a tap dictates, a released press is always a voice note to send.
    if (!onTap && (held < TAP_MS || !recorder.current?.recording)) {
      // A tap (or a press while the phone was still asking for the
      // microphone): keep recording, hands-free.
      setDrag({ dx: 0, dy: 0 });
      lockedAt.current = performance.now();
      setState("locked");
      return;
    }
    void finish();
  }, [finish, onTap]);

  // Redraw the timer and the live waveform; stop at the length limit.
  useEffect(() => {
    if (state !== "holding" && state !== "locked") return;
    const id = window.setInterval(() => {
      setTick((t) => t + 1);
      if ((recorder.current?.elapsedMs() ?? 0) > MAX_VOICE_MS) void finish();
    }, 100);
    return () => window.clearInterval(id);
  }, [state, finish]);

  // Leaving the screen, or the app, throws an unsent recording away: the
  // phone takes the microphone back in the background anyway.
  useEffect(
    () => () => {
      window.clearTimeout(pending.current);
      recorder.current?.cancel();
    },
    [],
  );
  useEffect(() => {
    const onHidden = () => {
      if (document.hidden && stateRef.current !== "idle" && stateRef.current !== "finishing") cancel();
    };
    document.addEventListener("visibilitychange", onHidden);
    return () => document.removeEventListener("visibilitychange", onHidden);
  }, [cancel]);

  return {
    state,
    pressing,
    tapDictates: !!onTap,
    started: recorder.current?.started ?? false,
    drag,
    elapsedMs: recorder.current?.elapsedMs() ?? 0,
    levels: recorder.current?.recent(30) ?? [],
    press,
    move,
    release,
    tapSend: () => {
      if (stateRef.current === "locked" && performance.now() - lockedAt.current > 400) void finish();
    },
    startHandsFree: () => {
      if (stateRef.current !== "idle") return;
      if (onTap) {
        onTap();
        return;
      }
      press(0, 0);
      pressRef.current.down = false;
      lockedAt.current = performance.now();
      setState("locked");
    },
    finish: () => void finish(),
    cancel,
  };
}

/** The microphone, under the finger while holding, and the send button once locked. */
export function MicButton({ capture }: { capture: VoiceCapture }) {
  const { state } = capture;
  const recording = state === "holding" || state === "locked" || state === "finishing";
  const label =
    state === "locked"
      ? "Send voice note"
      : state === "holding"
        ? "Recording, release to send"
        : capture.tapDictates
          ? "Dictate, or hold to record a voice note"
          : "Record voice note";
  return (
    <button
      type="button"
      className={capture.pressing ? `olano-mic ${state} pressing` : `olano-mic ${state}`}
      aria-label={label}
      onPointerDown={(e) => {
        if (state === "locked" || state === "finishing") return;
        e.preventDefault();
        try {
          e.currentTarget.setPointerCapture(e.pointerId);
        } catch {
          // A synthetic pointer: the gesture still works without capture.
        }
        capture.press(e.clientX, e.clientY);
      }}
      onPointerMove={(e) => capture.move(e.clientX, e.clientY)}
      onPointerUp={() => capture.release()}
      onPointerCancel={() => {
        if (state === "holding" || capture.pressing) capture.cancel();
      }}
      onClick={(e) => {
        if (state === "locked") capture.tapSend();
        // Enter or Space on a keyboard: no pointer came first.
        else if (state === "idle" && e.detail === 0) capture.startHandsFree();
      }}
      onContextMenu={(e) => e.preventDefault()}
    >
      {state === "finishing" ? (
        <IonSpinner name="crescent" />
      ) : (
        <IonIcon icon={state === "locked" ? arrowUp : mic} />
      )}
      {recording && state === "holding" && (
        <span className="olano-mic-lock" style={{ transform: `translateY(${Math.max(-40, capture.drag.dy * 0.6)}px)` }} aria-hidden="true">
          <IonIcon icon={lockClosed} />
          <IonIcon icon={chevronUp} />
        </span>
      )}
    </button>
  );
}

/** What replaces the message box while recording. */
export function RecordingStrip({ capture }: { capture: VoiceCapture }) {
  const { state, started, drag, elapsedMs, levels } = capture;
  const locked = state === "locked" || state === "finishing";
  // Grey until the microphone hears, red once talking counts.
  const classes = ["olano-recording", locked ? "locked" : "", started || state === "finishing" ? "" : "starting"].filter(Boolean).join(" ");
  return (
    <div className={classes} role="status" aria-live="polite">
      {locked && (
        <button type="button" className="olano-recording-bin" aria-label="Delete recording" onClick={capture.cancel}>
          <IonIcon icon={trashOutline} />
        </button>
      )}
      <span className="olano-recording-dot" aria-hidden="true" />
      <span className="olano-recording-time" aria-label="Recording time">
        {formatDuration(elapsedMs)}
      </span>
      {locked ? (
        <span className="olano-recording-bars" aria-hidden="true">
          {levels.map((level, i) => (
            <span key={i} style={{ height: `${Math.round(Math.max(0.1, Math.min(1, level)) * 100)}%` }} />
          ))}
        </span>
      ) : (
        <span
          className="olano-recording-hint"
          style={{ transform: `translateX(${drag.dx}px)`, opacity: Math.max(0.2, 1 + drag.dx / 120) }}
        >
          <IonIcon icon={chevronBack} />
          Slide to cancel
        </span>
      )}
    </div>
  );
}
