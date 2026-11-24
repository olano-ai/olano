// Mid-turn steering — the pure half of the chat store's queue logic.
//
// A message typed while the agent is still replying is either *queued* (sent
// as the next turn when the reply ends) or *steered* (handed to the running
// turn through `POST /api/chat/steer`; the model reads it at its next step,
// after the tool calls in flight finish). This module holds the data-only
// rules — what a `steer` frame does to the transcript and the queue, which
// chips a reconciled thread proves delivered, how Edit takes a chip back into
// the composer — with no React and no store, so `node --test` covers them
// (steering.test.ts). sessionStore.ts owns the network calls and the mutation.
import type { Msg, QueuedMsg, ThreadMessage } from "./sessionStore";
import type { Attachment } from "./attachments";

export interface SteerFrameLike {
  id: string;
  text: string;
  sender?: string;
}

// A client-minted steer id: the chip is matched to its `steer` frame (and can be
// cancelled) without waiting for the POST to answer. UUID where the platform
// offers one; `crypto.randomUUID` is absent on a plain-http LAN dashboard, so the
// fallback stays inside the backend's `[A-Za-z0-9_-]{8,64}` rule.
export function newSteerId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
}

// Can this chip still be steered? Only a queued, text-only chip while a turn
// is live — a steer chip already was, files can't ride a steer (v1), and with
// nothing running the chip simply drains as the next turn.
export function canSteerChip(q: QueuedMsg, streaming: boolean): boolean {
  return q.kind === "queue" && streaming && q.attachments.length === 0;
}

// The model just read a steered message. Drop its chip and split the live
// transcript the way `foldRow` rebuilds the persisted rows: the aside as a user
// bubble, then an empty assistant bubble the continuation streams into.
export function applySteerFrame(
  messages: Msg[],
  queue: QueuedMsg[],
  frame: SteerFrameLike,
  nextId: () => number,
  ts: number,
): { messages: Msg[]; queue: QueuedMsg[] } {
  // Match on the id the chip was posted with. A chip whose POST has not
  // answered yet still carries its client-minted id, so this is exact; the
  // oldest id-less steer chip is the fallback for a server-minted id.
  let idx = queue.findIndex((q) => q.kind === "steer" && q.steerId === frame.id);
  if (idx < 0) idx = queue.findIndex((q) => q.kind === "steer" && !q.steerId);
  const nextQueue = idx < 0 ? queue : queue.filter((_, i) => i !== idx);
  const user: Msg = {
    id: nextId(),
    role: "user",
    body: frame.text,
    tools: [],
    subs: [],
    ts,
    steer: true,
    sender: frame.sender || undefined,
  };
  const assistant: Msg = { id: nextId(), role: "assistant", body: "", tools: [], subs: [], approvals: [], ts };
  return { messages: [...messages, user, assistant], queue: nextQueue };
}

// After a reconcile, the server's rows are the truth about which steers the
// model read while this tab was cut off. Those chips must not drain as new
// turns — the checkpoint already holds them.
export function dropDeliveredSteers(queue: QueuedMsg[], rows: ThreadMessage[]): QueuedMsg[] {
  if (queue.length === 0) return queue;
  const ids = new Set<string>();
  const texts = new Set<string>();
  for (const r of rows) {
    if (r.role !== "user" || !r.steer) continue;
    if (r.steer_id) ids.add(r.steer_id);
    const body = (r.content || "").trim();
    if (body) texts.add(body);
  }
  if (ids.size === 0 && texts.size === 0) return queue;
  return queue.filter(
    (q) => !(q.kind === "steer" && ((q.steerId && ids.has(q.steerId)) || texts.has(q.text.trim()))),
  );
}

// Edit: pull a chip back into the composer. Its text goes AHEAD of whatever is
// already typed (one line each, the way Claude Code takes back a queued entry)
// and its files are re-staged before the current ones, so nothing the person
// wrote is lost or reordered behind what they wrote later.
export function takeBackQueued(
  queue: QueuedMsg[],
  id: number,
  input: string,
  staged: Attachment[],
): { queue: QueuedMsg[]; input: string; staged: Attachment[] } | null {
  const q = queue.find((x) => x.id === id);
  if (!q) return null;
  const typed = input.trim() ? input : "";
  const text = q.text && typed ? `${q.text}\n${typed}` : q.text || typed;
  return {
    queue: queue.filter((x) => x.id !== id),
    input: text,
    staged: [...q.attachments, ...staged],
  };
}
