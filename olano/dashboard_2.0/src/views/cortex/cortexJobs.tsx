import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import type { CortexJob, CortexJobsResponse } from "./types";

// The Cortex jobs the server is running right now, for every agent the viewer
// may see, shared by everything on the page that starts or shows one: the
// cards (Create project), the header (Think now, the optimizers, Runs), the
// phase rows. The server is the source of truth (olano/cognition/jobs.py): a
// job outlives the page that started it, so a card re-derives "Creating
// project..." from here after a reload, a tab switch or on another device,
// and its button follows the running job instead of starting a second one.
//
// Fed by two things: the `cortex_job` SSE frame the server publishes when a
// job starts or ends (instant), and a poll of `?active=true`, which is the
// floor when the stream is down. It polls fast only while something runs.

const POLL_RUNNING_MS = 3000;
const POLL_IDLE_MS = 20000;

export interface OpenJob {
  id: string;
  agent: string;
}

interface CortexJobsApi {
  running: CortexJob[];
  // Whether the server has answered at least once. Until then `running` is
  // empty because nothing has been asked, not because nothing runs.
  ready: boolean;
  // The live job of `kind` on `target` for `agent`, if one is running.
  liveFor: (agent: string, kind: string, target?: string) => CortexJob | undefined;
  // Record a job a request just started, before the next poll sees it.
  track: (job: CortexJob) => void;
  refresh: () => void;
  // Open one job's live log / result, or the Runs list.
  openJob: (job: OpenJob) => void;
  openRuns: () => void;
  // Called once per job this page saw running when it ends (any source).
  onFinished: (fn: (job: CortexJob) => void) => () => void;
}

const Ctx = createContext<CortexJobsApi | null>(null);

export function useCortexJobs(): CortexJobsApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useCortexJobs must be used within <CortexJobsProvider>");
  return ctx;
}

// Optional variant for components that may render outside the provider.
export function useCortexJobsOptional(): CortexJobsApi | null {
  return useContext(Ctx);
}

export function CortexJobsProvider({
  children,
  onOpenJob,
  onOpenRuns,
}: {
  children: ReactNode;
  onOpenJob: (job: OpenJob) => void;
  onOpenRuns: () => void;
}) {
  const [running, setRunning] = useState<CortexJob[]>([]);
  const [ready, setReady] = useState(false);
  const runningRef = useRef<Map<string, CortexJob>>(new Map());
  const listeners = useRef(new Set<(job: CortexJob) => void>());
  const openJobRef = useRef(onOpenJob);
  openJobRef.current = onOpenJob;
  const openRunsRef = useRef(onOpenRuns);
  openRunsRef.current = onOpenRuns;

  const publish = useCallback(() => {
    setRunning(
      Array.from(runningRef.current.values()).sort((a, b) => (a.started_at < b.started_at ? 1 : -1)),
    );
  }, []);

  // A job left the running set: tell the listeners how it ended. The SSE
  // frame carries the final record; a poll only sees it vanish, so fetch it.
  const ended = useCallback((job: CortexJob) => {
    const fire = (final: CortexJob) => listeners.current.forEach((fn) => fn(final));
    if (job.status !== "running") {
      fire(job);
      return;
    }
    api
      .get<{ job: CortexJob }>(
        `/api/cortex/jobs/${encodeURIComponent(job.id)}?agent=${encodeURIComponent(job.agent)}`,
      )
      .then((res) => fire(res.job))
      .catch(() => fire({ ...job, status: "interrupted" }));
  }, []);

  const refresh = useCallback(() => {
    api
      .get<CortexJobsResponse>("/api/cortex/jobs?all_agents=true&active=true")
      .then((res) => {
        const next = new Map((res.jobs || []).map((j) => [j.id, j] as const));
        for (const [id, job] of runningRef.current) if (!next.has(id)) ended(job);
        runningRef.current = next;
        publish();
        setReady(true);
      })
      .catch(() => {
        /* best-effort: the next tick or SSE frame catches up */
      });
  }, [ended, publish]);

  const track = useCallback(
    (job: CortexJob) => {
      if (job.status !== "running") return;
      runningRef.current.set(job.id, job);
      publish();
    },
    [publish],
  );

  // SSE: a job started or ended somewhere (this tab, another tab, the scheduler).
  useEffect(
    () =>
      subscribeStream((frame) => {
        const ev = frame as { type?: string; data?: { job?: CortexJob } };
        if (ev.type !== "cortex_job" || !ev.data?.job) return;
        const job = ev.data.job;
        if (job.status === "running") {
          runningRef.current.set(job.id, job);
        } else {
          const had = runningRef.current.delete(job.id);
          if (had) ended(job);
        }
        publish();
      }),
    [ended, publish],
  );

  // Poll: fast while anything runs, slow otherwise.
  const anyRunning = running.length > 0;
  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, anyRunning ? POLL_RUNNING_MS : POLL_IDLE_MS);
    return () => window.clearInterval(timer);
  }, [refresh, anyRunning]);

  const liveFor = useCallback(
    (agent: string, kind: string, target = "") =>
      running.find((j) => j.agent === agent && j.kind === kind && (j.target || "") === target),
    [running],
  );

  const onFinished = useCallback((fn: (job: CortexJob) => void) => {
    listeners.current.add(fn);
    return () => {
      listeners.current.delete(fn);
    };
  }, []);

  const value = useMemo<CortexJobsApi>(
    () => ({
      running,
      ready,
      liveFor,
      track,
      refresh,
      openJob: (job) => openJobRef.current(job),
      openRuns: () => openRunsRef.current(),
      onFinished,
    }),
    [running, ready, liveFor, track, refresh, onFinished],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

// Start a background Cortex action and hand back its job. `background: true`
// makes the server answer at once; a second press while one runs answers with
// that same job (`already_running`), so this never starts anything twice.
export async function startCortexJob(
  url: string,
  body: Record<string, unknown>,
): Promise<{ job: CortexJob; alreadyRunning: boolean }> {
  const res = await api.post<{ job: CortexJob; already_running: boolean }>(url, { ...body, background: true });
  return { job: res.job, alreadyRunning: !!res.already_running };
}
