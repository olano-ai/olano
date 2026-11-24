import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Bot,
  Check,
  CircleDot,
  CornerDownRight,
  ListChecks,
  MessageSquare,
  Play,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { errMessage, formatTs } from "./helpers";
import { Drawer, Empty, Markdown } from "./ui";
import { STR } from "./strings";
import type { CortexJob, CortexJobLogEntry, CortexJobStatus } from "./types";

const POLL_MS = 1500;

// Status → color, shared with the Runs list and the cards.
export const JOB_STATUS_COLOR: Record<CortexJobStatus, string> = {
  running: "var(--accent)",
  done: "var(--success)",
  error: "var(--danger)",
  stopped: "var(--warn)",
  interrupted: "var(--warn)",
};

const STEP_ICON: Record<string, LucideIcon> = {
  start: Play,
  end: Check,
  end_error: AlertTriangle,
  step: ListChecks,
  step_done: ListChecks,
  step_error: AlertTriangle,
  worker: Bot,
  worker_error: AlertTriangle,
  tool: Wrench,
  tool_result: CornerDownRight,
  tool_error: AlertTriangle,
  text: MessageSquare,
};

const ERROR_TYPES = new Set(["end_error", "step_error", "worker_error", "tool_error"]);

type Translate = (key: string, vars?: Record<string, string>) => string;

// `key` translated, or `fallbackKey` when the dictionary has no entry (a job
// kind or source a newer backend added): useT answers a miss with the key.
function tOr(t: Translate, key: string, fallbackKey: string): string {
  const hit = t(key);
  return hit === key ? t(fallbackKey) : hit;
}

export const jobKindLabel = (t: Translate, kind: string) => tOr(t, `jobs.kind.${kind}`, "jobs.kind.other");
export const jobSourceLabel = (t: Translate, source: string) => {
  const hit = t(`jobs.source.${source}`);
  return hit === `jobs.source.${source}` ? source : hit;
};

// "12s", "3m 04s", "1h 02m".
export function formatDuration(seconds: number | undefined): string {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

// Seconds since `startedAt`, ticking once a second while `live`.
export function useElapsed(startedAt: string | undefined, live: boolean, fallback = 0): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);
  if (!live || !startedAt) return fallback;
  const t0 = Date.parse(startedAt);
  return Number.isFinite(t0) ? Math.max(0, (now - t0) / 1000) : fallback;
}

