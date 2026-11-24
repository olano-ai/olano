/**
 * A voice note in a message: play and pause, a waveform that fills as it
 * plays (tap or drag it to jump), the time, a speed button while it plays,
 * and under it what the deployment heard: the transcript, or why there is
 * none. Used for the person's recordings and for an agent's spoken replies.
 *
 * A recording this phone made carries its waveform and length from the
 * recorder. One from the deployment is decoded once to draw its waveform and
 * learn its length; a file the phone cannot decode gets a stable placeholder
 * shape and still plays.
 */

import { IonIcon, IonSpinner } from "@ionic/react";
import { mic, pause, play } from "ionicons/icons";
import { useEffect, useRef, useState } from "react";

import { downsamplePeaks, formatDuration, placeholderPeaks, WAVE_BARS, type MessageVoice } from "../chat/voice.ts";
import { useStore } from "../state/store.ts";
import { cycleRate, pauseVoice, player, playVoice, seekVoice } from "./audioPlayer.ts";
import { useLoadedMedia } from "./mediaCache.ts";

interface Shape {
  durationMs: number;
  peaks: number[];
}

const decoded = new Map<string, Promise<Shape | null>>();

function decodeShape(key: string, blob: Blob): Promise<Shape | null> {
  let pending = decoded.get(key);
  if (!pending) {
    pending = (async () => {
      const w = window as unknown as { OfflineAudioContext?: typeof OfflineAudioContext; webkitOfflineAudioContext?: typeof OfflineAudioContext };
      const Ctor = w.OfflineAudioContext ?? w.webkitOfflineAudioContext;
      if (!Ctor) return null;
      try {
        const ctx = new Ctor(1, 44100, 44100);
        const bytes = await blob.arrayBuffer();
        // The callback form: older WebKit has no promise from decodeAudioData.
        const buffer = await new Promise<AudioBuffer>((resolve, reject) => {
          const result = ctx.decodeAudioData(bytes, resolve, reject);
          if (result && typeof (result as Promise<AudioBuffer>).then === "function") {
            (result as Promise<AudioBuffer>).then(resolve, reject);
          }
        });
        const data = buffer.getChannelData(0);
        const slices = WAVE_BARS * 5;
        const size = Math.max(1, Math.floor(data.length / slices));
        const levels: number[] = [];
        for (let i = 0; i < slices; i++) {
          let sum = 0;
          const start = i * size;
          for (let j = start; j < start + size && j < data.length; j++) sum += data[j] * data[j];
          levels.push(Math.sqrt(sum / size));
        }
        return { durationMs: buffer.duration * 1000, peaks: downsamplePeaks(levels, WAVE_BARS) };
      } catch {
        return null;
      }
    })();
    decoded.set(key, pending);
  }
  return pending;
}

