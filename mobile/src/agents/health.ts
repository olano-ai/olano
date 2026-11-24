/**
 * Where an agent stands, in words a person can act on: running, starting,
 * stopped on purpose, failed to start (with the reason), or held back (with
 * the reason), and which of Start, Stop and Restart make sense from there.
 */

import type { Agent } from "../net/types.ts";

export type AgentState = "running" | "starting" | "stopped" | "error" | "blocked";

export interface AgentHealth {
  state: AgentState;
  label: string;
  /** Why, when the agent is not answering: the boot error or what holds it back. */
  detail: string;
  canStart: boolean;
  canStop: boolean;
  canRestart: boolean;
}

export function agentHealth(agent: Pick<Agent, "running" | "paused" | "status" | "error" | "blocked" | "blocked_reason">): AgentHealth {
  const status = (agent.status || "").toLowerCase();
  const error = (agent.error || "").trim();
  if (agent.blocked || status === "blocked") {
    return {
      state: "blocked",
      label: "Held back",
      detail: (agent.blocked_reason || "").trim() || "The deployment is holding this agent back.",
      canStart: false,
      canStop: false,
      canRestart: false,
    };
  }
  if (status === "error") {
    return {
      state: "error",
      label: "Could not start",
      detail: error || "It stopped with an error. The deployment's logs have the details.",
      canStart: true,
      canStop: agent.running,
      canRestart: agent.running,
    };
  }
  if (status === "starting") {
    return { state: "starting", label: "Starting", detail: "", canStart: false, canStop: false, canRestart: false };
  }
  // Stopped on purpose wins over `running`: a booted agent's row can still
  // say running while its paused flag is set (dashboard views/agents/presence.ts).
  if (agent.running && !agent.paused) {
    return { state: "running", label: "Running", detail: "", canStart: false, canStop: true, canRestart: true };
  }
  return {
    state: "stopped",
    label: "Stopped",
    detail: agent.paused ? "Someone stopped it. It stays stopped until it is started again." : "It is not running right now.",
    canStart: true,
    canStop: false,
    canRestart: false,
  };
}

/** Why a conversation with this agent cannot go on, for the banner above the composer. */
export interface Unavailable {
  title: string;
  body: string;
  /** This person may start it from the phone (an admin, and starting can help). */
  canStart: boolean;
}

/**
 * The banner for an agent that will not answer, or null when it will (or is
 * starting). `admin` is whether this person may start agents; anyone else is
 * told who can.
 */
export function unavailableNotice(name: string, health: AgentHealth, admin: boolean): Unavailable | null {
  if (health.state === "running" || health.state === "starting") return null;
  const canStart = admin && health.canStart;
  const who = canStart ? "" : health.state === "blocked" ? "" : " Ask an admin of this deployment to start it.";
  switch (health.state) {
    case "blocked":
      return { title: `${name} is held back`, body: `${health.detail} It can't reply until that changes.`, canStart: false };
    case "error":
      return { title: `${name} couldn't start`, body: `${health.detail}${who}`, canStart };
    default:
      return { title: `${name} is stopped`, body: `It won't reply until it is started again. Earlier messages stay here.${who}`, canStart };
  }
}

/** The deployment's refusal (409 agent_not_running) as a banner, for when the cached agent list said it was running. */
export function unavailableFromError(name: string, detail: unknown): Unavailable | null {
  if (!detail || typeof detail !== "object") return null;
  const d = detail as Record<string, unknown>;
  if (d.code !== "agent_not_running") return null;
  const state = d.state === "blocked" ? "blocked" : d.state === "error" ? "error" : "stopped";
  const reason = typeof d.reason === "string" ? d.reason : "";
  const health: AgentHealth = {
    state,
    label: "",
    detail: reason || (state === "error" ? "It stopped with an error. The deployment's logs have the details." : ""),
    canStart: d.can_start === true,
    canStop: false,
    canRestart: false,
  };
  return unavailableNotice(name, health, d.can_start === true);
}
