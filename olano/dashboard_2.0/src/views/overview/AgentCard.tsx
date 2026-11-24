import type { CSSProperties, ReactNode } from "react";
import { MessageSquare, Pencil, Play, RotateCw, Square } from "lucide-react";
import { useT } from "../../core/i18n";
import { agentInitial, agentStatus, isTalking, isWorking, statusBadgeStyle } from "./helpers";
import { agentLabel } from "../agents/helpers";
import { STR } from "./strings";
import type { Agent, AgentAction, AgentActivity } from "./types";

interface AgentCardProps {
  agent: Agent;
  activity: AgentActivity | undefined;
  busy: boolean;
  selected: boolean;
  onSelect: () => void;
  onAction: (action: AgentAction) => void;
  onChat: () => void;
  onEdit: () => void;
}

// Compact icon button used inside the card's quick-action row. Stops the click
// from bubbling up to the card's select-on-click handler.
function QuickBtn({
  title,
  label,
  danger,
  disabled,
  onClick,
}: {
  title: string;
  label: ReactNode;
  danger?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className={danger ? "btn danger" : "btn"}
      title={title}
      aria-label={title}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      style={{ padding: "5px 9px", fontSize: 13, lineHeight: 1, display: "inline-flex", alignItems: "center", gap: 5 }}
    >
      {label}
    </button>
  );
}