export function VoiceNote({
  voice,
  mine,
  sending,
  transcript,
  untranscribed,
  transcribing,
}: {
  voice: MessageVoice;
  mine: boolean;
  sending?: boolean;
  transcript?: string;
  untranscribed?: boolean;
  /** The phone is turning it into words before it is sent. */
  transcribing?: boolean;
}) {
  const media = useLoadedMedia(voice);
  const [shape, setShape] = useState<Shape | null>(
    voice.peaks && voice.durationMs ? { durationMs: voice.durationMs, peaks: voice.peaks } : null,
  );
  const [audioDuration, setAudioDuration] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const wave = useRef<HTMLDivElement>(null);
  const state = useStore(player);

  // A deployment file: learn its shape from its bytes.
  useEffect(() => {
    if (shape || !media?.blob) return;
    let alive = true;
    void decodeShape(voice.key, media.blob).then((result) => {
      if (alive) setShape(result ?? { durationMs: 0, peaks: placeholderPeaks(voice.key) });
    });
    return () => {
      alive = false;
    };
  }, [media, shape, voice.key]);

  // When decoding gave no length, the audio element may still know it.
  useEffect(() => {
    if (!media || (shape && shape.durationMs)) return;
    const probe = new Audio();
    probe.preload = "metadata";
    probe.src = media.url;
    const onMeta = () => {
      if (Number.isFinite(probe.duration) && probe.duration > 0) setAudioDuration(probe.duration * 1000);
    };
    probe.addEventListener("loadedmetadata", onMeta);
    return () => {
      probe.removeEventListener("loadedmetadata", onMeta);
      probe.src = "";
    };
  }, [media, shape]);

  const active = state.key === voice.key;
  const durationMs = shape?.durationMs || audioDuration || (active ? state.durationMs : 0);
  const playing = active && state.playing;
  const progress = active && durationMs ? Math.min(1, state.position / durationMs) : 0;
  const peaks = shape?.peaks ?? voice.peaks ?? placeholderPeaks(voice.key);
  const shown = active && (state.playing || state.position > 0) ? state.position : durationMs;

  const seekTo = (clientX: number) => {
    const box = wave.current?.getBoundingClientRect();
    if (!box || !media || !box.width) return;
    seekVoice(voice.key, media.url, durationMs, (clientX - box.left) / box.width);
  };

  return (
    <div className={mine ? "olano-voice-wrap mine" : "olano-voice-wrap"}>
      <div className={`olano-voice ${mine ? "mine" : "theirs"}${sending ? " sending" : ""}`}>
        <button
          type="button"
          className="olano-voice-play"
          aria-label={playing ? "Pause voice note" : "Play voice note"}
          disabled={!media}
          onClick={() => {
            if (!media) return;
            if (playing) pauseVoice();
            else playVoice(voice.key, media.url, durationMs);
          }}
        >
          {media ? <IonIcon icon={playing ? pause : play} /> : <IonSpinner name="crescent" />}
        </button>
        <div
          ref={wave}
          className="olano-voice-wave"
          role="slider"
          tabIndex={0}
          aria-label="Voice note position"
          aria-valuemin={0}
          aria-valuemax={Math.round(durationMs / 1000)}
          aria-valuenow={Math.round((progress * durationMs) / 1000)}
          onPointerDown={(e) => {
            (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
            seekTo(e.clientX);
          }}
          onPointerMove={(e) => {
            if (e.buttons) seekTo(e.clientX);
          }}
          onKeyDown={(e) => {
            if (!media || !durationMs) return;
            const step = 5000 / durationMs;
            if (e.key === "ArrowRight") seekVoice(voice.key, media.url, durationMs, progress + step);
            if (e.key === "ArrowLeft") seekVoice(voice.key, media.url, durationMs, progress - step);
          }}
        >
          {peaks.map((height, i) => (
            <span
              key={i}
              className={(i + 0.5) / peaks.length <= progress ? "played" : undefined}
              style={{ height: `${Math.round(Math.max(0.12, height) * 100)}%` }}
            />
          ))}
        </div>
        <div className="olano-voice-meta">
          <span className="olano-voice-time">{formatDuration(shown)}</span>
          {active && (state.playing || state.position > 0) && (
            <button type="button" className="olano-voice-rate" aria-label="Playback speed" onClick={cycleRate}>
              {state.rate}x
            </button>
          )}
        </div>
      </div>
      {transcript ? (
        <button
          type="button"
          className={expanded ? "olano-voice-transcript open" : "olano-voice-transcript"}
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
        >
          {transcript}
        </button>
      ) : transcribing ? (
        <div className="olano-voice-transcript muted" role="status">
          Transcribing…
        </div>
      ) : untranscribed ? (
        <div className="olano-voice-transcript muted">{NOT_TRANSCRIBED}</div>
      ) : null}
    </div>
  );
}

const NOT_TRANSCRIBED = "Not transcribed: speech-to-text is off or failed on this deployment.";

/**
 * A voice note whose recording was not kept (a member's, which the deployment
 * transcribes and does not save): what it said, when it was heard.
 */
export function VoiceTranscript({ transcript, untranscribed }: { transcript?: string; untranscribed?: boolean }) {
  return (
    <div className="olano-voice-wrap mine">
      <div className="olano-bubble user olano-voice-heard">
        <span className="olano-voice-heard-label">
          <IonIcon icon={mic} aria-hidden="true" />
          Voice note
        </span>
        {transcript || (untranscribed ? NOT_TRANSCRIBED : "")}
      </div>
    </div>
  );
}
