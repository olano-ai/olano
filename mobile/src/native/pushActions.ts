/**
 * Answering an approval from its notification (iOS): the deployment sends
 * approval notifications with the `approval` category (olano/push/events.py),
 * whose Approve and Decline buttons AppDelegate.swift registers. Both buttons
 * ask the phone to be unlocked and open the app, which answers here and says
 * what happened. A high-risk approval comes without the buttons: the person
 * sees what exactly will run before deciding.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

export const APPROVE_ACTION = "approve";
export const DECLINE_ACTION = "decline";

export type NotificationAnswer =
  | { kind: "chat"; threadId: string; agent: string; count: number; approve: boolean }
  | { kind: "inbox"; id: string; approve: boolean };

/** What a button on a notification asks for, or null for a plain tap. */
export function answerFor(actionId: string, data: Record<string, unknown>): NotificationAnswer | null {
  if (actionId !== APPROVE_ACTION && actionId !== DECLINE_ACTION) return null;
  const approve = actionId === APPROVE_ACTION;
  const text = (key: string) => (typeof data[key] === "string" ? (data[key] as string) : "");
  if (data.kind === "approval" && text("thread_id")) {
    const count = Math.min(20, Math.max(1, Number.parseInt(text("count") || "1", 10) || 1));
    return { kind: "chat", threadId: text("thread_id"), agent: text("agent"), count, approve };
  }
  if (data.kind === "inbox_approval" && text("item_id")) return { kind: "inbox", id: text("item_id"), approve };
  return null;
}

/** What the app says once it has answered. */
export function answerNotice(answer: NotificationAnswer, outcome: "done" | "gone" | "failed", agentLabel = ""): string {
  const who = agentLabel || ("agent" in answer && answer.agent) || "The agent";
  if (outcome === "gone") return "That approval was already answered, or the agent stopped waiting.";
  if (outcome === "failed") return "The answer could not be sent. Open the approval and try again.";
  return answer.approve ? `Approved. ${who} carries on.` : `Declined. ${who} won't run it.`;
}

/** The route a notification's data asks for (see olano/push/events.py `_data`). */
export function routeFor(data: Record<string, unknown>): string | null {
  const agent = typeof data.agent === "string" ? data.agent : "";
  const thread = typeof data.thread_id === "string" ? data.thread_id : "";
  switch (data.kind) {
    case "reply":
    case "approval":
      return agent && thread ? `/chats/${encodeURIComponent(agent)}/${encodeURIComponent(thread)}` : null;
    case "browser_check":
      // The agent's live browser waits for a person: its conversations, where the browser shows.
      return agent ? `/chats/${encodeURIComponent(agent)}` : null;
    case "pulse":
      // A finding is read in Cortex; a card that needs an approval waits in the Inbox.
      return data.lane === "approval" ? "/inbox" : "/cortex/pulse";
    case "inbox_approval":
    case "alert":
      return "/inbox";
    default:
      return null;
  }
}
