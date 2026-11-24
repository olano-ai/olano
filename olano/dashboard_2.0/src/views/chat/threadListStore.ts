// The per-agent thread list, held once and shared.
//
// `GET /api/agents/{agent}/threads` walks every transcript of that agent on
// the server, and three surfaces want its answer: the dashboard sidebar's
// per-agent chat list, the chat view's history drawer and the simple shell's
// rail. Each used to fetch it for itself, wire its own SSE watcher, debounce
// its own refetch and keep its own copy — so opening the chat view read every
// transcript twice, and a rename in one list took a round-trip to show in the
// other. Here it is read once per agent, kept as long as anyone is looking,
// and every consumer sees the same rows the moment they change.
//
// Two rules, both inherited from the lists this replaces:
//
//   * a failed refetch keeps the last good rows — blanking the list makes a
//     network error look like "no conversations yet";
//   * a delete or rename lands in the rows immediately (before the refetch
//     that confirms it), because a refetch that fails would otherwise leave a
//     just-deleted thread as a ghost.
//
// Refetch triggers, per retained agent: `chatStore.subscribeThreads` (this tab
// created or finished a thread) and the app-wide `/api/stream` frames
// `railShouldRefetch` recognises (a thread of that agent moved on ANOTHER
// surface — Telegram, a scheduled run, a second browser), debounced because a
// turn announces itself several times within seconds.

import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { api } from "../../core/api";
import { subscribeStream } from "../../core/agentStream";
import type { ThreadSummary } from "../../core/types";
import { chatStore } from "./sessionStore";
import { pickFromNewestPage, pickLatestThread } from "./threadPick";
import { railShouldRefetch } from "./threadSync";

export type ThreadListStatus = "idle" | "loading" | "ready" | "error";

/** How long after an external frame the refetch waits for its siblings. */
const REFETCH_DEBOUNCE_MS = 800;

interface Entry {
  threads: ThreadSummary[];
  status: ThreadListStatus;
  inflight: Promise<ThreadSummary[]> | null;
  /**
   * A refetch was asked for while one was in flight. That request started
   * before whatever prompted the ask (a turn's first row, a rename), so its
   * answer may not have it: one more fetch runs when it lands. Joining it
   * instead is how a thread a phone had just started stayed missing from the
   * rail until the turn ended, minutes later on a busy box.
   */
  again: boolean;
  /** How many hooks currently hold this agent's list open. */
  retained: number;
  unwatch: (() => void) | null;
  timer: ReturnType<typeof setTimeout> | null;
}

const EMPTY: ThreadSummary[] = [];
const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();
const errorListeners = new Set<(agent: string) => void>();
let version = 0;
let unsubscribeThreads: (() => void) | null = null;

function enc(s: string): string {
  return encodeURIComponent(s);
}

function entryFor(agent: string): Entry {
  let e = entries.get(agent);
  if (!e) {
    e = { threads: EMPTY, status: "idle", inflight: null, again: false, retained: 0, unwatch: null, timer: null };
    entries.set(agent, e);
  }
  return e;
}

function bump(): void {
  version += 1;
  for (const l of listeners) l();
}

function load(agent: string): Promise<ThreadSummary[]> {
  if (!agent) return Promise.resolve(EMPTY);
  const e = entryFor(agent);
  // Single-flight: two consumers asking within one round-trip share it.
  if (e.inflight) return e.inflight;
  if (e.status !== "ready") {
    e.status = "loading";
    bump();
  }
  const p = api
    .get<ThreadSummary[]>(`/api/agents/${enc(agent)}/threads`)
    .then((rows) => {
      e.threads = Array.isArray(rows) ? rows : EMPTY;
      e.status = "ready";
      return e.threads;
    })
    .catch((err: unknown) => {
      // Keep the rows we have; only the status says something went wrong.
      e.status = e.threads.length > 0 ? "ready" : "error";
      for (const l of errorListeners) l(agent);
      throw err;
    })
    .finally(() => {
      if (e.inflight === p) e.inflight = null;
      bump();
      if (e.again) {
        e.again = false;
        reload(agent);
      }
    });
  e.inflight = p;
  return p;
}

