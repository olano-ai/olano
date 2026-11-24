import { useCallback, useRef, useState } from "react";
import { readSSE } from "../../core/sse";
import { errMessage } from "./helpers";
import type { ThinkFrame } from "./types";
import type { ErrorRemediation } from "../../core/types";

// Translator function shape (matches what useT(STR) returns); passed in by the
// page so this hook can localise the live phase status text.
type Translate = (key: string, vars?: Record<string, string>) => string;

// The eight phases "Think now" runs, in order. Mirrors MAGIC_PHASES.
// labelKey is a translation key resolved at render time.
export const THINK_PHASES: { id: string; labelKey: string }[] = [
  { id: "pulse", labelKey: "think.phase.pulse" },
  { id: "brainstorm", labelKey: "think.phase.brainstorm" },
  { id: "ideas", labelKey: "think.phase.ideas" },
  { id: "prototype", labelKey: "think.phase.prototype" },
  { id: "advance", labelKey: "think.phase.advance" },
  { id: "skills", labelKey: "think.phase.skills" },
  { id: "improve", labelKey: "think.phase.improve" },
  { id: "checks", labelKey: "think.phase.checks" },
];

// Per-phase noun translation keys (for "{count} {noun}s" badges).
const NOUN_KEY: Record<string, string> = {
  pulse: "think.noun.pulse",
  ideas: "think.noun.ideas",
  brainstorm: "think.noun.brainstorm",
  prototype: "think.noun.prototype",
  skills: "think.noun.skills",
  checks: "think.noun.checks",
};

export type PhaseState = "pending" | "running" | "done" | "error" | "skipped";
export interface PhaseRow {
  state: PhaseState;
  statusText: string;
  badge: string;
  // Machine code on a failed phase (e.g. "vault_locked") — drives the inline
  // "Unlock vault" affordance, mirroring the original's per-row unlock button.
  code?: string;
  // Structured fix-it for a failed phase (reconnect provider / set key / top up)
  // — drives a one-click <ErrorRemediation> button, same as chat.
  remediation?: ErrorRemediation | null;
}

export interface ThinkState {
  agent: string;
  running: boolean;
  done: boolean;
  error: string;
  // Error code on a top-level failure (e.g. "vault_locked").
  errorCode?: string;
  // Structured fix-it for a top-level failure.
  errorRemediation?: ErrorRemediation | null;
  total: number;
  phases: Record<string, PhaseRow>;
}

function initialPhases(t: Translate): Record<string, PhaseRow> {
  const r: Record<string, PhaseRow> = {};
  for (const p of THINK_PHASES) r[p.id] = { state: "pending", statusText: t("think.waiting"), badge: "" };
  return r;
}

// How many phases have reached a terminal state (done/error/skipped) — drives the
// "Thinking… (n)" counter on the button.
export function countFinished(state: ThinkState): number {
  return Object.values(state.phases).filter((p) => ["done", "error", "skipped"].includes(p.state)).length;
}

// Reconstruct a live ThinkState purely from the backend's "running phases" list
// (from /api/cognition/status). Used when the page was remounted (e.g. the user
// switched dashboard tabs) mid-run: the in-hook SSE state is gone, but the backend
// still reports which think-cycle phases are executing — so the button can stay
// "Thinking…" and the drawer can re-open with progress, exactly like the Optimize
// buttons already do. Phases run sequentially, so everything before the
// furthest-along running phase is treated as finished. Returns null when no
// think-cycle phase is running (the only running phases belong to optimize/etc).
export function thinkStateFromRunning(
  running: string[] | undefined,
  agent: string,
  t: Translate,
): ThinkState | null {
  const order = THINK_PHASES.map((p) => p.id);
  const live = (running || []).filter((id) => order.includes(id));
  if (!live.length) return null;
  const maxIdx = Math.max(...live.map((id) => order.indexOf(id)));
  const phases: Record<string, PhaseRow> = {};
  THINK_PHASES.forEach((p, i) => {
    const state: PhaseState = live.includes(p.id) ? "running" : i < maxIdx ? "done" : "pending";
    phases[p.id] = {
      state,
      statusText: state === "running" ? t("think.running") : state === "done" ? t("think.doneShort") : t("think.waiting"),
      badge: "",
    };
  });
  return { agent, running: true, done: false, error: "", total: 0, phases };
}

