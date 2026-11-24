// Which conversation to open when a person lands on an agent.
//
// Clicking an agent in either shell's rail used to greet the reader with an
// empty composer — every time, on every reload — while the conversation they
// were having on WhatsApp ten minutes ago sat one more click away. The rule
// now, in order:
//
//   1. the most recent conversation a PERSON is in: typed on a dashboard (the
//      plain `chats` bucket, Cortex hand-offs included) or arrived over a
//      messenger (Telegram, WhatsApp, …) — the conversation most likely to be
//      the one they came back to continue;
//   2. failing that, the most recent thread of any kind (a scheduled run, an
//      agent-to-agent exchange, an inbound MCP/A2A call) — something is better
//      than nothing to read;
//   3. failing that, nothing: the caller starts a fresh draft. A draft is local
//      until its first message is sent (see sessionStore.newChat), so "start a
//      new chat" never manufactures an empty thread on the server, however
//      many times an agent with no history is opened.
//
// Pure, so it is unit-tested under Node with no store or DOM.

import { threadBucket } from "./threadFilters.ts";
import { PROTOCOL_CHANNELS } from "./channelMeta.ts";

/** The fields the choice reads; a ThreadSummary satisfies it. */
export interface PickableThread {
  id: string;
  origin?: string;
  channel?: string;
  updated_at?: string | null;
}

function ts(iso: string | null | undefined): number {
  if (!iso) return 0;
  const n = Date.parse(iso);
  return Number.isNaN(n) ? 0 : n;
}

/**
 * A conversation with a person in it — the tier-1 candidate. Origin outranks
 * channel exactly as the filter buckets do, so a scheduled fan-out that ran on
 * Telegram is still "scheduled", never a messenger chat.
 */
export function isHumanConversation(th: PickableThread): boolean {
  const bucket = threadBucket(th.origin ?? "", th.channel ?? "");
  if (bucket === "chats") return true;
  if (bucket.startsWith("ch:")) return !PROTOCOL_CHANNELS.has(th.channel ?? "");
  return false;
}

/** Most recent first; a thread with no timestamp sorts last. */
export function newestFirst<R extends PickableThread>(threads: readonly R[]): R[] {
  return [...threads].sort((a, b) => ts(b.updated_at) - ts(a.updated_at));
}

/** The thread to open on arrival, or null when the agent has none. */
export function pickLatestThread<R extends PickableThread>(threads: readonly R[]): R | null {
  if (threads.length === 0) return null;
  const sorted = newestFirst(threads);
  return sorted.find(isHumanConversation) ?? sorted[0] ?? null;
}

/**
 * The same choice made from the newest page of threads alone, when the page
 * is enough to make it; `decided: false` means only the whole list can.
 *
 * Opening an agent used to wait for its whole thread list (every transcript
 * the agent ever had, seconds on a busy box) only to pick one row from the
 * top of it. The page is the newest threads by transcript time, so a person's
 * conversation found on it is the newest one anywhere: any newer one would be
 * on the page too. A page holding none decides only when it is the whole list
 * (`hasMore` false); otherwise tier 1 may sit further down and the caller
 * falls back to the full list, exactly as before.
 */
export function pickFromNewestPage<R extends PickableThread>(
  threads: readonly R[],
  hasMore: boolean,
): { decided: boolean; pick: R | null } {
  const sorted = newestFirst(threads);
  const person = sorted.find(isHumanConversation);
  if (person) return { decided: true, pick: person };
  if (!hasMore) return { decided: true, pick: sorted[0] ?? null };
  return { decided: false, pick: null };
}