/**
 * A refetch the caller does not wait on: failures already reach the error
 * listeners. Asked for while a fetch is in flight, it runs once more after
 * that one rather than joining it (see `Entry.again`).
 */
function reload(agent: string): void {
  const e = entryFor(agent);
  if (e.inflight) {
    e.again = true;
    return;
  }
  void load(agent).catch(() => {});
}

/** One page of the newest threads (`?limit=N`), read without touching the shared list. */
interface ThreadPage {
  threads: ThreadSummary[];
  has_more: boolean;
}

/** Threads in the arrival read: enough that a person's conversation is almost always on it. */
const ARRIVAL_PAGE = 20;

function newestPage(agent: string): Promise<ThreadPage> {
  return api.get<ThreadPage>(`/api/agents/${enc(agent)}/threads?limit=${ARRIVAL_PAGE}`);
}

/**
 * The thread to open when a chat shell arrives on an agent (`threadPick`'s
 * rule), without waiting for the whole list when the newest page can answer.
 *
 * Rows already loaded (the sidebar holds them live) answer at once. Otherwise
 * the newest page decides whenever a person's conversation is on it; only an
 * agent whose newest twenty threads are all machine runs waits for the full
 * list, as every arrival used to. A runtime that predates pages answers the
 * page request with the whole list, which is then the list. Rejects only when
 * the full list cannot be read either.
 */
async function arrivalThread(agent: string): Promise<ThreadSummary | null> {
  const e = entryFor(agent);
  if (e.status === "ready" && e.threads.length > 0) return pickLatestThread(e.threads);
  try {
    const page: ThreadPage | ThreadSummary[] = await newestPage(agent);
    if (Array.isArray(page)) return pickLatestThread(page);
    const got = pickFromNewestPage(page.threads ?? [], Boolean(page.has_more));
    if (got.decided) return got.pick;
  } catch {
    // The full list below is the fallback, and it reports its own failure.
  }
  return pickLatestThread(await load(agent));
}

function scheduleReload(agent: string): void {
  const e = entryFor(agent);
  if (e.timer !== null) return;
  e.timer = setTimeout(() => {
    e.timer = null;
    reload(agent);
  }, REFETCH_DEBOUNCE_MS);
}

function watch(agent: string): void {
  const e = entryFor(agent);
  if (e.unwatch) return;
  e.unwatch = subscribeStream((frame) => {
    if (railShouldRefetch(frame, agent)) scheduleReload(agent);
  });
  if (!unsubscribeThreads) {
    unsubscribeThreads = chatStore.subscribeThreads(() => {
      for (const [name, entry] of entries) if (entry.retained > 0) reload(name);
    });
  }
}

function unwatch(agent: string): void {
  const e = entries.get(agent);
  if (!e) return;
  e.unwatch?.();
  e.unwatch = null;
  if (e.timer !== null) {
    clearTimeout(e.timer);
    e.timer = null;
  }
}

