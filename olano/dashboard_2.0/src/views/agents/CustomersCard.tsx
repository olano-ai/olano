// The Multi-user tab's end-user list: everyone who has talked to this agent
// (one private folder each, customers/<platform>-<id>/), with block/unblock.
// The dashboard twin of `olano customers list|block|unblock`, over
// GET /api/agents/{name}/customers and PUT …/customers/{key}/blocked
// (runtime/routes/customers.py). Blocking is an operator switch: no agent
// conversation can set or lift it, so it lives here and not in a tool.

import { useCallback, useEffect, useState } from "react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { STR } from "./strings";

interface CustomerRow {
  key: string;
  platform: string;
  user_id: string;
  blocked: boolean;
  muted: boolean;
  blocked_at?: string | null;
  muted_at?: string | null;
  last_active?: number | null;
}

function fmtWhen(epochSeconds?: number | null): string {
  if (!epochSeconds) return "";
  try {
    return new Date(epochSeconds * 1000).toLocaleString();
  } catch {
    return "";
  }
}

export function CustomersCard({ agent }: { agent: string }) {
  const t = useT(STR);
  const toast = useToast();
  const [rows, setRows] = useState<CustomerRow[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string>("");

  const load = useCallback(() => {
    setError("");
    api
      .get<{ customers: CustomerRow[] }>(`/api/agents/${encodeURIComponent(agent)}/customers`)
      .then((d) => setRows(d.customers || []))
      .catch((e: unknown) => {
        setRows([]);
        setError(e instanceof Error ? e.message : String(e));
      });
  }, [agent]);

  useEffect(() => {
    load();
  }, [load]);

  async function setBlocked(row: CustomerRow, blocked: boolean) {
    setBusy(row.key);
    try {
      const res = await api.put<{ customer: CustomerRow | null }>(
        `/api/agents/${encodeURIComponent(agent)}/customers/${encodeURIComponent(row.key)}/blocked`,
        { blocked },
      );
      setRows((prev) =>
        (prev || []).map((r) => (r.key === row.key ? res.customer || { ...r, blocked } : r)),
      );
      toast.push(t(blocked ? "mu.custBlockedToast" : "mu.custUnblockedToast", { key: row.key }), "success");
    } catch (e) {
      toast.push(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="card" style={{ padding: 16, marginBottom: 14 }} data-testid="mu-customers">
      <div className="row" style={{ justifyContent: "space-between", gap: 8, marginBottom: 6 }}>
        <strong style={{ fontSize: 13 }}>{t("mu.custTitle")}</strong>
        <button className="btn ghost" onClick={load} disabled={rows === null}>
          {t("mu.custRefresh")}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 11, lineHeight: 1.5, margin: "0 0 10px" }}>
        {t("mu.custHint")}
      </p>
      {rows === null ? (
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          {t("mu.custLoading")}
        </p>
      ) : error ? (
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          {t("mu.custError", { error })}
        </p>
      ) : rows.length === 0 ? (
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          {t("mu.custEmpty")}
        </p>
      ) : (
        <div>
          {rows.map((r) => (
            <div
              key={r.key}
              className="row"
              style={{
                justifyContent: "space-between",
                gap: 10,
                padding: "8px 0",
                borderBottom: "1px solid var(--border)",
                flexWrap: "wrap",
              }}
            >
              <div style={{ minWidth: 0, flex: "1 1 200px" }}>
                <div style={{ fontSize: 13, overflowWrap: "anywhere" }}>
                  {r.user_id}
                  {r.platform ? (
                    <span className="muted" style={{ fontSize: 11, marginLeft: 6 }}>
                      {r.platform}
                    </span>
                  ) : null}
                  {r.blocked ? (
                    <span className="pill" style={{ marginLeft: 6, fontSize: 10 }}>
                      {t("mu.custBlocked")}
                    </span>
                  ) : null}
                  {r.muted ? (
                    <span className="pill" style={{ marginLeft: 6, fontSize: 10 }} title={t("mu.custMutedTitle")}>
                      {t("mu.custMuted")}
                    </span>
                  ) : null}
                </div>
                <div className="muted" style={{ fontSize: 11, marginTop: 1, overflowWrap: "anywhere" }}>
                  <span style={{ fontFamily: "var(--font-mono)" }}>{r.key}</span>
                  {r.last_active ? ` · ${t("mu.custLastActive", { when: fmtWhen(r.last_active) })}` : ""}
                </div>
              </div>
              <button
                className={r.blocked ? "btn" : "btn ghost"}
                disabled={busy === r.key}
                onClick={() => void setBlocked(r, !r.blocked)}
              >
                {r.blocked ? t("mu.custUnblock") : t("mu.custBlock")}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
