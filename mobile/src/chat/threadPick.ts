/**
 * Which conversation opens when you tap an agent: the one you were last
 * having with it, like the dashboard's chat (views/chat/threadPick.ts), so the
 * list of every thread (scheduled runs, other agents, Cortex) is one step away
 * instead of in the way.
 *
 * A conversation here is one a person had: in the dashboard, this app, or a
 * messenger. Scheduled and heartbeat runs, agent-to-agent threads, MCP and A2A
 * calls never count (the dashboard's rule), and neither do Cortex threads,
 * which this app keeps under Cortex. No conversation yet: a new one.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

import { isHumanConversation, newestFirst } from "../shared/dashboard.ts";

export interface PickableThread {
  id: string;
  origin?: string;
  channel?: string;
  updated_at?: string;
  created_at?: string;
}

export function isConversation(thread: PickableThread): boolean {
  return isHumanConversation(thread) && (thread.origin || "") !== "cortex";
}

/** The conversation to open, or null for a new one. */
export function lastConversation<T extends PickableThread>(threads: readonly T[]): T | null {
  return newestFirst(threads).find(isConversation) ?? null;
}

/** Where tapping the agent goes. */
export function conversationPath(agent: string, thread: PickableThread | null): string {
  return `/chats/${encodeURIComponent(agent)}/${thread ? encodeURIComponent(thread.id) : "new"}`;
}