export const threadListStore = {
  // useSyncExternalStore wiring (stable references).
  subscribe(l: () => void): () => void {
    listeners.add(l);
    return () => listeners.delete(l);
  },
  getSnapshot(): number {
    return version;
  },
  /** Fires with the agent whose refetch just failed — for a toast. */
  subscribeError(l: (agent: string) => void): () => void {
    errorListeners.add(l);
    return () => errorListeners.delete(l);
  },

  /** The rows known for an agent right now (a stable empty array before the first load). */
  get(agent: string): ThreadSummary[] {
    return entries.get(agent)?.threads ?? EMPTY;
  },
  status(agent: string): ThreadListStatus {
    return entries.get(agent)?.status ?? "idle";
  },

  /**
   * Fetch (or join the fetch in flight for) an agent's list. Resolves with the
   * rows; rejects on a network failure, in which case the previous rows are
   * kept and `subscribeError` listeners have already been told.
   */
  load,
  /** Refetch without waiting. */
  refresh: reload,
  /**
   * The newest threads only (`GET …/threads?limit=20`): the server reads those
   * transcripts and stats the rest, where the full list reads every summary.
   * What a chat view arriving on an agent opens from (`threadPick.pickFromNewestPage`).
   */
  newestPage,
  arrivalThread,

  /**
   * Keep an agent's list live: loaded now, refetched on every signal that it
   * changed, for as long as at least one holder remains. Returns the release.
   */
  retain(agent: string): () => void {
    if (!agent) return () => {};
    const e = entryFor(agent);
    e.retained += 1;
    if (e.retained === 1) watch(agent);
    // Always refetch on a new holder: the rows may be minutes old, and the
    // previous ones stay on screen meanwhile (same agent, so never wrong).
    // A fetch already in flight is fresh enough for a new holder, so this one
    // joins it; only a change signal (`reload`) queues a second one.
    void load(agent).catch(() => {});
    let released = false;
    return () => {
      if (released) return;
      released = true;
      e.retained = Math.max(0, e.retained - 1);
      if (e.retained === 0) unwatch(agent);
    };
  },

  /** Drop a row now (a delete that succeeded), ahead of the confirming refetch. */
  remove(agent: string, threadId: string): void {
    const e = entries.get(agent);
    if (!e) return;
    const next = e.threads.filter((th) => th.id !== threadId);
    if (next.length === e.threads.length) return;
    e.threads = next;
    bump();
  },

  /** Rename a row now (a PATCH that succeeded), ahead of the confirming refetch. */
  setName(agent: string, threadId: string, name: string): void {
    const e = entries.get(agent);
    if (!e) return;
    e.threads = e.threads.map((th) => (th.id === threadId ? { ...th, name } : th));
    bump();
  },

  /**
   * Persist a display name. The row updates as soon as the server agrees, and
   * a refetch follows so the derived fields settle too. Throws on failure so
   * the caller can say so.
   */
  async rename(agent: string, threadId: string, name: string): Promise<void> {
    await api.patch(`/api/agents/${enc(agent)}/threads/${enc(threadId)}`, { name });
    threadListStore.setName(agent, threadId, name);
    reload(agent);
  },

  /**
   * Delete a thread everywhere it lives: the server, this list, and the live
   * session store (which aborts any stream on it and clears the active
   * pointer — the caller decides what to open next). Throws on failure.
   */
  async delete(agent: string, threadId: string): Promise<void> {
    await api.del(`/api/agents/${enc(agent)}/threads/${enc(threadId)}`);
    chatStore.dropThread(agent, threadId);
    threadListStore.remove(agent, threadId);
    reload(agent);
  },
};

/**
 * An agent's thread list, live for the life of the component. `onError` is
 * called on each failed refetch (the rows on screen are kept) — the place for
 * a toast.
 */
export function useThreadList(
  agent: string,
  onError?: () => void,
): { threads: ThreadSummary[]; status: ThreadListStatus; refresh: () => void } {
  useSyncExternalStore(threadListStore.subscribe, threadListStore.getSnapshot);
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  useEffect(() => {
    if (!agent) return;
    const release = threadListStore.retain(agent);
    const off = threadListStore.subscribeError((who) => {
      if (who === agent) onErrorRef.current?.();
    });
    return () => {
      release();
      off();
    };
  }, [agent]);
  const refresh = useCallback(() => {
    if (agent) reload(agent);
  }, [agent]);
  return {
    threads: agent ? threadListStore.get(agent) : EMPTY,
    status: agent ? threadListStore.status(agent) : "idle",
    refresh,
  };
}