export function AgentCard({
  agent,
  activity,
  busy,
  selected,
  onSelect,
  onAction,
  onChat,
  onEdit,
}: AgentCardProps) {
  const t = useT(STR);
  const working = isWorking(agent, activity);
  const talking = isTalking(agent, activity);
  const color = agent.color || "var(--accent)";
  const status = agentStatus(agent);
  const statusLabel = t(`status.${status}`);

  // Avatar glow class: animated talking > working pulse, hairline ring at rest.
  const avatarStateClass = talking ? "is-talking" : working ? "is-working" : "";
  const restGlow: CSSProperties = avatarStateClass ? {} : { boxShadow: "0 0 0 1px var(--border)" };

  const channelCount = agent.channels?.length ?? 0;

  // Drive the keyframed avatar/card glow off a per-card --agent-color custom
  // property, so the animations in styles.ts pick up the agent's own hue. The
  // custom property is injected via a typed cast (CSSProperties has no index).
  const cardStyle: CSSProperties = {
    padding: 0,
    overflow: "hidden",
    display: "flex",
    cursor: "pointer",
    position: "relative",
    // Borderless: running agents get a green-tinted surface; selection shows an
    // accent ring (box-shadow) rather than a border.
    background: agent.running ? "color-mix(in srgb, var(--success) 9%, var(--surface))" : undefined,
    boxShadow: selected ? "0 0 0 2px var(--accent)" : undefined,
    ...({ "--agent-color": color } as Record<string, string>),
  };

  return (
    <div
      className={`card lc-ov-card${selected ? " is-active" : ""}`}
      onClick={onSelect}
      role="button"
      aria-pressed={selected}
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      style={cardStyle}
    >
      {/* colour bar */}
      <div style={{ width: 4, background: color, flex: "0 0 auto" }} aria-hidden />
      <div style={{ padding: 14, flex: 1, minWidth: 0 }}>
        {/* head: avatar + name + model */}
        <div className="row" style={{ alignItems: "flex-start", gap: 10 }}>
          <div
            className={`lc-ov-avatar ${avatarStateClass}`}
            style={{
              width: 40,
              height: 40,
              borderRadius: "50%",
              flex: "0 0 auto",
              display: "grid",
              placeItems: "center",
              fontSize: 16,
              fontWeight: 700,
              color: agent.color ? color : "var(--text-2)",
              background: agent.color ? `${color}22` : "var(--surface-2)",
              overflow: "hidden",
              ...restGlow,
            }}
          >
            {agent.avatar_url ? (
              <img src={agent.avatar_url} alt="" style={{ width: "100%", height: "100%", objectFit: "cover" }} />
            ) : (
              <span>{agentInitial(agent)}</span>
            )}
          </div>
          <div style={{ minWidth: 0, flex: 1 }}>
            <strong style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {agentLabel(agent)}
            </strong>
            <div
              className="muted"
              style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            >
              {agent.model || "—"}
            </div>
          </div>
        </div>

        {/* description */}
        <div
          className="muted"
          style={{
            fontSize: 13,
            marginTop: 8,
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {agent.description || t("agent.noDescription")}
        </div>

        {/* meta badges */}
        <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 10 }}>
          <span
            className="badge"
            style={statusBadgeStyle(status)}
            title={status === "error" ? agent.error : undefined}
          >
            {statusLabel}
          </span>
          {(agent.subagent_count ?? 0) > 0 && (
            <span className="badge">
              {t(agent.subagent_count === 1 ? "badge.subagent" : "badge.subagents", {
                n: String(agent.subagent_count),
              })}
            </span>
          )}
          {(agent.tool_count ?? 0) > 0 && (
            <span className="badge">
              {t(agent.tool_count === 1 ? "badge.tool" : "badge.tools", { n: String(agent.tool_count) })}
            </span>
          )}
          {(agent.skill_count ?? 0) > 0 && (
            <span className="badge">
              {t(agent.skill_count === 1 ? "badge.skill" : "badge.skills", { n: String(agent.skill_count) })}
            </span>
          )}
          {(agent.mcp_count ?? 0) > 0 && (
            <span className="badge">
              {t(agent.mcp_count === 1 ? "badge.mcp" : "badge.mcps", { n: String(agent.mcp_count) })}
            </span>
          )}
          {channelCount > 0 && (
            <span className="badge">
              {t(channelCount === 1 ? "badge.channel" : "badge.channels", { n: String(channelCount) })}
            </span>
          )}
        </div>

        {/* quick actions — running agents keep their primary action (Chat) and
            idle agents keep Start always visible; the secondary controls fade in
            on hover / focus / selection (legacy reveal-on-hover behaviour). */}
        <div className="row" style={{ gap: 6, marginTop: 12, flexWrap: "wrap" }}>
          {agent.running ? (
            <>
              <QuickBtn
                title={t("agent.chat")}
                label={
                  <>
                    <MessageSquare size={14} strokeWidth={1.75} /> {t("agent.chat")}
                  </>
                }
                disabled={busy}
                onClick={onChat}
              />
              <QuickBtn
                title={t("agent.edit")}
                label={
                  <>
                    <Pencil size={14} strokeWidth={1.75} /> {t("agent.edit")}
                  </>
                }
                onClick={onEdit}
              />
              <span className="row lc-ov-quick" style={{ gap: 6, flexWrap: "wrap" }}>
                <QuickBtn
                  title={t("agent.restart")}
                  label={busy ? <span className="spinner" /> : <RotateCw size={14} strokeWidth={1.75} />}
                  disabled={busy}
                  onClick={() => onAction("restart")}
                />
                <QuickBtn
                  title={t("agent.stop")}
                  label={busy ? <span className="spinner" /> : <Square size={14} strokeWidth={1.75} />}
                  danger
                  disabled={busy}
                  onClick={() => onAction("stop")}
                />
              </span>
            </>
          ) : (
            <>
              <button
                className="btn primary"
                disabled={busy}
                onClick={(e) => {
                  e.stopPropagation();
                  onAction("start");
                }}
                style={{ padding: "5px 11px", fontSize: 13, display: "inline-flex", alignItems: "center", gap: 5 }}
              >
                {busy ? (
                  <span className="spinner" />
                ) : (
                  <>
                    <Play size={14} strokeWidth={1.75} /> {t("agent.start")}
                  </>
                )}
              </button>
              <span className="row lc-ov-quick" style={{ gap: 6 }}>
                <QuickBtn title={t("agent.edit")} label={<Pencil size={14} strokeWidth={1.75} />} onClick={onEdit} />
              </span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
