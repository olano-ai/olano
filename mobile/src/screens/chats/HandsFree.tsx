/**
 * Hands-free mode, for when your hands and eyes are busy (driving, cooking):
 * the phone listens, sends what you said when you stop talking, reads the
 * reply aloud and listens again. The panel takes the composer's place; one
 * big button stops a reading to talk, talks again after a pause, and End
 * returns the keyboard. chat/handsFree.ts decides when talking has stopped.
 *
 * Where the phone can dictate (native/dictation.ts), it listens with its own
 * recognizer: your words show in the panel as you say them and go as text,
 * worked out on the phone. Elsewhere, or if dictation cannot start, it
 * records a voice note, as before.
 *
 * The phone's own voice (or the deployment's, when it has one) reads the
 * replies, so a hands-free conversation costs nothing beyond the messages.
 */

import { IonButton, IonIcon } from "@ionic/react";
import { mic, pause, volumeHigh } from "ionicons/icons";
import { useCallback, useEffect, useRef, useState } from "react";

import type { Conversation } from "../../chat/conversation.ts";
import { phaseText, SilenceDetector, WordsPauseDetector, type HandsFreePhase } from "../../chat/handsFree.ts";
import { LiveDictation } from "../../native/dictation.ts";
import { haptic } from "../../native/haptics.ts";
import { RecorderError, VoiceRecorder } from "../../native/recorder.ts";
import { useStore } from "../../state/store.ts";
import { stopVoice } from "../../ui/audioPlayer.ts";
import { reader, stopReading } from "../../ui/readAloud.ts";

/** How often the microphone's loudness is read. */
const TICK_MS = 100;
/** After a turn ends, how long to wait for the reading to start before listening again. */
const READ_GRACE_MS = 1200;
/** The panel shows the end of what was heard, at most this much of it. */
const WORDS_SHOWN = 160;

export interface HandsFree {
  phase: HandsFreePhase;
  level: number;
  /** What the phone has heard so far, while it dictates. */
  words: string;
  start(): void;
  end(): void;
  /** The big button: stop a reading and talk, talk again after a pause, or send now. */
  tap(): void;
}

