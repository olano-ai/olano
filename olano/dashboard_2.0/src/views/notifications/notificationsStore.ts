// App-level operator-notifications store.
//
// The runtime raises a notification whenever something needs a human: a
// connector skipped because its credential is missing, a vault still locked
// after boot, a disk over threshold. The backend (runtime/notifications.py)
// broadcasts a "notifications_changed" frame on /api/stream on every change.
//
// The bell lives in the sidebar and the section lives in Overview, so the count
// must stay correct on every tab — which is why state lives at module scope and
// one shared, reference-counted subscription refreshes it (mirrors
// approvalsStore / schedulerStore).

import { useSyncExternalStore } from "react";
import { api } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { subscribeResume, shouldRefetch } from "../../core/lifecycle";

export type NotificationLevel = "info" | "success" | "warn" | "error";

export interface OperatorNotification {
  id: string;
  kind: string;
  level: NotificationLevel;
  title: string;
  body: string;
  agent: string | null;
  created_at: string;
  updated_at: string;
  read: boolean;
  count: number;
  action: { label: string; href: string } | null;
}

interface Snapshot {
  notifications: OperatorNotification[];
  unread: number;
  total: number;
  level: NotificationLevel | null;
}

const EMPTY: Snapshot = { notifications: [], unread: 0, total: 0, level: null };

let snapshot: Snapshot = EMPTY;
const listeners = new Set<() => void>();
let unsub: (() => void) | null = null;
let unsubResume: (() => void) | null = null;
let refCount = 0;

function emit(): void {
  for (const l of listeners) l();
}

// Only the newest refresh may write. A slow response snapshotted before a
// "mark read" could otherwise land after it and resurrect the badge.
let refreshSeq = 0;

async function refresh(): Promise<void> {
  const seq = ++refreshSeq;
  try {
    const data = await api.get<Snapshot>("/api/notifications");
    if (seq !== refreshSeq) return;
    snapshot = {
      notifications: data.notifications || [],
      unread: data.unread || 0,
      total: data.total || 0,
      level: data.level ?? null,
    };
    emit();
  } catch {
    // Transient (server restart / auth gap) — keep the last known state.
  }
}

// Worst-first, matching the backend's own ordering.
const LEVELS: NotificationLevel[] = ["error", "warn", "success", "info"];

function worstUnread(rows: OperatorNotification[]): NotificationLevel | null {
  return LEVELS.find((lv) => rows.some((n) => !n.read && n.level === lv)) ?? null;
}

/** Mark notifications read. Omit `ids` for everything currently visible. */
export async function markRead(ids?: string[]): Promise<void> {
  const targets = ids ?? snapshot.notifications.filter((n) => !n.read).map((n) => n.id);
  if (targets.length === 0) return;
  // Optimistic: opening the bell should clear the badge immediately, and a
  // failed POST is recoverable (the refresh below restores the truth).
  // Recompute the count and level from the rows rather than subtracting the
  // target count — a partial mark-read must leave the remaining unread ones
  // (and the badge's tone) intact.
  const seen = new Set(targets);
  const next = snapshot.notifications.map((n) => (seen.has(n.id) ? { ...n, read: true } : n));
  snapshot = {
    ...snapshot,
    notifications: next,
    unread: next.filter((n) => !n.read).length,
    level: worstUnread(next),
  };
  emit();
  try {
    await api.post("/api/notifications/read", { ids: targets });
  } finally {
    void refresh();
  }
}

export async function dismissNotification(id: string): Promise<void> {
  // Dismissing an unread row has to move the badge too, so recompute rather
  // than only shrinking the total.
  const next = snapshot.notifications.filter((n) => n.id !== id);
  snapshot = {
    ...snapshot,
    notifications: next,
    total: Math.max(0, snapshot.total - 1),
    unread: next.filter((n) => !n.read).length,
    level: worstUnread(next),
  };
  emit();
  try {
    await api.del(`/api/notifications/${encodeURIComponent(id)}`);
  } finally {
    void refresh();
  }
}

export async function clearRead(): Promise<void> {
  try {
    await api.post("/api/notifications/clear", {});
  } finally {
    void refresh();
  }
}

function start(): void {
  if (unsub) return;
  void refresh();
  // Share the app-wide multiplexed /api/stream connection rather than opening
  // a dedicated EventSource.
  unsub = subscribeStream((frame) => {
    if (frame.type === "notifications_changed") void refresh();
  });
  // Frames pushed while the tab was suspended are lost (no replay), and these
  // survive server-side, so re-pull on resume.
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

function subscribe(l: () => void): () => void {
  listeners.add(l);
  if (++refCount === 1) start();
  return () => {
    listeners.delete(l);
    if (--refCount === 0) stop();
  };
}

export function useNotifications(): Snapshot {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/** Live unread count for the bell and the Overview nav badge. */
export function useUnreadCount(): number {
  return useSyncExternalStore(
    subscribe,
    () => snapshot.unread,
  );
}
