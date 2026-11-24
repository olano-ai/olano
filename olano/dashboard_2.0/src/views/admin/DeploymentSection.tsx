import { useCallback, useEffect, useState } from "react";
import { api } from "../../core/api";
import { refreshCapabilities } from "../../core/capabilities";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { STR } from "./strings";

interface ResourceRow {
  key: string;
  label: string;
  limit: number | null;
  current: number | null;
}
interface UsageRow {
  key: string;
  label: string;
  limit: number | null;
  window: string;
  current: number | null;
  unit: string;
}
export interface DeploymentInfo {
  posture: "open" | "enforced" | "quarantine";
  source: string;
  mode: string | null;
  deployment_id: string | null;
  plan: string | null;
  policy_revision: number | null;
  issued_at: number | null;
  expires_at: number | null;
  last_refresh: number | null;
  error: string | null;
  resources: ResourceRow[];
  usage: UsageRow[];
  features: Record<string, boolean>;
  tabs: Record<string, boolean>;
  credits_left: number | null;
  olano_model_credits: Record<string, number>;
  blocked_agents: Record<string, string>;
}

function fmtEpoch(epoch: number | null): string {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleString();
}

function fmtAmount(row: UsageRow, value: number | null): string {
  if (value === null) return "∞";
  if (row.unit === "micro_usd") return `$${(value / 1_000_000).toFixed(2)}`;
  return value.toLocaleString();
}

/** Admin card: the deployment's entitlement posture, limits vs usage, and
 * feature/tab toggles. Read-only — the settings themselves are signed by the
 * olano control plane and cannot be edited from here by design. */
export function DeploymentSection() {
  const t = useT(STR);
  const toast = useToast();
  const [info, setInfo] = useState<DeploymentInfo | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(() => {
    api
      .get<DeploymentInfo>("/api/system/deployment")
      .then(setInfo)
      .catch(() => setInfo(null));
  }, []);
  useEffect(load, [load]);

  const forceRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await api.post("/api/system/deployment/refresh", {});
      refreshCapabilities();
      load();
      toast.push(t("deployment.refreshed"));
    } catch {
      toast.push(t("deployment.refreshFailed"), "error");
    } finally {
      setRefreshing(false);
    }
  }, [load, t, toast]);

  if (!info) return null;

  const postureStyle =
    info.posture === "quarantine"
      ? { background: "var(--danger, #b91c1c)", color: "#fff" }
      : info.posture === "enforced"
        ? { background: "var(--accent, #2563eb)", color: "var(--accent-fg, #fff)" }
        : undefined;
  const disabledFeatures = Object.entries(info.features).filter(([, v]) => !v);
  const hiddenTabs = Object.entries(info.tabs).filter(([, v]) => !v);
  const blocked = Object.entries(info.blocked_agents || {});

  return (
    <section className="card" style={{ marginTop: 24 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 15 }}>{t("deployment.title")}</h2>
          <p className="muted" style={{ margin: "2px 0 0", fontSize: 12.5 }}>
            {t("deployment.subtitle")}
          </p>
        </div>
        <button className="btn" onClick={forceRefresh} disabled={refreshing}>
          {refreshing ? t("deployment.refreshing") : t("deployment.refresh")}
        </button>
      </div>

      <div className="row" style={{ gap: 8, marginTop: 12, flexWrap: "wrap" }}>
        <span className="badge" style={postureStyle} data-testid="deployment-posture">
          {t(`deployment.posture.${info.posture}`)}
        </span>
        <span className="badge">{t("deployment.source")}: {info.source}</span>
        {info.plan && <span className="badge">{t("deployment.plan")}: {info.plan}</span>}
        {info.policy_revision !== null && (
          <span className="badge">rev {info.policy_revision}</span>
        )}
      </div>
      {info.posture === "quarantine" && (
        <p className="error-text" style={{ marginTop: 8, fontSize: 13 }}>
          {t("deployment.quarantineHint")} {info.error ? `(${info.error})` : ""}
        </p>
      )}

      {info.posture !== "open" && (
        <>
          <div className="row" style={{ gap: 16, marginTop: 10, flexWrap: "wrap", fontSize: 12.5 }}>
            <span className="muted">{t("deployment.expires")}: {fmtEpoch(info.expires_at)}</span>
            <span className="muted">{t("deployment.lastRefresh")}: {fmtEpoch(info.last_refresh)}</span>
            {info.credits_left !== null && (
              <span className="muted">{t("deployment.credits")}: {info.credits_left}</span>
            )}
          </div>

          <table className="table" style={{ marginTop: 12 }}>
            <thead>
              <tr>
                <th>{t("deployment.col.limit")}</th>
                <th style={{ textAlign: "right" }}>{t("deployment.col.current")}</th>
                <th style={{ textAlign: "right" }}>{t("deployment.col.max")}</th>
                <th>{t("deployment.col.window")}</th>
              </tr>
            </thead>
            <tbody>
              {info.resources.map((r) => (
                <tr key={r.key}>
                  <td>{r.label}</td>
                  <td style={{ textAlign: "right" }}>{r.current ?? "—"}</td>
                  <td style={{ textAlign: "right" }}>{r.limit === null ? "∞" : r.limit}</td>
                  <td className="muted">—</td>
                </tr>
              ))}
              {info.usage.map((u) => (
                <tr key={u.key}>
                  <td>{u.label}</td>
                  <td style={{ textAlign: "right" }}>{u.current === null ? "—" : fmtAmount(u, u.current)}</td>
                  <td style={{ textAlign: "right" }}>{fmtAmount(u, u.limit)}</td>
                  <td className="muted">{u.limit === null ? "—" : u.window}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {disabledFeatures.length > 0 && (
            <p style={{ marginTop: 10, fontSize: 12.5 }}>
              <strong>{t("deployment.disabledFeatures")}:</strong>{" "}
              {disabledFeatures.map(([k]) => k).join(", ")}
            </p>
          )}
          {hiddenTabs.length > 0 && (
            <p style={{ marginTop: 4, fontSize: 12.5 }}>
              <strong>{t("deployment.hiddenTabs")}:</strong> {hiddenTabs.map(([k]) => k).join(", ")}
            </p>
          )}
          {blocked.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <strong style={{ fontSize: 12.5 }}>{t("deployment.blockedAgents")}</strong>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 12.5 }}>
                {blocked.map(([name, reason]) => (
                  <li key={name}>
                    <code>{name}</code> — <span className="muted">{reason}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
      {info.posture === "open" && (
        <p className="muted" style={{ marginTop: 10, fontSize: 12.5 }}>
          {t("deployment.openHint")}
        </p>
      )}
    </section>
  );
}
