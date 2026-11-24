import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { errMessage, relPast } from "./helpers";
import { Drawer, Empty } from "./ui";
import { AgentTag } from "./AgentTag";
import { STR } from "./strings";
import { JOB_STATUS_COLOR, formatDuration, jobKindLabel, jobSourceLabel, useElapsed } from "./JobDrawer";
import { useCortexJobs } from "./cortexJobs";
import type { CortexJob, CortexJobsResponse } from "./types";

type Filter = "all" | "running" | "failed";
const FILTERS: Filter[] = ["all", "running", "failed"];
const FAILED = new Set(["error", "stopped", "interrupted"]);

function RunRow({ job, onOpen }: { job: CortexJob; onOpen: () => void }) {
  const t = useT(STR);
  const running = job.status === "running";
  const elapsed = useElapsed(job.started_at, running, job.duration_seconds ?? 0);
  const kindLabel = jobKindLabel(t, job.kind);
  return (
    <button
      type="button"
      className="cortex-run-row"
      data-testid="cortex-run-row"
      data-job-status={job.status}
      onClick={onOpen}
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        background: "none",
        border: "none",
        borderBottom: "1px solid var(--border)",
        padding: "10px 0",
        color: "inherit",
        cursor: "pointer",
      }}
    >
      <div className="row" style={{ gap: 8, alignItems: "flex-start", justifyContent: "space-between" }}>
        <div className="row" style={{ gap: 8, alignItems: "flex-start", minWidth: 0 }}>
          {running ? (
            <div className="spinner" style={{ width: 12, height: 12, flexShrink: 0, marginTop: 3 }} />
          ) : (
            <span
              aria-hidden
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: JOB_STATUS_COLOR[job.status],
                flexShrink: 0,
                marginTop: 6,
              }}
            />
          )}
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 13, overflowWrap: "anywhere" }}>{job.title}</div>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>
              <span style={{ color: JOB_STATUS_COLOR[job.status], fontWeight: 600 }}>{t(`jobs.status.${job.status}`)}</span>
              {" · "}
              {kindLabel}
              {" · "}
              {jobSourceLabel(t, job.source)}
              {" · "}
              {running ? formatDuration(elapsed) : `${relPast(job.started_at)} · ${formatDuration(job.duration_seconds)}`}
            </div>
            {running && job.last_step ? (
              <div
                className="muted"
                style={{ fontSize: 11.5, marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
              >
                {job.last_step.tool ? `${job.last_step.tool}: ` : ""}
                {job.last_step.text}
              </div>
            ) : !running && FAILED.has(job.status) && job.error ? (
              <div
                style={{
                  fontSize: 11.5,
                  marginTop: 2,
                  color: "var(--danger)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {job.error}
              </div>
            ) : null}
          </div>
        </div>
        <span style={{ flexShrink: 0 }}>
          <AgentTag name={job.agent} title={t("agentTag.title", { name: job.agent })} />
        </span>
      </div>
    </button>
  );
}

// Every Cortex run, newest first: what is running now and the history, across
// every agent the viewer may see (narrowed by the page's agents filter). Each
// row opens the run's full step log. Re-polls while anything is running.
export function RunsDrawer({
  hidden,
  onClose,
}: {
  // The page's agents filter (hidden agents).
  hidden: Set<string>;
  onClose: () => void;
}) {
  const t = useT(STR);
  const jobs = useCortexJobs();
  const [rows, setRows] = useState<CortexJob[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const load = useCallback(() => {
    api
      .get<CortexJobsResponse>("/api/cortex/jobs?all_agents=true&limit=150")
      .then((res) => {
        setRows(res.jobs || []);
        setError(null);
      })
      .catch((e) => setError(errMessage(e, t("jobs.loadFailed"))));
  }, [t]);

  // Reload when the set of running jobs changes (a run started or ended), and
  // on a slow tick while something runs so the last-step lines move.
  const runningKey = jobs.running.map((j) => j.id).join(",");
  useEffect(() => {
    load();
  }, [load, runningKey]);
  useEffect(() => {
    if (!runningKey) return;
    const timer = window.setInterval(load, 4000);
    return () => window.clearInterval(timer);
  }, [load, runningKey]);

  const visible = useMemo(() => (rows || []).filter((r) => !hidden.has(r.agent)), [rows, hidden]);
  const counts: Record<Filter, number> = {
    all: visible.length,
    running: visible.filter((r) => r.status === "running").length,
    failed: visible.filter((r) => FAILED.has(r.status)).length,
  };
  const shown = visible.filter((r) =>
    filter === "running" ? r.status === "running" : filter === "failed" ? FAILED.has(r.status) : true,
  );

  return (
    <Drawer
      title={t("jobs.runs.title")}
      sub={t("jobs.runs.sub")}
      onClose={onClose}
      width={560}
      footer={
        <>
          <button className="btn" onClick={load} style={{ marginRight: "auto" }}>
            {t("items.refresh")}
          </button>
          <button className="btn primary" onClick={onClose}>
            {t("jobs.close")}
          </button>
        </>
      }
    >
      <div className="row" style={{ gap: 4, marginBottom: 10, flexWrap: "wrap" }}>
        {FILTERS.map((f) => (
          <button
            key={f}
            className={f === filter ? "btn primary" : "btn"}
            style={{ fontSize: 12.5 }}
            onClick={() => setFilter(f)}
          >
            {t(`jobs.filter.${f}`)}
            {counts[f] ? (
              <span
                className="badge"
                style={{ marginLeft: 4, ...(f === filter ? { background: "color-mix(in srgb, var(--accent-fg) 25%, transparent)", color: "var(--accent-fg)" } : {}) }}
              >
                {counts[f]}
              </span>
            ) : null}
          </button>
        ))}
      </div>
      {error && rows === null ? (
        <Empty title={t("jobs.loadFailed")} sub={error} />
      ) : rows === null ? (
        <div className="spinner" />
      ) : shown.length === 0 ? (
        <Empty title={t(`jobs.runs.empty.${filter}`)} sub={filter === "all" ? t("jobs.runs.empty.sub") : undefined} />
      ) : (
        <div data-testid="cortex-runs-list">
          {shown.map((job) => (
            <RunRow key={job.id} job={job} onOpen={() => jobs.openJob({ id: job.id, agent: job.agent })} />
          ))}
        </div>
      )}
    </Drawer>
  );
}
