// Small shared bits of the Agent-to-Agent view: agent identity chrome and the
// time formatters the rail and the transcript both use.

import type { Agent } from "../../core/types";
import { colorFor } from "../insights/helpers";
import { Globe } from "lucide-react";
import { externalParty, normName } from "./threads";

type T = (key: string, vars?: Record<string, string>) => string;

/**
 * An agent's colour: the roster value when the name is a real agent, else a
 * stable per-name hue. Subagent *types* are never on the roster, so the hash
 * fallback is the normal path for half the conversations here.
 */
export function agentColor(name: string, roster: Map<string, Agent>): string {
  return roster.get(name)?.color || colorFor(normName(name));
}

export function initial(name: string | undefined): string {
  const c = (name || "?").trim()[0];
  return (c || "?").toUpperCase();
}

/** Small colour dot identifying a participant. */
export function Pip({ color, size = 10 }: { color: string; size?: number }) {
  return (
    <span
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        background: color,
        boxShadow: "0 0 0 1px var(--border)",
        display: "inline-block",
        flexShrink: 0,
      }}
    />
  );
}

/** The agent's avatar image when it has one, else a flat initial chip on its
 *  colour. Never an emoji. */
export function AgentAvatar({
  name,
  color,
  avatarUrl,
  size = 28,
}: {
  name: string;
  color: string;
  avatarUrl?: string;
  size?: number;
}) {
  const base: React.CSSProperties = {
    width: size,
    height: size,
    borderRadius: "50%",
    flexShrink: 0,
    boxShadow: "0 0 0 1px var(--border)",
    filter: "saturate(1.1)",
  };
  if (avatarUrl) {
    return <img src={avatarUrl} alt="" style={{ ...base, objectFit: "cover", background: color }} />;
  }
  return (
    <span
      style={{
        ...base,
        background: color,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: Math.round(size * 0.43),
        fontWeight: 600,
        color: "#fff",
      }}
    >
      {initial(name)}
    </span>
  );
}

/**
 * The outside party of an external exchange — an MCP client, an A2A caller, a
 * remote A2A agent. Not one of our agents, so it gets a globe on a neutral disc
 * rather than an initial that pretends it is.
 */
export function ExternalAvatar({ size = 28, title }: { size?: number; title?: string }) {
  return (
    <span
      title={title}
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        flexShrink: 0,
        boxShadow: "0 0 0 1px var(--border)",
        background: "var(--surface-2)",
        color: "var(--text-2)",
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Globe size={Math.round(size * 0.55)} strokeWidth={1.75} />
    </span>
  );
}

/** The MCP / A2A badge an external party carries wherever it is named. */
export function ProtocolBadge({ protocol }: { protocol: string | undefined }) {
  const p = (protocol || "").toLowerCase();
  if (p !== "mcp" && p !== "a2a") return null;
  return <span className={`badge a2a-proto a2a-proto-${p}`}>{p.toUpperCase()}</span>;
}

/**
 * One side of a conversation as the rail and the transcript name it: our
 * agent's avatar + name, or — for an outside party — the globe, the caller's
 * label and an MCP / A2A badge, so a stranger's call never reads as a peer.
 */
export function Party({
  name,
  roster,
  size = 18,
  nameStyle,
}: {
  name: string;
  roster: Map<string, Agent>;
  size?: number;
  nameStyle?: React.CSSProperties;
}) {
  const ext = externalParty(name);
  if (ext) {
    return (
      <>
        <ExternalAvatar size={size} title={name} />
        <span className="a2a-name" style={nameStyle} title={name}>
          {ext.label}
        </span>
        <ProtocolBadge protocol={ext.protocol} />
      </>
    );
  }
  return (
    <>
      <AgentAvatar name={name} color={agentColor(name, roster)} size={size} />
      <span className="a2a-name" style={nameStyle}>
        {name}
      </span>
    </>
  );
}

export function fmtTime(d: string | undefined): string {
  if (!d) return "";
  const dt = new Date(d);
  if (Number.isNaN(dt.getTime())) return "";
  return dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function fmtRel(t: T, d: string | undefined): string {
  if (!d) return "";
  const dt = new Date(d);
  if (Number.isNaN(dt.getTime())) return "";
  const s = (Date.now() - dt.getTime()) / 1000;
  if (s < 60) return t("time.now");
  if (s < 3600) return t("time.min", { n: String(Math.floor(s / 60)) });
  if (s < 86400) return t("time.hour", { n: String(Math.floor(s / 3600)) });
  if (s < 604800) return t("time.day", { n: String(Math.floor(s / 86400)) });
  return dt.toLocaleDateString();
}

export function fmtDuration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** Truncate a preview at a word boundary rather than mid-token. */
export function peek(text: string, max = 90): string {
  const clean = (text || "").replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max);
  const space = cut.lastIndexOf(" ");
  return `${space > max * 0.6 ? cut.slice(0, space) : cut}…`;
}
