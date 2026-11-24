import { Link } from "react-router-dom";
import { Check, Circle, Loader, Minus, X, type LucideIcon } from "lucide-react";
import { useT } from "../../core/i18n";
import { Drawer } from "./ui";
import { STR } from "./strings";
import { THINK_PHASES, type PhaseState, type ThinkState } from "./useThinkRun";
import { ErrorRemediation } from "../chat/ErrorRemediation";

function VaultUnlockLink() {
  const t = useT(STR);
  return (
    <Link to="/vault" className="btn primary" style={{ marginLeft: 6, padding: "2px 9px", fontSize: 11 }}>
      {t("think.unlockVault")}
    </Link>
  );
}

const ICON: Record<PhaseState, LucideIcon> = {
  pending: Circle,
  running: Loader,
  done: Check,
  error: X,
  skipped: Minus,
};
const ICON_COLOR: Record<PhaseState, string> = {
  pending: "var(--text-4)",
  running: "var(--accent)",
  done: "var(--success)",
  error: "var(--danger)",
  skipped: "var(--text-3)",
};

// Presentational live-status panel for a "Think now" run. The run itself lives in
// useThinkRun (held by the page) so it survives closing/re-opening this drawer.
export function ThinkDrawer({
  state,
  onClose,
  onViewLog,
}: {
  state: ThinkState;
  onClose: () => void;
  // Opens the run's step log (the Think-now Cortex job), when there is one.
  onViewLog?: () => void;
}) {
  const t = useT(STR);
  return (
    <Drawer title={t("think.title")} sub={state.agent} onClose={onClose} footer={
        <>
          {onViewLog ? (
            <button className="btn" onClick={onViewLog} style={{ marginRight: "auto" }}>
              {t("items.job.viewLog")}
            </button>
          ) : null}
          <button className="btn primary" onClick={onClose}>
            {t("think.close")}
          </button>
        </>
      }>
      <div className="muted" style={{ fontSize: 13, marginBottom: 10 }}>
        {t("think.intro")}
      </div>
      {THINK_PHASES.map((p) => {
        const r = state.phases[p.id];
        return (
          <div
            key={p.id}
            className="row"
            style={{ gap: 10, padding: "9px 0", borderBottom: "1px solid var(--border)", alignItems: "flex-start" }}
          >
            <span
              style={{ flexShrink: 0, color: ICON_COLOR[r.state], width: 16, display: "inline-flex", justifyContent: "center", paddingTop: 1 }}
            >
              {(() => {
                const Glyph = ICON[r.state];
                return <Glyph size={14} strokeWidth={1.75} />;
              })()}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 13 }}>{t(p.labelKey)}</div>
              <div
                style={{ fontSize: 12, color: r.state === "error" ? "var(--danger)" : "var(--text-3)", wordBreak: "break-word" }}
              >
                {r.statusText}
                {r.code === "vault_locked" ? <VaultUnlockLink /> : null}
              </div>
              {/* One-click fix (reconnect / set key / top up) — vault keeps its own link above. */}
              {r.remediation && r.code !== "vault_locked" ? <ErrorRemediation remediation={r.remediation} /> : null}
            </div>
            <span style={{ fontSize: 12, color: ICON_COLOR[r.state], whiteSpace: "nowrap" }}>{r.badge}</span>
          </div>
        );
      })}
      <div style={{ marginTop: 14 }}>
        {state.error ? (
          <div style={{ color: "var(--danger)", fontSize: 13 }}>
            {t("think.failed", { error: state.error })}
            {state.errorCode === "vault_locked" ? <VaultUnlockLink /> : null}
            {state.errorRemediation && state.errorCode !== "vault_locked" ? (
              <ErrorRemediation remediation={state.errorRemediation} />
            ) : null}
          </div>
        ) : state.done ? (
          <div style={{ fontWeight: 600 }}>{t("think.done", { total: String(state.total) })}</div>
        ) : (
          <div className="muted" style={{ fontSize: 13 }}>
            {t("think.working")}
          </div>
        )}
      </div>
    </Drawer>
  );
}
