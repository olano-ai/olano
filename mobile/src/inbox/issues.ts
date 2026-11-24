/**
 * Issues as the app shows them. Who did what is a principal on the
 * deployment's tracker: `human-<username>` for a person, `ai-<agent>` for an
 * agent (olano/issues/principals.py).
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

import type { IssueEvent } from "../net/types.ts";

export function principalKind(principal: string): "human" | "agent" | "other" {
  if (principal.startsWith("human-")) return "human";
  if (principal.startsWith("ai-")) return "agent";
  return "other";
}

/** The name without its prefix ("ai-nova" is "nova"). */
export function bareName(principal: string): string {
  return principal.replace(/^(human|ai)-/, "");
}

/** A principal for an agent, to hand an issue to it. */
export function agentPrincipal(agent: string): string {
  return `ai-${agent}`;
}

/** One line for an event that is not a comment ("nova closed this"), or null for a comment. */
export function eventLine(event: IssueEvent, name: (principal: string) => string): string | null {
  const who = name(event.actor);
  const reason = event.payload.reason ? `: ${event.payload.reason}` : "";
  switch (event.kind) {
    case "comment":
      return null;
    case "close":
      return `${who} closed this${reason}`;
    case "reopen":
      return `${who} reopened this${reason}`;
    case "assign":
      return event.payload.assignee ? `${who} assigned it to ${name(event.payload.assignee)}` : `${who} unassigned it`;
    case "label":
      return event.payload.labels?.length ? `${who} set the labels to ${event.payload.labels.join(", ")}` : `${who} removed the labels`;
    case "type":
      return `${who} made it a ${event.payload.type ?? "task"}`;
    default:
      return `${who}: ${event.kind}`;
  }
}

/** The message that asks an agent about the issue, for the person to send (or change first). */
export function issueDraft(issue: { id: number; title: string }): string {
  return `Let's talk about issue #${issue.id}, "${issue.title}". Read it with its comments first.`;
}

/** The signed-in person as the tracker names them (the runtime's `_actor`). */
export function myPrincipal(me: { user?: { username?: string } } | null | undefined): string {
  return `human-${me?.user?.username || "owner"}`;
}

export type IssueFilter = "open" | "mine" | "all";

/** What each filter asks the tracker for. */
export function issueQuery(filter: IssueFilter, me: string): { state: "open" | "all"; assignee: string } {
  if (filter === "mine") return { state: "open", assignee: me };
  return { state: filter === "all" ? "all" : "open", assignee: "" };
}
