import type { Agent as BaseAgent } from "../../core/types";

// Overview consumes a few extra fields beyond the shared Agent slice (mcp/skill
// counts, sprite strip, boot error). Extend locally rather than mutating the
// shared source of truth.
export interface Agent extends BaseAgent {
  mcp_count?: number;
  skill_count?: number;
  sprites?: string | null;
  error?: string;
}

export interface StatusResponse {
  status: string;
  agents: string[];
  /** Connector types actually attached to a booted agent (`platform:telegram`, …). */
  channels: string[];
  /** Connector count the booted agents declare — may exceed `channels.length`
   *  when one failed to attach. Absent on runtimes older than this field. */
  channels_configured?: number;
  scheduler_jobs: number;
  uptime: string;
  capabilities: { image_gen?: boolean; openai_image_gen: boolean };
}

export interface SchedulerTask {
  enabled: boolean;
}

export interface AgentActivity {
  status: string; // thinking | talking | idle
  updatedAt: number;
  lastWorkingAt: number;
}

export interface StreamEvent {
  type: string;
  data: unknown;
  ts?: string;
}

export type SystemAction = "start" | "stop" | "restart";
export type AgentAction = "start" | "stop" | "restart";

export type LogLevel = "error" | "warn" | "debug" | "info";

export interface ParsedLog {
  level: LogLevel;
  ts: string;
  msg: string;
  raw: string;
}
