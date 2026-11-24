// Live delivery of magic-link outcomes into an open chat.
//
// A sign-in finishes on the user's phone, minutes after the turn that started
// it ended. The chat has no stream of its own by then — `streamPost` lives and
// dies with a single turn — so the only way the news reaches an open thread is
// the app-wide multiplexed `/api/stream`, which is what this subscribes to.
//
// The frame is a convenience, not the record: the same outcome is persisted
// into the thread's transcript server-side, so a user who was not looking (or
// had the tab closed) still finds the card when they open the thread. That is
// why a frame for a thread this tab has not loaded is simply dropped rather
// than queued — there is nothing to lose.

import { useEffect } from "react";
import { subscribeStream, type StreamFrame } from "../../core/agentStream";
import { chatStore, type MagicLinkResult } from "./sessionStore";

interface MagicLinkFrame {
  agent?: string;
  thread_id?: string;
  link_id?: string;
  status?: string;
  flow?: string;
  label?: string;
  target_agent?: string;
  apply?: string;
  text?: string;
}

/** Frame payload → the shape the card renders. Mirrors `magicLinkFromRow`. */
function fromFrame(d: MagicLinkFrame): MagicLinkResult {
  return {
    linkId: d.link_id || "",
    status: d.status || "",
    flow: d.flow || "",
    label: d.label || "",
    targetAgent: d.target_agent || "",
    apply: d.apply || "",
    text: d.text || "",
  };
}

/**
 * Subscribe to `magic_link_updated` frames for the life of the session.
 *
 * Mounted **once**, in `app.tsx`'s `GatedApp` — above both UI modes, not in
 * either chat view. The chat store is global and shared by both shells, and a
 * notice has to land in its own thread whether or not that thread (or even
 * that shell) is what is currently on screen. The subscription is ref-counted
 * onto the one shared EventSource, so this costs no extra connection.
 */
export function useConnectNotices(enabled: boolean): void {
  useEffect(() => {
    // Gated rather than conditional, because hooks cannot be: before the auth
    // gate resolves, /api/stream would 401 and the shared EventSource would
    // burn its reconnect backoff on a request that was never going to work.
    if (!enabled) return;
    return subscribeStream((frame: StreamFrame) => {
      if (frame.type !== "magic_link_updated") return;
      const d = frame.data as MagicLinkFrame | undefined;
      if (!d?.agent || !d.thread_id) return;
      chatStore.pushConnectResult(d.agent, d.thread_id, fromFrame(d));
    });
  }, [enabled]);
}
