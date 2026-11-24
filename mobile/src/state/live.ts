/**
 * The deployment's event stream (`GET /api/stream`), held open while the app
 * is in the foreground and signed in. It is what keeps lists current without
 * polling: a new message on Telegram, a reply finishing, an approval waiting.
 *
 * Closed when the app goes to the background (the system would cut it anyway,
 * and an open view is what stops the deployment from sending a push), and
 * reopened with a short backoff when it drops.
 */

import type { OpenStream } from "../net/transport.ts";
import { allConversations, findConversation } from "../chat/conversation.ts";
import { allGroupConversations, findGroupConversation } from "../chat/groupConversation.ts";
import { connectResultFromRow } from "../chat/model.ts";
import { liveBrowserFor } from "../browser/live.ts";
import { invalidate, invalidateSoon } from "./resources.ts";
import { session } from "./session.ts";

let stream: OpenStream | null = null;
let wanted = false;
let retry: ReturnType<typeof setTimeout> | null = null;
let attempts = 0;

type Handler = (type: string, data: Record<string, unknown>) => void;
const handlers = new Set<Handler>();

export function onLiveEvent(handler: Handler): () => void {
  handlers.add(handler);
  return () => handlers.delete(handler);
}

function handle(type: string, data: Record<string, unknown>): void {
  const agent = typeof data.agent === "string" ? data.agent : "";
  const threadId = typeof data.thread_id === "string" ? data.thread_id : "";
  switch (type) {
    case "connected":
      attempts = 0;
      break;
    case "thread_updated": {
      const role = (data.message as { role?: string } | undefined)?.role ?? data.role;
      if (agent && threadId) {
        // A conversation that is following its own stream already has this row.
        if (role === "user" || role === "assistant" || role === "peer_inbound") {
          findConversation(agent, threadId)?.refreshIfIdle();
          invalidateSoon(`threads:${agent}`);
        }
      }
      break;
    }
    case "browser_session":
      // An agent's browser opened, closed, changed hands or asked for help.
      if (agent) {
        invalidate(`browser:${agent}`);
        if (data.event === "opened") liveBrowserFor(agent).retry();
      }
      break;
    case "magic_link_updated":
      // How a magic link sent in a conversation ended: shown in that
      // conversation if it is open (its transcript has it for later).
      if (agent && threadId) {
        findConversation(agent, threadId)?.addConnectResult(connectResultFromRow(data as Record<string, string>));
        invalidateSoon(`threads:${agent}`);
      }
      break;
    case "run_ended":
      if (agent) invalidateSoon(`threads:${agent}`);
      break;
    case "group_updated": {
      const id = typeof data.id === "number" ? data.id : null;
      const kind = typeof data.kind === "string" ? data.kind : "";
      invalidate("groups");
      if (id !== null) {
        invalidate(`group-preview:${id}`);
        findGroupConversation(id)?.onUpdated(kind);
      }
      break;
    }
    case "agents_update":
    case "agent_status":
    case "agent_joined":
    case "agent_avatar_updated":
      invalidate("agents");
      break;
    case "notifications_changed":
      invalidate("notifications");
      break;
    case "issue_updated":
      invalidate("issue:");
      invalidate("issues:");
      break;
    case "scheduler_update":
      invalidate("scheduled");
      break;
    case "file_updated":
      invalidate("files:");
      break;
    case "approvals_changed":
      invalidate("approvals");
      break;
    case "pulse_resolved":
    case "cortex_job":
      invalidate("cortex");
      invalidate("approvals");
      break;
    default:
      break;
  }
  for (const handler of handlers) handler(type, data);
}

async function open(): Promise<void> {
  const api = session.get().api;
  if (!wanted || stream || !api || session.get().phase !== "ready") return;
  // Read before opening: the stream's first frame ("connected") resets the count.
  const reconnecting = attempts > 0;
  try {
    const opened = await api.events((event) => handle(event.type, event.data ?? {}));
    if (!wanted) {
      opened.cancel();
      return;
    }
    stream = opened;
    // Anything that happened while the stream was closed.
    if (reconnecting) {
      invalidate("");
      for (const conversation of allConversations()) conversation.refreshIfIdle();
      for (const group of allGroupConversations()) group.onUpdated("");
    }
    const end = await opened.done;
    if (stream === opened) stream = null;
    if (wanted && !end.cancelled) schedule();
  } catch {
    stream = null;
    if (wanted) schedule();
  }
}

function schedule(): void {
  if (retry) return;
  attempts += 1;
  const delay = Math.min(30000, 1000 * 2 ** Math.min(attempts, 5));
  retry = setTimeout(() => {
    retry = null;
    void open();
  }, delay);
}

export function startLive(): void {
  wanted = true;
  void open();
}

export function stopLive(): void {
  wanted = false;
  if (retry) clearTimeout(retry);
  retry = null;
  stream?.cancel();
  stream = null;
}