function clockTime(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function StepRow({ entry }: { entry: CortexJobLogEntry }) {
  const Icon = STEP_ICON[entry.type] || CircleDot;
  const isError = ERROR_TYPES.has(entry.type);
  const long = entry.text.length > 280 || entry.text.split("\n").length > 4;
  const muted = entry.type === "tool_result";
  const body = (
    <span
      style={{
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        fontFamily: entry.type === "tool" || entry.type === "tool_result" ? "var(--font-mono)" : undefined,
        fontSize: entry.type === "tool" || entry.type === "tool_result" ? 11.5 : 12.5,
        color: isError ? "var(--danger)" : undefined,
      }}
      className={muted ? "muted" : undefined}
    >
      {entry.text}
    </span>
  );
  return (
    <div className="cortex-job-step" data-type={entry.type} style={{ display: "flex", gap: 8, padding: "6px 0" }}>
      <span
        aria-hidden
        style={{ flexShrink: 0, paddingTop: 2, color: isError ? "var(--danger)" : "var(--text-3, var(--muted))" }}
      >
        <Icon size={13} strokeWidth={1.75} />
      </span>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="row" style={{ gap: 6, alignItems: "baseline" }}>
          {entry.tool ? (
            <code style={{ fontSize: 11.5, fontWeight: 600 }}>{entry.tool}</code>
          ) : null}
          <span className="muted" style={{ fontSize: 10.5, marginLeft: "auto", flexShrink: 0 }}>
            {clockTime(entry.ts)}
          </span>
        </div>
        {entry.text ? (
          long ? (
            <details>
              <summary style={{ cursor: "pointer", fontSize: 12 }} className="muted">
                {entry.text.slice(0, 160).replace(/\s+/g, " ")}…
              </summary>
              {body}
            </details>
          ) : (
            body
          )
        ) : null}
      </div>
    </div>
  );
}

// One Cortex job: what it is doing now, every step it took, and how it ended.
// A pure view of the server's record (GET /api/cortex/jobs/{id}), polled while
// the job runs, so closing this changes nothing about the run, re-opening
// shows exactly where it is, and it reads the same after a reload or from
// another device.
export function JobDrawer({
  agent,
  jobId,
  onClose,
  onViewProjects,
}: {
  agent: string;
  jobId: string;
  onClose: () => void;
  // Promote jobs offer "View projects" once the project exists.
  onViewProjects?: () => void;
}) {
  const t = useT(STR);
  const toast = useToast();
  const [job, setJob] = useState<CortexJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  const running = job?.status === "running";

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const load = () => {
      api
        .get<{ job: CortexJob }>(`/api/cortex/jobs/${encodeURIComponent(jobId)}?agent=${encodeURIComponent(agent)}`)
        .then((res) => {
          if (!alive) return;
          setJob(res.job);
          setError(null);
          if (res.job.status === "running") timer = window.setTimeout(load, POLL_MS);
        })
        .catch((e) => {
          if (!alive) return;
          setError(errMessage(e, t("jobs.loadFailed")));
          // A blip must not freeze a live view: keep trying while it may run.
          timer = window.setTimeout(load, POLL_MS * 3);
        });
    };
    load();
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [agent, jobId, t]);

  // Follow the log while it grows, unless the reader scrolled up to read.
  useEffect(() => {
    const el = logRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [job?.log?.length]);

  const elapsed = useElapsed(job?.started_at, running, job?.duration_seconds ?? 0);

  async function stop() {
    if (!job) return;
    setStopping(true);
    try {
      await api.post(`/api/cortex/jobs/${encodeURIComponent(job.id)}/stop`, { agent: job.agent });
      toast.push(t("jobs.toast.stopping"));
    } catch (e) {
      toast.push(errMessage(e, t("jobs.toast.stopFailed")), "error");
    } finally {
      setStopping(false);
    }
  }

  const result = (job?.result || {}) as {
    project?: { path?: string; title?: string; kind?: string };
    artifacts?: string[];
  };
  const projectPath = job?.kind === "promote" ? result.project?.path : undefined;
  const artifacts = (result.artifacts || []).filter((p) => typeof p === "string" && p && p !== projectPath);
  const lastStep = job?.log?.length ? job.log[job.log.length - 1] : job?.last_step;

  return (
    <Drawer
      title={job?.title || t("jobs.title")}
      sub={
        job ? (
          <span className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <span>{job.agent}</span>
            <span>·</span>
            <span style={{ color: JOB_STATUS_COLOR[job.status] || undefined, fontWeight: 600 }} data-testid="job-status">
              {t(`jobs.status.${job.status}`)}
            </span>
            <span>·</span>
            <span>{jobSourceLabel(t, job.source)}</span>
            <span>·</span>
            <span title={job.started_at}>
              {running ? t("jobs.elapsed", { time: formatDuration(elapsed) }) : formatTs(job.started_at)}
            </span>
          </span>
        ) : (
          agent
        )
      }
      onClose={onClose}
      width={560}
      footer={
        <>
          {running ? (
            <button className="btn danger" disabled={stopping} onClick={() => void stop()} style={{ marginRight: "auto" }}>
              {stopping ? t("jobs.stopping") : t("jobs.stop")}
            </button>
          ) : null}
          {projectPath && onViewProjects ? (
            <button
              className="btn"
              onClick={() => {
                onClose();
                onViewProjects();
              }}
            >
              {t("items.promote.viewProjects")}
            </button>
          ) : null}
          <button className="btn primary" onClick={onClose}>
            {running ? t("jobs.hide") : t("jobs.close")}
          </button>
        </>
      }
    >
      {!job ? (
        error ? <Empty title={t("jobs.loadFailed")} sub={error} /> : <div className="spinner" />
      ) : (
        <div data-testid="cortex-job-drawer" data-job-status={job.status}>
          {running ? (
            <div
              className="card"
              style={{ padding: 14, display: "flex", gap: 12, alignItems: "flex-start", marginBottom: 14 }}
            >
              <div className="spinner" style={{ flexShrink: 0, marginTop: 2 }} />
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600 }}>{tOr(t, `jobs.working.${job.kind}`, "jobs.working.other")}</div>
                {lastStep ? (
                  <div
                    className="muted"
                    style={{ fontSize: 12.5, marginTop: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  >
                    {lastStep.tool ? `${lastStep.tool}: ` : ""}
                    {lastStep.text}
                  </div>
                ) : null}
                <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                  {t("jobs.keepsRunning")}
                </div>
              </div>
            </div>
          ) : job.status === "done" ? (
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontWeight: 600, display: "flex", alignItems: "center", gap: 6, color: "var(--success)" }}>
                <Check size={15} strokeWidth={1.75} />
                {job.kind === "promote" ? t("items.promote.created") : t("jobs.finished", { time: formatDuration(job.duration_seconds) })}
              </div>
              {projectPath ? (
                <Link
                  to={`/files?path=${encodeURIComponent(projectPath)}&agent=${encodeURIComponent(job.agent)}`}
                  style={{ fontSize: 13, fontFamily: "var(--font-mono)", display: "inline-block", marginTop: 4 }}
                  title={t("items.openProjectFolder")}
                >
                  {projectPath}
                </Link>
              ) : null}
              {job.summary ? <Markdown source={job.summary} style={{ marginTop: 10 }} /> : null}
            </div>
          ) : (
            <div className="card" style={{ padding: 14, marginBottom: 14, borderLeft: `3px solid ${JOB_STATUS_COLOR[job.status]}` }}>
              <div style={{ fontWeight: 600 }}>{t(`jobs.ended.${job.status}`)}</div>
              {job.error || job.summary ? (
                <div className="muted" style={{ fontSize: 13, marginTop: 4, whiteSpace: "pre-wrap" }}>
                  {job.error || job.summary}
                </div>
              ) : null}
            </div>
          )}

          {artifacts.length ? (
            <div style={{ marginBottom: 14 }}>
              <div className="muted" style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                {t("jobs.artifacts")}
              </div>
              {artifacts.map((p) => (
                <div key={p}>
                  <Link
                    to={`/files?path=${encodeURIComponent(p)}&agent=${encodeURIComponent(job.agent)}`}
                    style={{ fontSize: 12.5, fontFamily: "var(--font-mono)" }}
                  >
                    {p}
                  </Link>
                </div>
              ))}
            </div>
          ) : null}

          <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
            <div className="muted" style={{ fontSize: 12, fontWeight: 600 }}>
              {t("jobs.steps", { count: String(job.log_count || job.log?.length || 0) })}
            </div>
            {job.log_dropped ? (
              <div className="muted" style={{ fontSize: 11 }}>
                {t("jobs.dropped", { count: String(job.log_dropped) })}
              </div>
            ) : null}
          </div>
          <div
            ref={logRef}
            data-testid="cortex-job-log"
            onScroll={(e) => {
              const el = e.currentTarget;
              stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
            }}
            style={{
              maxHeight: "52vh",
              overflowY: "auto",
              borderTop: "1px solid var(--border)",
              borderBottom: "1px solid var(--border)",
            }}
          >
            {(job.log || []).length ? (
              (job.log || []).map((entry, i) => <StepRow key={`${entry.ts}:${i}`} entry={entry} />)
            ) : (
              <div className="muted" style={{ fontSize: 12.5, padding: "10px 0" }}>
                {t("jobs.noSteps")}
              </div>
            )}
          </div>
        </div>
      )}
    </Drawer>
  );
}