export function useHandsFree(
  conversation: Conversation,
  onProblem: (message: string) => void,
  /** The phone can dictate (Dictation.tsx's useDictationOffered): listen for words, not a recording. */
  dictate = false,
): HandsFree {
  const [phase, setPhaseState] = useState<HandsFreePhase>("off");
  const phaseRef = useRef<HandsFreePhase>("off");
  const [level, setLevel] = useState(0);
  const recorder = useRef<VoiceRecorder | null>(null);
  const session = useRef<LiveDictation | null>(null);
  const [words, setWordsState] = useState("");
  const wordsRef = useRef("");
  const setWords = (next: string) => {
    wordsRef.current = next;
    setWordsState(next);
  };
  // Read when listening starts, so a request that arrives before the phone
  // has answered "can you dictate" (Siri) still gets words once it has.
  const dictateRef = useRef(dictate);
  dictateRef.current = dictate;
  // Dictation failed to start in this hands-free session: voice notes instead.
  const recordOnly = useRef(false);
  const timer = useRef<number | null>(null);
  const state = useStore(conversation.store);
  const reading = useStore(reader);
  const readStarted = useRef(false);

  const setPhase = useCallback((next: HandsFreePhase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  const stopTimer = () => {
    if (timer.current !== null) window.clearInterval(timer.current);
    timer.current = null;
  };

  const dropRecorder = useCallback(() => {
    stopTimer();
    recorder.current?.cancel();
    recorder.current = null;
    const listening = session.current;
    session.current = null;
    void listening?.cancel();
    setWords("");
    setLevel(0);
  }, []);

  /** The words go as a text message; the panel shows them until they are sent. */
  const deliverWords = useCallback(
    async (text: string) => {
      const said = text.trim();
      if (!said) {
        setWords("");
        setPhase("paused");
        return;
      }
      setWords(said);
      setPhase("sending");
      void haptic("light");
      readStarted.current = false;
      const ok = await conversation.send(said);
      if (phaseRef.current !== "sending") return;
      setWords("");
      if (!ok) {
        onProblem("Your message could not be sent.");
        setPhase("paused");
        return;
      }
      setPhase("thinking");
    },
    [conversation, onProblem, setPhase],
  );

  const sendNow = useCallback(async () => {
    const listening = session.current;
    if (listening) {
      stopTimer();
      session.current = null;
      setLevel(0);
      setPhase("sending");
      const text = await listening.stop();
      if (phaseRef.current !== "sending") return;
      await deliverWords(text);
      return;
    }
    const current = recorder.current;
    if (!current) return;
    stopTimer();
    recorder.current = null;
    setLevel(0);
    setPhase("sending");
    const voice = await current.stop();
    if (phaseRef.current !== "sending") return;
    if (!voice) {
      setPhase("paused");
      return;
    }
    void haptic("light");
    readStarted.current = false;
    const ok = await conversation.send("", [], voice);
    if (phaseRef.current !== "sending") return;
    if (!ok) {
      onProblem("The voice note could not be sent.");
      setPhase("paused");
      return;
    }
    setPhase("thinking");
  }, [conversation, deliverWords, onProblem, setPhase]);

  const listen = useCallback(() => {
    if (phaseRef.current === "off") return;
    dropRecorder();
    stopVoice();
    if (dictateRef.current && !recordOnly.current) {
      listenForWords();
      return;
    }
    const current = new VoiceRecorder();
    recorder.current = current;
    const detector = new SilenceDetector();
    setPhase("listening");
    current
      .start()
      .then(() => {
        if (recorder.current !== current) return;
        timer.current = window.setInterval(() => {
          if (recorder.current !== current) return;
          const now = current.level();
          setLevel(now);
          const verdict = detector.feed(now, performance.now());
          if (verdict === "done") void sendNow();
          else if (verdict === "timeout") {
            dropRecorder();
            setPhase("paused");
          }
        }, TICK_MS);
      })
      .catch((error) => {
        if (recorder.current !== current) return;
        recorder.current = null;
        setPhase("off");
        conversation.readEveryReply = false;
        onProblem(error instanceof RecorderError ? error.message : "The microphone could not be started.");
      });

    /** Listening with the phone's recognizer: the end of speech is read off the words. */
    function listenForWords() {
      const detector = new WordsPauseDetector();
      let loudest = 0;
      const listening = new LiveDictation({
        onText: (text) => {
          if (session.current === listening) setWords(text);
        },
        onLevel: (value) => {
          if (session.current === listening) loudest = Math.max(loudest, value);
        },
        onEnd: (text, end) => {
          // It stopped by itself (a call, an error): what was heard still goes.
          if (session.current !== listening) return;
          session.current = null;
          stopTimer();
          setLevel(0);
          if (!text.trim()) onProblem(end.message || "Listening stopped.");
          void deliverWords(text);
        },
      });
      session.current = listening;
      setWords("");
      setPhase("listening");
      listening
        .start()
        .then(() => {
          if (session.current !== listening) return;
          timer.current = window.setInterval(() => {
            if (session.current !== listening) return;
            setLevel(loudest);
            loudest *= 0.35;
            const verdict = detector.feed(wordsRef.current, performance.now());
            if (verdict === "done") void sendNow();
            else if (verdict === "timeout") {
              dropRecorder();
              setPhase("paused");
            }
          }, TICK_MS);
        })
        .catch((error: Error & { code?: string }) => {
          if (session.current !== listening) return;
          session.current = null;
          // Voice notes for the rest of this hands-free session.
          recordOnly.current = true;
          onProblem(
            error.code === "denied"
              ? "Olano isn't allowed to use speech recognition, so Hands-Free sends voice notes instead."
              : "Your words can't be worked out on this phone right now, so Hands-Free sends voice notes instead.",
          );
          listen();
        });
    }
  }, [conversation, deliverWords, dropRecorder, onProblem, sendNow, setPhase]);

  const start = useCallback(() => {
    if (phaseRef.current !== "off") return;
    void haptic("medium");
    stopReading();
    recordOnly.current = false;
    conversation.readEveryReply = true;
    setPhase("paused");
    listen();
  }, [conversation, listen, setPhase]);

  const end = useCallback(() => {
    dropRecorder();
    stopReading();
    conversation.readEveryReply = false;
    setPhase("off");
  }, [conversation, dropRecorder, setPhase]);

  const tap = useCallback(() => {
    void haptic("light");
    const now = phaseRef.current;
    if (now === "listening") void sendNow();
    else if (now === "speaking") {
      stopReading();
      listen();
    } else if (now === "paused") listen();
  }, [listen, sendNow]);

  // The turn ended: wait for the reply to be read, then listen again.
  useEffect(() => {
    if (phase !== "thinking" || state.busy) return;
    const id = window.setTimeout(() => {
      if (phaseRef.current !== "thinking") return;
      if (reader.get().phase === "idle" && !readStarted.current) listen();
    }, READ_GRACE_MS);
    return () => window.clearTimeout(id);
  }, [phase, state.busy, listen]);

  useEffect(() => {
    const current = phaseRef.current;
    if (reading.phase !== "idle" && (current === "thinking" || current === "speaking")) {
      readStarted.current = true;
      if (current === "thinking") setPhase("speaking");
    } else if (reading.phase === "idle" && current === "speaking") {
      readStarted.current = false;
      listen();
    }
  }, [reading.phase, listen, setPhase]);

  // Leaving the conversation (or the app going away) ends it.
  useEffect(() => {
    const hidden = () => {
      if (document.hidden && phaseRef.current !== "off") end();
    };
    document.addEventListener("visibilitychange", hidden);
    return () => {
      document.removeEventListener("visibilitychange", hidden);
      dropRecorder();
      conversation.readEveryReply = false;
    };
  }, [conversation, dropRecorder, end]);

  return { phase, level, words, start, end, tap };
}

/** The end of what was heard, so the newest words are always in view. */
function lastWords(text: string): string {
  if (text.length <= WORDS_SHOWN) return text;
  const cut = text.slice(-WORDS_SHOWN);
  return `…${cut.slice(cut.indexOf(" ") + 1)}`;
}

/** The panel in the composer's place while hands-free is on. */
export function HandsFreePanel({ handsFree, agent }: { handsFree: HandsFree; agent: string }) {
  const { phase, level, words: heard } = handsFree;
  const words = phaseText(phase, agent);
  const icon = phase === "speaking" ? volumeHigh : phase === "paused" ? pause : mic;
  const scale = phase === "listening" ? 1 + Math.min(level, 1) * 0.35 : 1;
  return (
    <div className="olano-handsfree" role="region" aria-label="Hands-free">
      <button
        type="button"
        className={`olano-handsfree-orb ${phase}`}
        aria-label={phase === "speaking" ? "Stop reading and talk" : phase === "listening" ? "Send now" : "Talk"}
        onClick={handsFree.tap}
        disabled={phase === "sending" || phase === "thinking"}
      >
        <span className="olano-handsfree-pulse" style={{ transform: `scale(${scale})` }} />
        <IonIcon icon={icon} />
      </button>
      <div className="olano-handsfree-words" aria-live="polite">
        <strong>{words.title}</strong>
        {words.hint && <span>{words.hint}</span>}
      </div>
      {heard && (phase === "listening" || phase === "sending") && (
        <p className="olano-handsfree-heard" aria-label="What was heard">
          {lastWords(heard)}
        </p>
      )}
      <IonButton fill="clear" size="small" onClick={handsFree.end}>
        End Hands-Free
      </IonButton>
    </div>
  );
}
