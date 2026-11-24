/**
 * Live dictation in the composer: tap the microphone and the words appear in
 * the box as they are said, worked out on the phone (native/dictation.ts);
 * what is sent is the text. While it listens the composer turns into
 *
 *   [ the words so far ...                         ]
 *   [ x ]  ·|||·····||·······   [ stop ]  [ send ]
 *
 * x throws the dictated words away, stop leaves them in the box to edit, send
 * sends them. Holding the microphone still records a voice note.
 */

import { IonButton, IonIcon } from "@ionic/react";
import { arrowUp, close } from "ionicons/icons";
import { useCallback, useEffect, useRef, useState } from "react";

import { MAX_DICTATION_MS, waveform, withDictation } from "../../chat/dictation.ts";
import { haptic } from "../../native/haptics.ts";
import { dictationAvailable, LiveDictation } from "../../native/dictation.ts";
import { useVoicePrefs } from "../../state/voicePrefs.ts";
import { stopVoice } from "../../ui/audioPlayer.ts";
import { stopReading } from "../../ui/readAloud.ts";

export type DictationState = "idle" | "starting" | "listening" | "stopping";

/** Readings kept for the waveform, one every TICK_MS. */
const HISTORY = 120;
const TICK_MS = 100;
/** Bars drawn across the row. */
const BARS = 36;

export interface Dictation {
  state: DictationState;
  /** Everything heard so far. */
  text: string;
  levels: number[];
  start(): void;
  /** Stops listening; resolves with the words heard, or null when nothing was being dictated. */
  finish(): Promise<string | null>;
  cancel(): void;
}

/** Whether a tap on the microphone dictates here: the phone can, and the person left it on. */
export function useDictationOffered(): boolean {
  const { dictation } = useVoicePrefs();
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    let live = true;
    if (dictation) void dictationAvailable().then((ok) => live && setAvailable(ok));
    return () => {
      live = false;
    };
  }, [dictation]);
  return dictation && available;
}

export function useDictation(onProblem: (message: string) => void, onStopped: (text: string) => void): Dictation {
  const [state, setStateRaw] = useState<DictationState>("idle");
  const stateRef = useRef<DictationState>("idle");
  const setState = (next: DictationState) => {
    stateRef.current = next;
    setStateRaw(next);
  };
  const session = useRef<LiveDictation | null>(null);
  const [text, setText] = useState("");
  const levels = useRef<number[]>([]);
  const latest = useRef(0);
  const startedAt = useRef(0);
  const [, setTick] = useState(0);

  const reset = () => {
    session.current = null;
    levels.current = [];
    latest.current = 0;
    setText("");
    setState("idle");
  };

  const finish = useCallback(async (): Promise<string | null> => {
    const current = session.current;
    if (!current || stateRef.current === "stopping") return null;
    setState("stopping");
    const words = await current.stop();
    if (session.current === current) reset();
    return words;
  }, []);

  const cancel = useCallback(() => {
    const current = session.current;
    if (!current) return;
    void current.cancel();
    reset();
    void haptic("light");
  }, []);

  const start = useCallback(() => {
    if (stateRef.current !== "idle") return;
    stopVoice();
    stopReading();
    void haptic("medium");
    const current = new LiveDictation({
      onText: (words) => {
        if (session.current === current) setText(words);
      },
      onLevel: (level) => {
        if (session.current === current) latest.current = Math.max(latest.current, level);
      },
      onEnd: (words, end) => {
        if (session.current !== current) return;
        reset();
        // What was heard stays: in the box, to send or carry on with.
        onStopped(words);
        onProblem(end.reason === "interrupted" ? "Dictation stopped: the phone needed the microphone." : end.message || "Dictation stopped.");
      },
    });
    session.current = current;
    levels.current = [];
    startedAt.current = performance.now();
    setText("");
    setState("starting");
    current
      .start()
      .then(() => {
        if (session.current !== current) {
          void current.cancel();
          return;
        }
        setState("listening");
        void haptic("light");
      })
      .catch((error: Error) => {
        if (session.current !== current) return;
        reset();
        onProblem(error.message);
      });
  }, [onProblem, onStopped]);

  // The waveform scrolls at a steady pace whatever the plugin's event rate:
  // one reading per tick, the loudest heard since the last, fading to quiet.
  useEffect(() => {
    if (state === "idle") return;
    const id = window.setInterval(() => {
      if (stateRef.current === "listening") {
        levels.current.push(latest.current);
        if (levels.current.length > HISTORY) levels.current.splice(0, levels.current.length - HISTORY);
        latest.current *= 0.35;
      }
      setTick((t) => t + 1);
      if (performance.now() - startedAt.current > MAX_DICTATION_MS) {
        void finish().then((words) => words !== null && onStopped(words));
      }
    }, TICK_MS);
    return () => window.clearInterval(id);
  }, [state, finish, onStopped]);

  // Leaving the app keeps what was said, in the box: the phone takes the
  // microphone back in the background anyway.
  useEffect(() => {
    const onHidden = () => {
      if (document.hidden && (stateRef.current === "starting" || stateRef.current === "listening")) {
        void finish().then((words) => words !== null && onStopped(words));
      }
    };
    document.addEventListener("visibilitychange", onHidden);
    return () => document.removeEventListener("visibilitychange", onHidden);
  }, [finish, onStopped]);
  useEffect(() => () => void session.current?.cancel(), []);

  return { state, text, levels: levels.current, start, finish, cancel };
}

/** The words so far, where the message box was. */
export function DictationField({ draft, dictation }: { draft: string; dictation: Dictation }) {
  const shown = withDictation(draft, dictation.text);
  return (
    <div className="olano-dictation-text" role="status" aria-live="polite" aria-label="Dictated text">
      {shown || <span className="olano-dictation-placeholder">{dictation.state === "starting" ? "Starting…" : "Listening…"}</span>}
    </div>
  );
}

/** Cancel, the waveform, stop and send, where the composer's buttons were. */
export function DictationControls({ dictation, onStop, onSend }: { dictation: Dictation; onStop: () => void; onSend: () => void }) {
  const stopping = dictation.state === "stopping";
  return (
    <div className="olano-dictation-row">
      <button type="button" className="olano-dictation-cancel" aria-label="Cancel dictation" onClick={dictation.cancel}>
        <IonIcon icon={close} />
      </button>
      <span className={dictation.state === "listening" ? "olano-dictation-wave" : "olano-dictation-wave waiting"} aria-hidden="true">
        {waveform(dictation.levels, BARS).map((height, i) =>
          height === null ? <span key={i} className="dot" /> : <span key={i} className="bar" style={{ height: `${Math.round(height * 100)}%` }} />,
        )}
      </span>
      <button type="button" className="olano-dictation-stop" aria-label="Stop dictating" disabled={stopping} onClick={onStop}>
        <span aria-hidden="true" />
      </button>
      <IonButton shape="round" className="olano-send" aria-label="Send" disabled={stopping} onClick={onSend}>
        <IonIcon slot="icon-only" icon={arrowUp} />
      </IonButton>
    </div>
  );
}