// Owns the long-running "Think now" SSE run. The run lives in this hook (held by
// the page), so it survives closing the drawer — re-opening shows live status,
// re-pressing while in-flight just re-opens (never restarts). Mirrors _thinkState.
export function useThinkRun(opts: { onPhaseDone: () => void; onFinished: () => void; t: Translate }) {
  const [state, setState] = useState<ThinkState | null>(null);
  const runningRef = useRef(false);
  const optsRef = useRef(opts);
  optsRef.current = opts;

  const finishedCount = state ? countFinished(state) : 0;

  const setPhase = useCallback((id: string, patch: Partial<PhaseRow>) => {
    setState((s) => (s ? { ...s, phases: { ...s.phases, [id]: { ...s.phases[id], ...patch } } } : s));
  }, []);

  const applyFrame = useCallback(
    (ev: ThinkFrame) => {
      const t = optsRef.current.t;
      if (ev.event === "phase_start") {
        setPhase(ev.id, { state: "running", statusText: ev.running || t("think.running"), badge: "" });
      } else if (ev.event === "phase_done") {
        if (ev.status === "skipped") {
          setPhase(ev.id, {
            state: "skipped",
            statusText: ev.summary ? String(ev.summary).slice(0, 200) : t("think.skipped"),
            badge: t("think.badge.skipped"),
          });
        } else if (ev.status === "error" || ev.status === "disabled") {
          setPhase(ev.id, {
            state: "error",
            statusText: ev.summary
              ? String(ev.summary).slice(0, 200)
              : ev.status === "disabled"
                ? t("think.disabled")
                : t("think.phaseFailed"),
            badge: ev.status === "disabled" ? t("think.badge.off") : t("think.badge.error"),
            code: ev.code || "",
            remediation: ev.remediation ?? null,
          });
        } else {
          const noun = t(NOUN_KEY[ev.id] || "think.noun.item");
          const hasSummary = !!(ev.summary && String(ev.summary).trim());
          const label = ev.count
            ? t("think.count", { count: String(ev.count), noun })
            : ev.id === "improve"
              ? t("think.reviewed")
              : hasSummary
                ? t("think.doneShort")
                : t("think.nothingNew");
          setPhase(ev.id, { state: "done", statusText: hasSummary ? String(ev.summary).slice(0, 200) : label, badge: label });
        }
        optsRef.current.onPhaseDone();
      } else if (ev.event === "all_done") {
        setState((s) => (s ? { ...s, total: ev.total_artifacts || 0 } : s));
      } else if (ev.event === "error") {
        setState((s) =>
          s
            ? { ...s, error: ev.message || t("think.error"), errorCode: ev.code || "", errorRemediation: ev.remediation ?? null }
            : s,
        );
      }
    },
    [setPhase],
  );

  const start = useCallback(
    (agent: string) => {
      if (runningRef.current) return; // already in flight — caller re-opens drawer
      runningRef.current = true;
      setState({ agent, running: true, done: false, error: "", total: 0, phases: initialPhases(optsRef.current.t) });

      void (async () => {
        // A Generate is minutes of work, so the tab that starts it is the one
        // most likely to be closed, slept or navigated away before it ends. The
        // run is detached server-side (olano/core/runs.py), so a dropped socket
        // is only a lost viewer: reconnect from the last `seq` seen and the
        // remaining phases arrive as if nothing happened.
        let cursor = 0;
        let sawTerminal = false;
        const consume = (ev: ThinkFrame & { seq?: number }) => {
          if (typeof ev.seq === "number" && ev.seq > cursor) cursor = ev.seq;
          if (ev.event === "all_done" || ev.event === "error") sawTerminal = true;
          applyFrame(ev);
        };
        try {
          const res = await fetch("/api/cognition/generate", {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
            body: JSON.stringify({ agent }),
          });
          if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
          await readSSE<ThinkFrame & { seq?: number }>(res, consume);
          // The stream ended without a terminal frame: the socket dropped, not
          // the run. Rejoin it, once — a second failure is a real one.
          if (!sawTerminal) {
            const again = await fetch(
              `/api/cognition/generate/attach?agent=${encodeURIComponent(agent)}&cursor=${cursor}`,
              { credentials: "include", headers: { Accept: "text/event-stream" } },
            );
            if (again.ok && again.body) await readSSE<ThinkFrame & { seq?: number }>(again, consume);
          }
        } catch (e) {
          setState((s) => (s ? { ...s, error: errMessage(e, optsRef.current.t("think.failedFallback")) } : s));
        } finally {
          runningRef.current = false;
          setState((s) => (s ? { ...s, running: false, done: !s.error } : s));
          optsRef.current.onFinished();
        }
      })();
    },
    [applyFrame],
  );

  const clear = useCallback(() => setState(null), []);

  return { state, finishedCount, start, clear };
}
