import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { errMessage, formatTs, statusColor } from "./helpers";
import { Drawer, Empty } from "./ui";
import { STR } from "./strings";
import type { CognitionAction, CognitionActionsResponse } from "./types";

// Known action → label translation key.
const LABEL_KEY: Record<string, string> = {
  optimize_skills: "activity.label.optimizeSkills",
  optimize_workspace: "activity.label.optimizeWorkspace",
  undo_optimization: "activity.label.undoOptimization",
  generate_all: "activity.label.generateAll",
};

// The audit log of what Cortex did (autonomously or on demand) for this agent.
// Mirrors showCognitionActivity(). When `phaseFilter` is set, only that phase's
// runs are shown (the per-phase "Log" button); `titleOverride` retitles the drawer.
export function ActivityDrawer({
  agent,
  onClose,
  phaseFilter,
  titleOverride,
}: {
  agent: string;
  onClose: () => void;
  phaseFilter?: string;
  titleOverride?: string;
}) {
  const t = useT(STR);
  const [actions, setActions] = useState<CognitionAction[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<CognitionActionsResponse>(`/api/cognition/actions?agent=${encodeURIComponent(agent)}&limit=100`)
      .then((res) => {
        let list = res.actions || [];
        if (phaseFilter) {
          list = list.filter(
            (a) =>
              ((a.detail || {}) as Record<string, unknown>).phase === phaseFilter ||
              a.action === `cortex_${phaseFilter}`,
          );
        }
        setActions(list);
      })
      .catch((e) => setError(errMessage(e, t("activity.toast.loadFailed"))));
  }, [agent, t, phaseFilter]);

  function row(a: CognitionAction, i: number) {
    const d = (a.detail || {}) as Record<string, unknown>;
    const isCortex = String(a.action || "").startsWith("cortex_");
    const title = isCortex
      ? `${(d.label as string) || a.action.replace("cortex_", "")}${d.source === "autonomous" ? t("activity.auto") : ""}`
      : LABEL_KEY[a.action]
        ? t(LABEL_KEY[a.action])
        : a.action;
    const status = d.status as string | undefined;
    const vaultLocked = d.code === "vault_locked";
    const bits: string[] = [];
    if (d.count)
      bits.push(t((d.count as number) === 1 ? "activity.items" : "activity.items.plural", { count: String(d.count) }));
    if (d.files_changed != null) bits.push(t("activity.changed", { count: String(d.files_changed) }));
    if (d.files_removed) bits.push(t("activity.removed", { count: String(d.files_removed) }));
    if (d.lines_delta)
      bits.push(t("activity.lines", { delta: `${(d.lines_delta as number) > 0 ? "+" : ""}${d.lines_delta}` }));
    return (
      <div key={i} style={{ padding: "10px 0", borderBottom: "1px solid var(--border)" }}>
        <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
          <span style={{ fontWeight: 600, fontSize: 13 }}>
            {title}
            {status ? (
              <span style={{ color: statusColor(status), fontWeight: 600, marginLeft: 6, fontSize: 12 }}>{status}</span>
            ) : null}
          </span>
          <span className="muted" style={{ fontSize: 11 }} title={a.ts}>
            {formatTs(a.ts)}
          </span>
        </div>
        {a.summary ? (
          <div
            className="muted"
            style={{
              fontSize: 13,
              marginTop: 2,
              maxHeight: 120,
              overflowY: "auto",
              color: status === "error" ? "var(--danger)" : undefined,
              whiteSpace: "pre-wrap",
            }}
          >
            {a.summary}
          </div>
        ) : null}
        {bits.length ? (
          <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
            {bits.join(" · ")}
          </div>
        ) : null}
        {vaultLocked ? (
          <div style={{ marginTop: 4 }}>
            <Link to="/vault" className="btn primary" style={{ padding: "2px 9px", fontSize: 11 }}>
              {t("activity.unlockVault")}
            </Link>
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <Drawer
      title={titleOverride || t("activity.title")}
      sub={agent}
      onClose={onClose}
      footer={
        <button className="btn primary" onClick={onClose}>
          {t("activity.close")}
        </button>
      }
    >
      {error ? (
        <Empty title={t("activity.loadFailedTitle")} sub={error} />
      ) : actions === null ? (
        <div className="spinner" />
      ) : actions.length === 0 ? (
        <Empty
          title={phaseFilter ? t("activity.phase.empty.title") : t("activity.empty.title")}
          sub={phaseFilter ? t("activity.phase.empty.sub") : t("activity.empty.sub")}
        />
      ) : (
        actions.map(row)
      )}
    </Drawer>
  );
}
