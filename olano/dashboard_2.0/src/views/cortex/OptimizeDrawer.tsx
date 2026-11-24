import { useState } from "react";
import { Link } from "react-router-dom";
import { Check } from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { errMessage } from "./helpers";
import { Drawer, Empty, Markdown } from "./ui";
import { STR } from "./strings";
import type { OptimizeKind, OptimizeRun } from "./useOptimizeRun";

export type { OptimizeKind } from "./useOptimizeRun";

// title/working are translation keys, resolved at render time.
const META: Record<OptimizeKind, { titleKey: string; workingKey: string }> = {
  workspace: { titleKey: "opt.workspace.title", workingKey: "opt.workspace.working" },
  skills: { titleKey: "opt.skills.title", workingKey: "opt.skills.working" },
};

// Pure VIEW of an optimization run (owned by useOptimizeRun on the page). Renders
// the live spinner while running and, once done, the concrete outcome: changed
// files with before→after line counts, the agent's narrative, and an Undo button.
// The run survives this drawer being closed, so re-opening shows live/final state.
export function OptimizeDrawer({
  kind,
  agent,
  run,
  onClose,
  onUndone,
  onViewLog,
}: {
  kind: OptimizeKind;
  agent: string;
  // The run state from useOptimizeRun. Null when the in-flight run was started in
  // the background (scheduled) rather than from this page — then we just show the
  // working spinner until it clears.
  run: OptimizeRun | null;
  onClose: () => void;
  onUndone: () => void;
  // Opens the run's step log (its Cortex job), when there is one.
  onViewLog?: () => void;
}) {
  const toast = useToast();
  const t = useT(STR);
  const meta = META[kind];
  const result = run?.result ?? null;
  const error = run?.error ?? null;
  const [undone, setUndone] = useState(false);
  const [busy, setBusy] = useState(false);

  async function undo() {
    if (!result?.backup_job) return;
    setBusy(true);
    try {
      await api.post("/api/cognition/restore-backup", { agent, backup_job: result.backup_job });
      toast.push(t("opt.toast.reverted"));
      setUndone(true);
      onUndone();
    } catch (e) {
      toast.push(t("opt.toast.undoFailed", { error: errMessage(e, t("opt.toast.restoreError")) }), "error");
    } finally {
      setBusy(false);
    }
  }

  const changes = result?.changes ?? null;
  const changedFiles = changes ? (changes.files || []).filter((f) => f.status !== "unchanged") : [];

  let headline = "";
  if (result) {
    if (result.status === "empty") headline = t("opt.headline.nothing");
    else if (changedFiles.length && changes) {
      const parts: string[] = [];
      if (changes.files_changed)
        parts.push(
          t(changes.files_changed === 1 ? "opt.headline.filesImproved" : "opt.headline.filesImproved.plural", {
            count: String(changes.files_changed),
          }),
        );
      if (changes.files_removed) parts.push(t("opt.headline.merged", { count: String(changes.files_removed) }));
      if ((changes.lines_delta ?? 0) < 0)
        parts.push(t("opt.headline.fewerLines", { count: String(Math.abs(changes.lines_delta!)) }));
      else if ((changes.lines_delta ?? 0) > 0)
        parts.push(t("opt.headline.moreLines", { count: String(changes.lines_delta) }));
      headline = parts.join(" · ") || t("opt.headline.changes");
    } else headline = t("opt.headline.clean");
  }

  return (
    <Drawer
      title={t(meta.titleKey)}
      sub={agent}
      onClose={onClose}
      footer={
        <>
          {onViewLog ? (
            <button className="btn" onClick={onViewLog} style={{ marginRight: "auto" }}>
              {t("items.job.viewLog")}
            </button>
          ) : null}
          {result ? (
            <>
              {result.backup_job && changedFiles.length && !undone ? (
                <button className="btn danger" disabled={busy} onClick={() => void undo()}>
                  {busy ? t("opt.reverting") : t("opt.undo")}
                </button>
              ) : null}
              <button className="btn primary" onClick={onClose}>
                {t("opt.done")}
              </button>
            </>
          ) : error ? (
            <button className="btn primary" onClick={onClose}>
              {t("opt.close")}
            </button>
          ) : (
            <button className="btn primary" onClick={onClose}>
              {t("jobs.hide")}
            </button>
          )}
        </>
      }
    >
      {error ? (
        <Empty title={t("opt.failedTitle")} sub={error} />
      ) : !result ? (
        <div style={{ textAlign: "center", padding: "32px 12px", display: "grid", gap: 12, justifyItems: "center" }}>
          <div className="spinner" />
          <div style={{ fontWeight: 600 }}>{t(meta.workingKey)}</div>
          <div className="muted" style={{ fontSize: 13 }}>
            {t("opt.deepNote")}
          </div>
        </div>
      ) : (
        <>
          {undone ? (
            <div className="card" style={{ padding: 12, marginBottom: 12, borderLeft: "3px solid var(--warn)" }}>
              {t("opt.revertedBanner")}
            </div>
          ) : null}
          <div className="row" style={{ gap: 10, marginBottom: 4 }}>
            <span style={{ color: "var(--accent)", display: "inline-flex" }}>
              <Check size={16} strokeWidth={1.75} />
            </span>
            <div style={{ fontWeight: 600, fontSize: 15 }}>{headline}</div>
          </div>
          {changes && (changes.lines_before || changes.lines_after) ? (
            <div className="muted" style={{ fontSize: 13 }}>
              {t("opt.totalLines", { before: String(changes.lines_before), after: String(changes.lines_after) })}
            </div>
          ) : null}

          {changedFiles.length ? (
            <div style={{ marginTop: 18 }}>
              <div className="muted" style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
                {t("opt.filesChanged")}
              </div>
              {changedFiles.map((f) => {
                const color = f.status === "removed" ? "var(--danger)" : "var(--accent)";
                return (
                  <div
                    key={f.path}
                    className="row"
                    style={{ gap: 8, padding: "7px 0", borderBottom: "1px solid var(--border)" }}
                  >
                    <span style={{ width: 6, height: 6, borderRadius: "50%", background: color, flexShrink: 0 }} />
                    <Link
                      to={`/files?path=${encodeURIComponent(f.path)}&agent=${encodeURIComponent(agent)}`}
                      style={{
                        flex: 1,
                        minWidth: 0,
                        fontFamily: "var(--font-mono)",
                        fontSize: 12.5,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {f.path}
                    </Link>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {f.status === "removed"
                        ? t("opt.file.removed", { before: String(f.lines_before) })
                        : t("opt.file.lines", { before: String(f.lines_before), after: String(f.lines_after) })}
                    </span>
                    {f.lines_delta < 0 ? (
                      <span style={{ color: "var(--success)", fontSize: 12 }}>{f.lines_delta}</span>
                    ) : f.lines_delta > 0 ? (
                      <span style={{ color: "var(--warn)", fontSize: 12 }}>+{f.lines_delta}</span>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="muted" style={{ fontSize: 13, marginTop: 12 }}>
              {result.status === "empty"
                ? result.summary || t("opt.noItemsFound")
                : t("opt.foundNothing")}
            </div>
          )}

          {result.summary ? (
            <div style={{ marginTop: 18 }}>
              <div className="muted" style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
                {t("opt.whatAgentDid")}
              </div>
              <Markdown source={result.summary} />
            </div>
          ) : null}
        </>
      )}
    </Drawer>
  );
}
