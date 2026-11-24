// App-level scheduler activity store.
//
// The sidebar Scheduler nav item shows a live bubble with the number of ACTIVE
// (enabled) scheduled jobs — the same count the Scheduler view labels "Active".
// That badge must stay correct even while the user is on another tab (where
// SchedulerView is unmounted), so the count can't live in SchedulerView's
// component state. This module owns it at module scope and keeps it fresh from
// the global /api/stream "scheduler_update" frame, mirroring how chatStore keeps
// the Chat unread badge alive across navigation.
//
// Components read it through useSyncExternalStore (see Layout): listeners fire
// only when the count actually changes, so a mounted subscriber re-renders the
// moment a job is created, deleted, paused, resumed, or completes. The
// EventSource is reference-counted — opened when the first subscriber mounts,
// closed when the last unmounts — so there's exactly one shared stream.

import { useSyncExternalStore } from "react";
import { api } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { subscribeResume, shouldRefetch } from "../../core/lifecycle";
import type { Task } from "./types";

let activeCount = 0;
const listeners = new Set<() => void>();
let unsub: (() => void) | null = null;
let unsubResume: (() => void) | null = null;
let refCount = 0;

function setActive(n: number): void {
  if (n === activeCount) return;
  activeCount = n;
  for (const l of listeners) l();
}

// Count enabled tasks the caller can see. The server already scopes
// /api/scheduler/tasks to the authenticated user's visibility, so this is the
// app-wide active count for the badge — no per-agent filtering (the badge is
// global, unlike the Scheduler view's filterable "Active" section).
async function refresh(): Promise<void> {
  try {
    const tasks = await api.get<Task[]>("/api/scheduler/tasks");
    setActive(tasks.filter((t) => t.enabled).length);
  } catch {
    // Transient (server restart / auth gap) — keep the last known count.
  }
}

function start(): void {
  if (unsub) return;
  void refresh();
  // Share the app-wide multiplexed /api/stream connection instead of opening a
  // dedicated EventSource (see core/agentStream.ts). The backend broadcasts
  // scheduler_update on create / delete / update (pause / resume / edit) and on
  // one-time task completion — every event that can move the active count.
  unsub = subscribeStream((frame) => {
    if (frame.type === "scheduler_update") void refresh();
  });
  // scheduler_update frames pushed during a tab suspension are lost (no
  // replay) — re-pull the snapshot when the tab comes back.
  unsubResume = subscribeResume((e) => {
    if (shouldRefetch(e)) void refresh();
  });
}

function stop(): void {
  unsub?.();
  unsub = null;
  unsubResume?.();
  unsubResume = null;
}

export const schedulerStore = {
  subscribe(l: () => void): () => void {
    listeners.add(l);
    if (++refCount === 1) start();
    return () => {
      listeners.delete(l);
      if (--refCount === 0) stop();
    };
  },
  // Primitive snapshot — useSyncExternalStore re-renders only when it changes.
  getSnapshot: (): number => activeCount,
};

// Sidebar badge hook: the live count of active (enabled) scheduled jobs.
export function useSchedulerActiveCount(): number {
  return useSyncExternalStore(schedulerStore.subscribe, schedulerStore.getSnapshot);
}
