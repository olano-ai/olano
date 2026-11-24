import { useCallback, useRef, useState } from "react";
import { api } from "../../core/api";
import { errMessage } from "./helpers";
import { startCortexJob } from "./cortexJobs";
import type { CortexJob, OptimizeResponse } from "./types";

// Translator shape (matches useT(STR)) — passed in so this hook can localise.
type Translate = (key: string, vars?: Record<string, string>) => string;

export type OptimizeKind = "workspace" | "skills";

const ENDPOINT: Record<OptimizeKind, string> = {
  workspace: "/api/cognition/optimize-workspace",
  skills: "/api/cognition/optimize-skills",
};

// The Cortex job kind each optimizer runs as (olano/cognition/jobs.py).
export const OPTIMIZE_JOB_KIND: Record<OptimizeKind, string> = {
  workspace: "optimize_workspace",
  skills: "optimize_skills",
};

const POLL_MS = 2000;

export interface OptimizeRun {
  kind: OptimizeKind;
  agent: string; // the agent this run belongs to (guards against agent switches)
  // The server-side job this run follows.
  jobId: string;
  running: boolean;
  result: OptimizeResponse | null;
  error: string | null;
}

// Owns the "Optimize workspace/skills" runs (one per kind). The run itself is a
// server-side Cortex job, so it survives closing the drawer, leaving the page
// and a reload; this hook only follows it. Pressing the button while one runs
// joins it (the server answers with the same job), and `adopt` picks up a job
// this page did not start (another tab, before a reload) so its drawer works.
export function useOptimizeRun(opts: { onDone: () => void; t: Translate }) {
  const [runs, setRuns] = useState<Record<OptimizeKind, OptimizeRun | null>>({
    workspace: null,
    skills: null,
  });
  // Monotonic token per kind, bumped on every start/adopt/clear. A poll only
  // applies its result if its token is still current, so a stale follow (one
  // for a previous agent) can't clobber a newer run's state.
  const tokenRef = useRef<Record<OptimizeKind, number>>({ workspace: 0, skills: 0 });
  const followingRef = useRef<Record<OptimizeKind, string>>({ workspace: "", skills: "" });
  const optsRef = useRef(opts);
  optsRef.current = opts;

  const follow = useCallback((kind: OptimizeKind, agent: string, jobId: string) => {
    const token = ++tokenRef.current[kind];
    followingRef.current[kind] = jobId;
    setRuns((r) => ({ ...r, [kind]: { kind, agent, jobId, running: true, result: null, error: null } }));
    void (async () => {
      let misses = 0;
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, POLL_MS));
        if (tokenRef.current[kind] !== token) return;
        let job: CortexJob;
        try {
          job = (
            await api.get<{ job: CortexJob }>(
              `/api/cortex/jobs/${encodeURIComponent(jobId)}?agent=${encodeURIComponent(agent)}`,
            )
          ).job;
          misses = 0;
        } catch (e) {
          // A dropped request is not a dropped run: keep following, and give up
          // only once the server has stopped answering for a while.
          if (++misses < 15) continue;
          if (tokenRef.current[kind] === token) {
            const msg = errMessage(e, optsRef.current.t("opt.toast.failed"));
            setRuns((r) => ({ ...r, [kind]: { kind, agent, jobId, running: false, result: null, error: msg } }));
            followingRef.current[kind] = "";
          }
          return;
        }
        if (tokenRef.current[kind] !== token) return;
        if (job.status === "running") continue;
        const result = job.result && Object.keys(job.result).length ? (job.result as unknown as OptimizeResponse) : null;
        const error = result ? null : job.error || optsRef.current.t("opt.toast.failed");
        setRuns((r) => ({ ...r, [kind]: { kind, agent, jobId, running: false, result, error } }));
        followingRef.current[kind] = "";
        optsRef.current.onDone();
        return;
      }
    })();
  }, []);

  const start = useCallback(
    (kind: OptimizeKind, agent: string) => {
      if (followingRef.current[kind]) return; // already following one, caller re-opens the drawer
      void (async () => {
        try {
          const { job } = await startCortexJob(ENDPOINT[kind], { agent });
          follow(kind, agent, job.id);
        } catch (e) {
          const msg = errMessage(e, optsRef.current.t("opt.toast.failed"));
          setRuns((r) => ({ ...r, [kind]: { kind, agent, jobId: "", running: false, result: null, error: msg } }));
        }
      })();
      // Show the drawer's working state at once; the job id lands a beat later.
      setRuns((r) => ({ ...r, [kind]: { kind, agent, jobId: "", running: true, result: null, error: null } }));
    },
    [follow],
  );

  // Follow a job already running on the server that this page did not start.
  const adopt = useCallback(
    (kind: OptimizeKind, agent: string, jobId: string) => {
      if (followingRef.current[kind] === jobId) return;
      follow(kind, agent, jobId);
    },
    [follow],
  );

  // Drop a finished/cancelled run's state (e.g. on agent switch or after
  // dismiss). Bumping the token stops following; the job itself keeps running.
  const clear = useCallback((kind: OptimizeKind) => {
    tokenRef.current[kind] += 1;
    followingRef.current[kind] = "";
    setRuns((r) => ({ ...r, [kind]: null }));
  }, []);

  return { runs, start, adopt, clear };
}
