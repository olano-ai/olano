/**
 * An agent's threads, held a page at a time.
 *
 * An agent that has been used for months has thousands of threads, and
 * listing them all was what kept its row in Chats from opening. The list now
 * reads the newest `PAGE_SIZE` (`GET …/threads?limit=N`) and the page below
 * as the person scrolls (`before`, the opaque cursor the previous page
 * returned); the agent's row asks for exactly one thread, the newest
 * conversation a person had (`kind: "conversation"`).
 *
 * A runtime from before paging ignores the parameters and answers with every
 * thread as a bare list. That reads as one page holding them all, so the app
 * keeps working against it: the `kind` filter and the newest-first order are
 * applied here, with nothing below.
 *
 * Pure: no network, no DOM, so `node --test` covers it.
 */

import type { ThreadPage, ThreadSummary } from "../net/types.ts";
import { newestFirst } from "../shared/dashboard.ts";
import { isConversation } from "./threadPick.ts";

/** Threads per page in the Conversations list. */
export const PAGE_SIZE = 20;

export interface ThreadPageRequest {
  limit: number;
  before?: string;
  kind?: "conversation";
}

export function normalizeThreadPage(answer: ThreadPage | ThreadSummary[], request: ThreadPageRequest): ThreadPage {
  if (!Array.isArray(answer)) {
    return { ...answer, threads: answer.threads ?? [], has_more: !!answer.has_more, next_before: answer.next_before ?? null };
  }
  const rows = newestFirst(request.kind === "conversation" ? answer.filter(isConversation) : answer);
  return { threads: rows, has_more: false, next_before: null };
}

/** The pages read so far as one list: newest first, every thread once. */
export function mergePages(pages: readonly (readonly ThreadSummary[])[]): ThreadSummary[] {
  const seen = new Set<string>();
  const out: ThreadSummary[] = [];
  for (const page of pages) {
    for (const row of page) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      out.push(row);
    }
  }
  return newestFirst(out);
}

/**
 * The agent's "latest conversation" page with a conversation this phone just
 * started on top. The row is what the deployment will list for it once the
 * first message is on disk (a mobile chat, titled by that message), so the
 * agents screen shows it, and a tap reopens it, from the moment the
 * deployment names the thread rather than after the next refresh lands.
 */
export function withStartedThread(page: ThreadPage | undefined, threadId: string, firstMessage: string, now = new Date()): ThreadPage {
  const title = firstMessage.trim().replace(/\s+/g, " ").slice(0, 120);
  const row: ThreadSummary = {
    id: threadId,
    title,
    preview: title,
    origin: "interactive",
    channel: "mobile",
    message_count: 1,
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
    running: true,
  };
  const rest = (page?.threads ?? []).filter((t) => t.id !== threadId);
  return { threads: [row, ...rest].slice(0, Math.max(1, page?.threads.length ?? 1)), has_more: page?.has_more ?? false, next_before: page?.next_before ?? null };
}

/** Loaded pages with `update` applied to the thread it names (a rename, a delete). */
export function mapThread(page: ThreadPage, id: string, update: (row: ThreadSummary) => ThreadSummary | null): ThreadPage {
  const threads: ThreadSummary[] = [];
  for (const row of page.threads) {
    if (row.id !== id) {
      threads.push(row);
      continue;
    }
    const next = update(row);
    if (next) threads.push(next);
  }
  return { ...page, threads };
}
