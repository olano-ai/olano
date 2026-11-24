// App-level HITL approvals-inbox store.
//
// When an autonomous (scheduled/cognition) or delegated A2A turn defers a gated
// action, the backend registers it via security/hitl.request_approval and
// broadcasts an "approvals_changed" frame on /api/stream. The deferred turn is
// blocked on a future until a human resolves it here (or it times out).
//
// The sidebar Approvals badge must stay correct on any tab, so the count lives
// at module scope (mirrors schedulerStore). One shared, reference-counted
// EventSource refreshes the list from /api/hitl/pending on every change.

import { useSyncExternalStore } from "react";
import { api } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import { subscribeResume, shouldRefetch } from "../../core/lifecycle";

export interface PendingApproval {
  id: string;
  agent: string;
  action: string;
  description: string;
  risk: string;
  root_source: string;
  call_path: string[];
  args: unknown;
  created_at: string;
  timeout_seconds: number;
}

let pending: PendingApproval[] = [];
const listeners = new Set<() => void>();
let unsub: (() => void) | null = null;
let unsubResume: (() => void) | null = null;
let refCount = 0;

function emit(): void {
  for (const l of listeners) l();
}

// Monotonic refresh sequencing: only the NEWEST refresh may write `pending`.
// Without it, a slow response snapshotted before a resolve could land after
// the resolve completed and resurrect the already-decided card.
let refreshSeq = 0;

async function refresh(): Promise<void> {
  const seq = ++refreshSeq;
  try {
    const data = await api.get<{ pending: PendingApproval[] }>("/api/hitl/pending");
    if (seq !== refreshSeq) return; // superseded by a newer refresh
    // Never resurrect a card whose decision is still in flight — a refresh
    // racing the POST would otherwise flash it back and invite a second click.
    pending = (data.pending || []).filter((p) => !inflightResolves.has(p.id));
    emit();
  } catch {
    // Transient (server restart / auth gap) — keep the last known list.
  }
}

// Decisions in flight, keyed by approval id: blocks double-submit (second
// click no-ops) and filters refresh() races.
const inflightResolves = new Set<string>();

// Optimistic: the card disappears the instant the user decides — an approval
// is a security surface, so feedback must be immediate and a double-click must
// be impossible. A failed POST restores the card in place and rethrows so the
// caller can surface the error.
export async function resolveApproval(id: string, approved: boolean): Promise<void> {
  if (inflightResolves.has(id)) return;
  const idx = pending.findIndex((p) => p.id === id);
  const item = idx >= 0 ? pending[idx] : null;
  inflightResolves.add(id);
  if (item) {
    pending = pending.filter((p) => p.id !== id);
    emit();
  }
  try {
    await api.post("/api/hitl/resolve", { id, approved });
  } catch (e) {
    if (item) {
      pending = [...pending.slice(0, idx), item, ...pending.slice(idx)];
      emit();
    }
    throw e;
  } finally {
    inflightResolves.delete(id);
    void refresh();
  }
}

function start(): void {
  if (unsub) return;
  void refresh();
  // Share the app-wide multiplexed /api/stream connection (see
  // core/agentStream.ts) rather than opening a dedicated EventSource.
  unsub = subscribeStream((frame) => {
    if (frame.type === "approvals_changed") void refresh();
  });
  // approvals_changed frames pushed during a tab suspension are lost (no
  // replay) — inbox approvals survive server-side, so re-pull on resume.
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

export function useApprovals(): PendingApproval[] {
  return useSyncExternalStore(subscribe, () => pending);
}

// Sidebar badge hook: live count of pending approvals.
export function useApprovalsCount(): number {
  return useSyncExternalStore(
    subscribe,
    () => pending.length,
  );
}
