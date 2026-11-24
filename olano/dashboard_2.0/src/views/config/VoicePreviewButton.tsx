import { useEffect, useRef, useState } from "react";
import { Loader2, Play, Square } from "lucide-react";
import { apiErrorFrom } from "../../core/api";
import { useToast } from "../../ui/toast";

// One-sample silent WAV. Synthesis takes seconds, and Safari only lets an
// element play sound if play() was first called on it inside the click: this
// is what that first call plays, so the real clip can start when it arrives.
const SILENCE = "data:audio/wav;base64,UklGRiwAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQgAAACAgICAgICAgA==";

export interface VoicePreviewSettings {
  provider: string;
  model: string;
  voice: string;
  url: string;
  api_key_name: string;
}

type Phase = "idle" | "loading" | "playing";

/** Plays a short sample in the TTS voice the form currently describes, saved
 *  or not (`POST /api/system/config/speech/preview`). A second click cancels
 *  the request or stops playback. */
export function VoicePreviewButton({
  settings,
  text,
  labels,
}: {
  settings: VoicePreviewSettings;
  text: string;
  labels: { play: string; loading: string; stop: string; failed: string };
}) {
  const toast = useToast();
  const [phase, setPhase] = useState<Phase>("idle");
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const reset = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    const audio = audioRef.current;
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
    }
    audioRef.current = null;
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = null;
  };

  useEffect(() => reset, []);

  const stop = () => {
    reset();
    setPhase("idle");
  };

  const start = async () => {
    reset();
    const audio = new Audio(SILENCE);
    audio.play().catch(() => {});
    audioRef.current = audio;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setPhase("loading");
    try {
      const res = await fetch("/api/system/config/speech/preview", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tts: settings, text }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw apiErrorFrom(res.status, res.statusText, await res.text());
      const blob = await res.blob();
      if (ctrl.signal.aborted || audioRef.current !== audio) return;
      abortRef.current = null;
      const url = URL.createObjectURL(blob);
      urlRef.current = url;
      audio.onended = stop;
      audio.onerror = () => {
        stop();
        toast.push(labels.failed, "error");
      };
      audio.src = url;
      await audio.play();
      setPhase("playing");
    } catch (err) {
      if (ctrl.signal.aborted) return;
      stop();
      const why = err instanceof Error ? err.message : "";
      toast.push(why ? `${labels.failed} ${why}` : labels.failed, "error");
    }
  };

  const label = phase === "idle" ? labels.play : phase === "loading" ? labels.loading : labels.stop;
  return (
    <button
      type="button"
      className="btn icon"
      title={label}
      aria-label={label}
      data-testid="voice-preview"
      onClick={() => (phase === "idle" ? void start() : stop())}
    >
      {phase === "idle" ? (
        <Play size={16} />
      ) : phase === "loading" ? (
        <Loader2 size={16} className="todo-spin" />
      ) : (
        <Square size={14} />
      )}
    </button>
  );
}
