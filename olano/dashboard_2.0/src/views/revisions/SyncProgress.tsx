// A running GitHub operation drawn as it happens: one row per step (done,
// under way, still to come), an overall bar, and git's own figures for an
// upload or download (objects, megabytes, speed). The data is the `busy`
// record of GET /api/revisions/remote, which the caller polls while it runs.

import { useEffect, useState } from "react";
import { Check, Circle, LoaderCircle } from "lucide-react";
import { useT } from "../../core/i18n";
import { elapsed } from "./format";
import { STR } from "./strings";
import type { RemoteOperation } from "./types";

// How much of the whole bar each step is worth: moving the history over the
// network is nearly all of the wait, so a step that takes a second does not
// claim a quarter of the bar.
const WEIGHT: Record<string, number> = {
  check: 5,
  snapshot: 15,
  upload: 75,
  download: 75,
  apply: 15,
  save: 5,
};

export function overallPercent(op: RemoteOperation): number | null {
  const p = op.progress;
  if (!p || !p.steps.length) return null;
  const weights = p.steps.map((s) => WEIGHT[s] ?? 10);
  const sum = weights.reduce((a, b) => a + b, 0);
  const at = p.step ? p.steps.indexOf(p.step) : -1;
  if (at < 0) return 0;
  const before = weights.slice(0, at).reduce((a, b) => a + b, 0);
  const within = (weights[at] * (p.percent ?? 0)) / 100;
  return Math.min(100, ((before + within) / sum) * 100);
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

export function SyncProgress({
  op,
  showBy = false,
  testId,
}: {
  op: RemoteOperation;
  /** Name who started it: someone else's operation, seen from this page. */
  showBy?: boolean;
  testId?: string;
}) {
  const t = useT(STR);
  const now = useNow(true);
  const p = op.progress;
  const steps = p?.steps ?? [];
  const at = p?.step ? steps.indexOf(p.step) : -1;
  const overall = overallPercent(op);
  // A step with no figure of its own (a snapshot, GitHub's checks) still
  // moves: the bar animates instead of sitting still at a number.
  const measuring = p?.percent === null || p?.percent === undefined;
  const label = t(`gh.op.${op.op}`);

  let detail = "";
  if (p?.phase && p.done !== undefined && p.total !== undefined) {
    detail = t("prog.objects", {
      phase: t(`prog.phase.${p.phase}`),
      done: p.done.toLocaleString(),
      total: p.total.toLocaleString(),
    });
  }
  if (p?.transfer) detail = detail ? `${detail} · ${p.transfer}` : p.transfer;

  return (
    <div className="gh-progress" role="status" aria-live="polite" data-testid={testId}>
      <div className="gh-progress-head">
        <LoaderCircle size={15} strokeWidth={2} className="gh-spin" aria-hidden />
        <strong>{t("prog.running", { op: label })}</strong>
        <span className="gh-progress-meta">
          {showBy ? t("prog.by", { by: op.by }) + " · " : ""}
          {elapsed(op.since, now)}
        </span>
      </div>
      {/* One step (a push, a check) is the bar itself; a list of one only
          repeats it. */}
      {steps.length > 1 ? (
        <ol className="gh-steps">
          {steps.map((step, i) => {
            const state = at < 0 ? "todo" : i < at ? "done" : i === at ? "now" : "todo";
            return (
              <li key={step} className={`gh-step gh-step--${state}`}>
                <span className="gh-step-icon" aria-hidden>
                  {state === "done" ? (
                    <Check size={12} strokeWidth={2.5} />
                  ) : state === "now" ? (
                    <LoaderCircle size={12} strokeWidth={2.5} className="gh-spin" />
                  ) : (
                    <Circle size={8} strokeWidth={2.5} />
                  )}
                </span>
                <span>{t(`prog.step.${step}`)}</span>
                {state === "now" && p?.percent !== null && p?.percent !== undefined ? (
                  <span className="gh-step-pct">{Math.round(p.percent)}%</span>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : null}
      <div
        className={`gh-bar${measuring ? " gh-bar--working" : ""}`}
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={overall === null ? undefined : Math.round(overall)}
      >
        <div className="gh-bar-fill" style={{ width: `${overall ?? 0}%` }} />
      </div>
      <div className="gh-progress-foot">
        <span>
          {steps.length === 1 && p?.step ? `${t(`prog.step.${p.step}`)}: ` : ""}
          {detail || (p?.step ? t(`prog.hint.${p.step}`) : t("prog.hint.start"))}
        </span>
        {overall !== null ? <span className="gh-progress-pct">{Math.round(overall)}%</span> : null}
      </div>
    </div>
  );
}
