import type { CSSProperties } from "react";
import type { Agent, AgentActivity, LogLevel, ParsedLog } from "./types";

export const PAGE_SIZE = 12;
export const WORKING_WINDOW_MS = 14000;
export const MAX_LOGS = 1000;
export const VISIBLE_LOGS = 30;

// ── status badge → colour mapping (mirrors statusBadge() in the legacy UI) ──
export function statusBadgeStyle(status: string): CSSProperties {
  switch (status) {
    case "running":
      return { background: "var(--success-soft)", color: "var(--success)" };
    case "error":
      return { background: "var(--danger-soft)", color: "var(--danger)" };
    case "stopped":
      return {}; // default badge styling
    default:
      // starting | stopping | paused | configured
      return { background: "var(--warn-soft)", color: "var(--warn)" };
  }
}

export function agentStatus(a: Agent): string {
  return a.status || (a.running ? "running" : "configured");
}

// First letter of an agent's name, for the flat fallback avatar (never an emoji).
export function agentInitial(a: Agent): string {
  return (a.name?.trim()?.[0] || "?").toUpperCase();
}

// ── uptime, ticking each render ─────────────────────────────────────────────
export function formatUptime(iso: string): string {
  const started = new Date(iso).getTime();
  if (!Number.isFinite(started)) return "—";
  const s = Math.max(0, Math.floor((Date.now() - started) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d`;
}

// ── working / talking glow (mirrors renderAgentCard activity logic) ─────────
export function isWorking(a: Agent, act: AgentActivity | undefined): boolean {
  if (!a.running || !act) return false;
  return act.status === "thinking" || Date.now() - act.lastWorkingAt < WORKING_WINDOW_MS;
}

export function isTalking(a: Agent, act: AgentActivity | undefined): boolean {
  return !!a.running && act?.status === "talking";
}

// Sort running agents first (preserves order otherwise), exactly like
// renderOverviewAgents().
export function sortRunningFirst(agents: Agent[]): Agent[] {
  return [...agents].sort((x, y) => (y.running ? 1 : 0) - (x.running ? 1 : 0));
}

// ── log parsing (mirrors inferLevel() + formatLogLine()) ────────────────────
export function inferLevel(line: string): LogLevel {
  if (line.includes("[ERROR]")) return "error";
  if (line.includes("[WARNING]") || line.includes("[WARN]")) return "warn";
  if (line.includes("[DEBUG]")) return "debug";
  return "info";
}

export function parseLog(line: string): ParsedLog {
  const level = inferLevel(line);
  // "2026-05-06 12:34:56,123 [LEVEL] name: message"
  const m = /^(\S+\s\S+)\s\[(\w+)\]\s([^:]+):\s?(.*)$/.exec(line);
  let ts = "";
  let msg = line;
  if (m) {
    ts = m[1].split(" ")[1]?.split(",")[0] || "";
    msg = `${m[3]}: ${m[4]}`;
  }
  return { level, ts, msg, raw: line };
}

export function logLevelColor(level: LogLevel): string {
  switch (level) {
    case "error":
      return "var(--danger)";
    case "warn":
      return "var(--warn)";
    case "debug":
      return "var(--text-4)";
    default:
      return "var(--info)";
  }
}

// Boot lines that should trigger an agent-list refresh.
export function isBootLine(line: string): boolean {
  return /Agent '[^']+' booted/.test(line) || /Failed to boot agent/.test(line);
}
