import { MessageSquare, Pencil, Play, RotateCw, Square, X } from "lucide-react";
import { useT } from "../../core/i18n";
import { Markdown } from "../../ui/Markdown";
import { agentInitial, agentStatus, statusBadgeStyle } from "./helpers";
import { STR } from "./strings";
import type { Agent, AgentAction } from "./types";

interface AgentDetailPanelProps {
  agent: Agent;
  busy: boolean;
  onClose: () => void;
  onAction: (action: AgentAction) => void;
  onChat: () => void;
  onEdit: () => void;
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.04em" }}>
        {label}
      </div>
      <div style={{ fontSize: 18, fontWeight: 700, marginTop: 2, lineHeight: 1.1 }}>{value}</div>
    </div>
  );
}

// Expanded panel shown under the agents grid when a card is selected. Mirrors
// the legacy "select an agent" affordance (the old UI drove theme/chat target
// from the selection); here we surface the agent's full description, runtime
// status, capability counts, channels and the boot error in one place.
export function AgentDetailPanel({ agent, busy, onClose, onAction, onChat, onEdit }: AgentDetailPanelProps) {
  const t = useT(STR);
  const color = agent.color || "var(--accent)";
  const status = agentStatus(agent);
  const channels = agent.channels ?? [];

  return (
    <div
      className="card lc-ov-detail"
      style={{
        marginTop: 12,
        padding: 0,
        overflow: "hidden",
        display: "flex",
        borderColor: "var(--border-strong)",
      }}
    >
      <div style={{ width: 4, background: color, flex: "0 0 auto" }} aria-hidden />
      <div style={{ padding: 18, flex: 1, minWidth: 0 }}>
        {/* head */}
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
          <div className="row" style={{ gap: 12, minWidth: 0 }}>
            <div
              style={{
                width: 44,
                height: 44,
                borderRadius: "50%",
                flex: "0 0 auto",
                display: "grid",
                placeItems: "center",
                fontSize: 18,
                fontWeight: 700,
                color: agent.color ? color : "var(--text-2)",
                background: agent.color ? `${color}22` : "var(--surface-2)",
                boxShadow: `0 0 0 1px ${color}`,
                overflow: "hidden",
              }}
            >
              {agent.avatar_url ? (
                <img src={agent.avatar_url} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
              ) : (
                <span>{agentInitial(agent)}</span>
              )}
            </div>
            <div style={{ minWidth: 0 }}>
              <div className="row" style={{ gap: 8 }}>
                <strong style={{ fontSize: 16 }}>{agent.name}</strong>
                <span
                  className="badge"
                  style={statusBadgeStyle(status)}
                  title={status === "error" ? agent.error : undefined}
                >
                  {t(`status.${status}`)}
                </span>
              </div>
              <div className="muted" style={{ fontSize: 12, fontFamily: "var(--font-mono)", marginTop: 2 }}>
                {agent.model || "—"}
              </div>
            </div>
          </div>
          <button
            className="btn ghost"
            title={t("agent.close")}
            aria-label={t("agent.closeDetails")}
            onClick={onClose}
            style={{ padding: "4px 9px", lineHeight: 1, display: "inline-flex", alignItems: "center" }}
          >
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>

        {/* boot error */}
        {status === "error" && agent.error && (
          <div
            style={{
              marginTop: 12,
              padding: "9px 12px",
              borderRadius: "var(--r-sm)",
              background: "var(--danger-soft)",
              border: "1px solid color-mix(in srgb, var(--danger) 40%, transparent)",
              color: "var(--danger)",
              fontSize: 12.5,
              fontFamily: "var(--font-mono)",
              wordBreak: "break-word",
            }}
          >
            {agent.error}
          </div>
        )}

        {/* description (markdown) */}
        <div style={{ marginTop: 12, color: "var(--text-2)", fontSize: 13 }}>
          {agent.description ? (
            <Markdown text={agent.description} />
          ) : (
            <span className="muted">{t("agent.noDescription")}</span>
          )}
        </div>

        {/* capability stats */}
        <div
          style={{
            display: "grid",
            gap: 12,
            marginTop: 16,
            gridTemplateColumns: "repeat(auto-fill,minmax(96px,1fr))",
          }}
        >
          <Stat label={t("cap.subagents")} value={agent.subagent_count ?? 0} />
          <Stat label={t("cap.tools")} value={agent.tool_count ?? 0} />
          <Stat label={t("cap.skills")} value={agent.skill_count ?? 0} />
          <Stat label={t("cap.mcps")} value={agent.mcp_count ?? 0} />
          <Stat label={t("cap.channels")} value={channels.length} />
        </div>

        {/* channel chips */}
        {channels.length > 0 && (
          <div style={{ marginTop: 14 }}>
            <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 6 }}>
              {t("agent.connectedChannels")}
            </div>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {channels.map((c) => (
                <span key={c} className="badge">
                  {c}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* actions */}
        <div className="row" style={{ gap: 8, marginTop: 18, flexWrap: "wrap" }}>
          {agent.running ? (
            <>
              <button
                className="btn primary"
                disabled={busy}
                onClick={onChat}
                style={{ padding: "7px 14px", display: "inline-flex", alignItems: "center", gap: 6 }}
              >
                <MessageSquare size={15} strokeWidth={1.75} /> {t("agent.chat")}
              </button>
              <button
                className="btn"
                disabled={busy}
                onClick={() => onAction("restart")}
                style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
              >
                {busy ? <span className="spinner" /> : <RotateCw size={15} strokeWidth={1.75} />} {t("agent.restart")}
              </button>
              <button
                className="btn danger"
                disabled={busy}
                onClick={() => onAction("stop")}
                style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
              >
                {busy ? <span className="spinner" /> : <Square size={15} strokeWidth={1.75} />} {t("agent.stop")}
              </button>
            </>
          ) : (
            <button
              className="btn primary"
              disabled={busy}
              onClick={() => onAction("start")}
              style={{ padding: "7px 14px", display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              {busy ? (
                <span className="spinner" />
              ) : (
                <>
                  <Play size={15} strokeWidth={1.75} /> {t("agent.start")}
                </>
              )}
            </button>
          )}
          <button className="btn" onClick={onEdit} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Pencil size={15} strokeWidth={1.75} /> {t("agent.edit")}
          </button>
        </div>
      </div>
    </div>
  );
}
