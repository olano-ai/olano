/**
 * Where "open this agent" goes, wherever it is asked from: the Chats list, a
 * widget, a Control Center control, a Shortcut. The last conversation the
 * person had with the agent (chat/threadPick.ts), asked of the deployment as
 * exactly one thread; a new conversation when there is none; and when the
 * deployment cannot answer, the agent's conversations, which say why and
 * offer Retry.
 */

import type { Api } from "../net/api.ts";
import { conversationPath } from "./threadPick.ts";

export async function openAgentPath(api: Api, agent: string): Promise<string> {
  try {
    const page = await api.threadPage(agent, { limit: 1, kind: "conversation" });
    return conversationPath(agent, page.threads[0] ?? null);
  } catch {
    return `/chats/${encodeURIComponent(agent)}`;
  }
}
