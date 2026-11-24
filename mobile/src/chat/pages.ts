/**
 * A conversation held a page at a time.
 *
 * Opening a conversation reads only its newest page (`PAGE_SIZE` messages,
 * rounded out to whole turns by the deployment), and scrolling up reads the
 * page above, until the first message. A refresh (another device wrote to the
 * thread, the app came back to the foreground) reads the newest page again
 * and keeps the older pages already read, as long as the new page continues
 * them; when so much was written in between that it does not, the older
 * pages are dropped and read again on the way up.
 *
 * Positions are the deployment's: the index of a row among the thread's
 * visible rows, which a transcript never renumbers (it only grows). A runtime
 * from before paging sends the whole transcript with no position, which is a
 * history that starts at 0 and has nothing above it.
 *
 * Pure: no network, no DOM, so `node --test` covers it. `fold` is
 * `foldTranscript`, passed in so this module carries no rendering rules.
 */

import type { ThreadDetail, ThreadRow } from "../net/types.ts";
import type { ChatMessage } from "./model.ts";

/** Messages per page. Two screens' worth of a busy conversation, and tens of KB. */
export const PAGE_SIZE = 50;

/** How close to the top (in screen heights) the page above starts loading. */
export const PREFETCH_SCREENS = 2;

export type Fold = (rows: ThreadRow[]) => ChatMessage[];

export interface History {
  /** Position of the first row held: the `before` of the page above. */
  first: number;
  /** The pages above the newest one, folded, oldest first. */
  older: ChatMessage[];
  /** Where the newest page starts, and its rows. */
  newestStart: number;
  newestRows: ThreadRow[];
}

export function hasMore(history: History | null): boolean {
  return !!history && history.first > 0;
}

/** The newest page arrived (opening, or a refresh). */
export function withNewest(prev: History | null, detail: Pick<ThreadDetail, "messages" | "start">, fold: Fold): History {
  const rows = detail.messages || [];
  const start = detail.start;
  if (start === undefined) {
    // The whole transcript, from a runtime that does not page.
    return { first: 0, older: [], newestStart: 0, newestRows: rows };
  }
  if (prev && start >= prev.newestStart && start <= prev.newestStart + prev.newestRows.length) {
    // Continues what is held: the rows that left the newest page join the older ones.
    const moved = prev.newestRows.slice(0, start - prev.newestStart);
    return {
      first: prev.first,
      older: moved.length ? [...prev.older, ...fold(moved)] : prev.older,
      newestStart: start,
      newestRows: rows,
    };
  }
  return { first: start, older: [], newestStart: start, newestRows: rows };
}

/**
 * The page above arrived. `null` when it no longer fits (the history was
 * replaced while it loaded), so the caller drops it.
 */
export function withOlder(
  prev: History,
  before: number,
  detail: Pick<ThreadDetail, "messages" | "start">,
  fold: Fold,
): { history: History; added: ChatMessage[] } | null {
  if (prev.first !== before) return null;
  // A runtime that ignored `before` sent everything again: nothing is above.
  const start = detail.start ?? 0;
  if (detail.start === undefined || start >= before) return { history: { ...prev, first: 0 }, added: [] };
  const added = fold(detail.messages || []);
  return { history: { ...prev, first: start, older: [...added, ...prev.older] }, added };
}
