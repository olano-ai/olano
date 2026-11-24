import { useEffect, useState } from "react";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import type { AgentConfig } from "../agents/types";
import { KvRow, Toggle } from "../agents/Toggle";
import { EndpointsCard, RecentCallsCard, StatusBadge, TokensCard, useExternalStatus } from "./ExternalAccessPanel";
import { STR } from "./strings";

/**
 * Agent tab: let systems outside the deployment reach THIS agent (inbound MCP /
 * A2A). Deliberately separate from the MCP tab — that one connects the agent to
 * other servers; this one publishes the agent — and it says so at the top.
 */
export function ExternalAccessTab({
  cfg,
  agentName,
  savePatch,
}: {
  cfg: AgentConfig;
  agentName: string;
  savePatch: (patch: Partial<AgentConfig>, successMsg?: string) => Promise<boolean>;
}) {
  const t = useT(STR);
  const toast = useToast();
  const { user } = useAuth();
  const isAdmin = !!user?.is_admin;
  const ext = cfg.external_access ?? { mcp: false, a2a: false, description: "" };
  const [mcp, setMcp] = useState(!!ext.mcp);
  const [a2a, setA2a] = useState(!!ext.a2a);
  const [description, setDescription] = useState(ext.description ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const { status, forbidden, reload } = useExternalStatus();

  useEffect(() => {
    setMcp(!!ext.mcp);
    setA2a(!!ext.a2a);
    setDescription(ext.description ?? "");
  }, [ext.mcp, ext.a2a, ext.description]);

  const dirty = mcp !== !!ext.mcp || a2a !== !!ext.a2a || description !== (ext.description ?? "");
  const needsDescription = (mcp || a2a) && !description.trim();

  async function save() {
    if (needsDescription) {
      setError(t("ext.description.required"));
      return;
    }
    setSaving(true);
    setError("");
    const ok = await savePatch({ external_access: { mcp, a2a, description: description.trim() } }, t("ext.saved"));
    setSaving(false);
    if (ok) reload();
    else toast.push(t("ext.mint.err"), "error");
  }

  return (
    <div className="ext-tab" data-testid="external-access-tab">
      <div className="card" style={{ padding: 14, borderLeft: "3px solid var(--accent, #6b8afd)" }}>
        <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5 }}>{t("ext.intro")}</p>
        <p className="muted" style={{ margin: "8px 0 0", fontSize: 12.5, lineHeight: 1.45 }}>
          <strong>{t("ext.notMcpTab").split(".")[0]}.</strong> {t("ext.notMcpTab").split(".").slice(1).join(".").trim()}
        </p>
      </div>

      <div className="card" style={{ padding: 14, marginTop: 12 }}>
        <KvRow label={t("ext.mcp")} hint={t("ext.mcp.hint")}>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {status && (
              <span className="muted" style={{ fontSize: 11 }} title={t("ext.live.hint")}>
                {t("ext.live")}
              </span>
            )}
            {status && (
              <StatusBadge
                serving={status.serving && mcp}
                enabled={status.enabled}
                https={status.https}
                requireHttps={status.require_https}
                originKnown={status.public_origin_known}
              />
            )}
            <Toggle checked={mcp} onChange={setMcp} disabled={!isAdmin} ariaLabel={t("ext.mcp")} />
          </div>
        </KvRow>
        {mcp && status && !status.enabled && (
          <p style={{ color: "var(--danger)", fontSize: 12.5 }}>{t("ext.homeOff.mcp")}</p>
        )}
        <KvRow label={t("ext.a2a")} hint={t("ext.a2a.hint")}>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {status?.a2a && (
              <span className="muted" style={{ fontSize: 11 }} title={t("ext.live.hint")}>
                {t("ext.live")}
              </span>
            )}
            {status?.a2a && (
              <StatusBadge
                serving={status.a2a.serving && a2a}
                enabled={status.a2a.enabled}
                https={status.https}
                requireHttps={status.require_https}
                originKnown={status.public_origin_known}
              />
            )}
            <Toggle checked={a2a} onChange={setA2a} disabled={!isAdmin} ariaLabel={t("ext.a2a")} />
          </div>
        </KvRow>
        {a2a && status?.a2a && !status.a2a.enabled && (
          <p style={{ color: "var(--danger)", fontSize: 12.5 }}>{t("ext.homeOff.a2a")}</p>
        )}
        <div className="field" style={{ marginTop: 12 }}>
          <label style={{ fontWeight: 600, display: "block", marginBottom: 4 }}>
            {t("ext.description")}
            {(mcp || a2a) && <span style={{ color: "var(--danger)" }}> *</span>}
          </label>
          <textarea
            className="input"
            rows={4}
            value={description}
            disabled={!isAdmin}
            onChange={(e) => setDescription(e.target.value)}
            maxLength={2000}
            style={{ borderColor: needsDescription ? "var(--danger)" : undefined }}
            data-testid="ext-description"
          />
          <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.45, marginTop: 4 }}>{t("ext.description.hint")}</div>
        </div>
        {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
        {!isAdmin && <p className="muted" style={{ fontSize: 12 }}>{t("ext.memberNote")}</p>}
        {isAdmin && (
          <div style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "flex-end" }}>
            <span className="muted" style={{ fontSize: 12 }}>{t("ext.restartNote")}</span>
            <button className="btn primary" type="button" disabled={!dirty || saving} onClick={save}>
              {t("ext.save")}
            </button>
          </div>
        )}
      </div>

      {!forbidden && status && (mcp || a2a || ext.mcp || ext.a2a) && (
        <EndpointsCard status={status} agentName={agentName} />
      )}
      {!forbidden && isAdmin && <TokensCard agentName={agentName} status={status} onChange={reload} />}
      {!forbidden && <RecentCallsCard agentName={agentName} />}
    </div>
  );
}
