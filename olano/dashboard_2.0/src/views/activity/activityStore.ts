// App-level A2A (Agent-to-Agent) activity store.
//
// The sidebar Activity nav item shows a live "working" pulse while any A2A
// exchange is mid-flight (a peer/subagent message sent but not yet answered)
// plus a badge with the number of NEW A2A messages that have arrived since the
// user last opened the Activity panel. Both must stay correct on any tab (where
// ActivityView is unmounted), so the state can't live in ActivityView's
// component state — this module owns it at module scope and keeps it fresh from
// the global /api/stream "agent_message" frames, mirroring schedulerStore /
// chatStore.
//
// The runtime emits two phases per A2A call: "send" opens an exchange and
// "reply" closes it. We count an open send as work-in-progress (the pulse) and
// treat every send/reply frame as a new message for the unread badge. A full
// page reload starts fresh (matching chatStore); ActivityView reloads the real
// conversation list from /api/a2a/threads on mount.

import { useSyncExternalStore } from "react";
import { api } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { subscribeResume, shouldRefetch } from "../../core/lifecycle";

let unread = 0;
// Open sends still awaiting a reply. > 0 ⇒ the working pulse is shown.
let working = 0;
// True while ActivityView is mounted/focused: arriving messages don't badge.
let viewing = false;
// Bumped on every change so useSyncExternalStore re-renders mounted subscribers.
let version = 0;

const listeners = new Set<() => void>();
let unsub: (() => void) | null = null;
let unsubResume: (() => void) | null = null;
let refCount = 0;

function emit(): void {
  version++;
  for (const l of listeners) l();
}

function onFrame(frame: { type?: string; data?: unknown }): void {
  if (frame.type !== "agent_message") return;
  const phase = (frame.data as { phase?: string } | undefined)?.phase;
  if (phase === "send") working += 1;
  else if (phase === "reply" && working > 0) working -= 1;
  // Both phases are a new message in the conversation — badge them unless the
  // user is already looking at the Activity panel.
  if (!viewing) unread += 1;
  emit();
}

// A tab suspension loses live frames with no replay — if a "reply" frame was
// among them, `working` would stay stuck (sidebar pulses forever). Recompute
// from the server's conversation rollup, which folds the whole scan window and
// ages an unanswered send out of "in flight" once it can no longer be pending
// (a failed talk_to_agent never publishes a reply at all).
//
// This replaces a client-side count over a 100-event tail that matched sends to
// replies *positionally* across every pair: a window starting mid-exchange left
// `working` permanently above zero, pulsing the sidebar forever.
async function recomputeWorking(): Promise<void> {
  try {
    const rows = await api.get<{ in_flight?: number }[]>("/api/a2a/threads");
    const next = (rows || []).reduce((n, r) => n + (r.in_flight || 0), 0);
    if (next !== working) {
      working = next;
      emit();
    }
  } catch {
    // Endpoint unavailable (older runtime) — keep the last known state.
  }
}

function start(): void {
  if (unsub) return;
  // Share the app-wide multiplexed /api/stream connection (see
  // core/agentStream.ts) rather than opening a dedicated EventSource.
  unsub = subscribeStream(onFrame);
  // `unread` keeps accumulating across a suspension (badging while away is
  // correct); only the stuck-pulse risk needs the resume recompute.
  unsubResume = subscribeResume((e) => {
    if (shouldRefetch(e)) void recomputeWorking();
  });
}

function stop(): void {
  unsub?.();
  unsub = null;
  unsubResume?.();
  unsubResume = null;
}

export const activityStore = {
  subscribe(l: () => void): () => void {
    listeners.add(l);
    if (++refCount === 1) start();
    return () => {
      listeners.delete(l);
      if (--refCount === 0) stop();
    };
  },
  // Primitive snapshot — useSyncExternalStore re-renders only when it changes.
  getSnapshot: (): number => version,

  // Aggregate indicator state for the sidebar. Never store the returned object
  // in getSnapshot (a fresh object would loop the store); read it after the
  // version snapshot has driven the re-render.
  stats(): { unread: number; busy: boolean } {
    return { unread, busy: working > 0 };
  },

  // The user opened the Activity panel: clear the unread badge and keep it clear
  // while the view stays mounted.
  markViewing(): void {
    viewing = true;
    if (unread !== 0) {
      unread = 0;
      emit();
    }
  },

  // The user navigated away — new messages count toward the badge again.
  clearViewing(): void {
    viewing = false;
  },
};

// Sidebar indicator hook: live unread A2A message count + working status.
export function useActivityIndicator(): { unread: number; busy: boolean } {
  useSyncExternalStore(activityStore.subscribe, activityStore.getSnapshot);
  return activityStore.stats();
}
