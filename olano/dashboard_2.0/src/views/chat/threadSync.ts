// Live sync of every chat thread across surfaces and devices.
//
// A turn can run on Telegram, from a scheduled job, through the OpenAI
// connector, or in another browser — none of which touch this tab's
// per-turn `/api/chat/stream` (`streamPost` lives and dies with one send). The
// runtime announces each of those on the app-wide multiplexed `/api/stream`:
//
//   thread_updated   one frame per transcript row, already projected into the
//                    exact shape `GET /threads/{id}` serves → folded into the
//                    open session for that thread (`chatStore.applyThreadRow`)
//   thread_activity  thinking / streaming (with the reply so far) / idle for a
//                    thread being worked from elsewhere → the typing indicator
//                    and a growing preview bubble (`chatStore.setRemoteActivity`)
//   run_started      a detached turn began on a thread (olano/core/runs.py) →
//                    an open session attaches to it, so a turn started on one
//                    device draws live on every other one
//   run_ended        that turn finished → the session stops considering it live
//   connected        the stream (re)opened → a missed-rows resync of every
//                    loaded, idle thread, but only after a drop, never on the
//                    first open of a page (nothing is loaded yet anyway)
//
// The rail (thread list) is a separate consumer: `useThreadListSync` in
// ChatView refetches the list on the frames that can change it.
//
// Mounted **once**, in `app.tsx`'s `GatedApp`, beside `useConnectNotices` and
// for the same reason: the chat store is global and shared by both shells, and
// a row has to land in its thread whether or not that thread is on screen.

import { useEffect } from "react";
import { subscribeStream, type StreamFrame } from "../../core/agentStream";
import { chatStore, type ThreadMessage } from "./sessionStore";

export interface ThreadUpdatedFrame {
  agent?: string;
  thread_id?: string;
  role?: string;
  ts?: string;
  message?: ThreadMessage;
}

export interface ThreadActivityFrame {
  agent?: string;
  thread_id?: string;
  status?: string;
  channel?: string;
  text?: string;
}

/** A detached run starting or ending. Content-free by design — an id and a
 * kind, never message bodies — because the shared bus fans out to every
 * subscriber and only frames it can attribute to an agent are scoped. */
export interface RunLifecycleFrame {
  agent?: string;
  thread_id?: string;
  run_id?: string;
  turn_id?: string;
  kind?: string;
}

/** Roles whose arrival changes what the thread rail shows (title, preview, order). */
const RAIL_ROLES = new Set(["user", "peer_inbound", "assistant"]);

/** Whether a frame is one the conversation rail should refetch on, for `agent`. */
export function railShouldRefetch(frame: StreamFrame, agent: string): boolean {
  if (!agent) return false;
  if (frame.type === "thread_updated") {
    const d = frame.data as ThreadUpdatedFrame | undefined;
    return d?.agent === agent && RAIL_ROLES.has(String(d.role || ""));
  }
  if (frame.type === "thread_activity") {
    // Turn end: cost/preview/updated_at settle in one go.
    const d = frame.data as ThreadActivityFrame | undefined;
    return d?.agent === agent && d.status === "idle";
  }
  if (frame.type === "run_started" || frame.type === "run_ended") {
    // The rail's live dot comes from the thread list's `running` flag.
    const d = frame.data as RunLifecycleFrame | undefined;
    return d?.agent === agent;
  }
  return false;
}

export function useThreadSync(enabled: boolean): void {
  useEffect(() => {
    // Gated rather than conditional, because hooks cannot be: before the auth
    // gate resolves, /api/stream would 401 and the shared EventSource would
    // burn its reconnect backoff on a request that was never going to work.
    if (!enabled) return;
    // The server emits `connected` at the top of EVERY connection, so the
    // first one is the page's own open — no rows can have been missed yet.
    let connections = 0;
    return subscribeStream((frame: StreamFrame) => {
      switch (frame.type) {
        case "thread_updated": {
          const d = frame.data as ThreadUpdatedFrame | undefined;
          if (!d?.agent || !d.thread_id || !d.message) return;
          chatStore.applyThreadRow(d.agent, d.thread_id, d.message);
          return;
        }
        case "thread_activity": {
          const d = frame.data as ThreadActivityFrame | undefined;
          if (!d?.agent || !d.thread_id || !d.status) return;
          chatStore.setRemoteActivity(d.agent, d.thread_id, d.status, {
            channel: d.channel,
            text: d.text,
          });
          return;
        }
        case "run_started": {
          const d = frame.data as RunLifecycleFrame | undefined;
          if (!d?.agent || !d.thread_id || !d.run_id) return;
          chatStore.noteRunStarted(d.agent, d.thread_id, d.run_id, d.turn_id || "");
          return;
        }
        case "run_ended": {
          const d = frame.data as RunLifecycleFrame | undefined;
          if (!d?.agent || !d.thread_id || !d.run_id) return;
          chatStore.noteRunEnded(d.agent, d.thread_id, d.run_id);
          return;
        }
        case "connected":
          if (++connections > 1) void chatStore.resyncOpenThreads();
          return;
        default:
          return;
      }
    });
  }, [enabled]);
}
