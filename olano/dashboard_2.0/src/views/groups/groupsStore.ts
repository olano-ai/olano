// Group chats — one module-scope store for the whole shell.
//
// A group is a named roster of 2–6 bots plus one attributed transcript
// (`GET/POST /api/groups*`). A post streams (`…/messages/stream`) but the
// transcript is shared state that other people and the bots themselves also
// write to, so every message is republished on the `/api/stream` bus as a
// content-free `group_updated` frame. That is why this store does four things
// the chat store does not have to:
//
//   1. **Optimistic echo.** A turn can take a minute, and the human's own
//      message must not be missing for it. Shown immediately, reconciled away
//      when the server's copy arrives.
//   2. **Live pull, not push.** `group_updated` carries `{id, kind}` and no
//      body, so it is a *hint*: refetch that group's transcript. That is how a
//      bot answering another bot appears without a refresh.
//   3. **A preview per group.** The list endpoint has no last-message field, so
//      the rail's one-line preview comes from a bounded `messages?limit=1` fan
//      out (see PREVIEW_LIMIT) refreshed per group on its own frame.
//   4. **Tool activity, held client-side.** The stream carries what a member
//      DID (tool calls, subagent transcripts) and the transcript endpoint does
//      not, so the two are kept apart: server prose in `messages`, live work in
//      `activity`, joined by message id at render time. What the transcript
//      DOES carry is where the tool cards go — `segments`, stored with the row
//      — and the files the reply named, re-derived per read against that row's
//      own author. Both matter because of (1): the post-send refetch below
//      replaces the live turn wholesale, so anything the server did not persist
//      is discarded a second after it was drawn.
//
// The whole surface is gated on a deployment capability that is OFF by default
// (see useGroupsEnabled) — nothing here fetches until a caller subscribes with
// `enabled`, so a default install never issues a single /api/groups request.

import { useSyncExternalStore } from "react";
import { api, ApiError } from "../../core/api";
import { readSSE, StreamRequestError, streamPost } from "../../core/sse";
import { subscribeStream } from "../../core/agentStream";
import { useCapabilities } from "../../core/capabilities";
import { shouldRefetch, subscribeResume } from "../../core/lifecycle";
import type { TurnSegment } from "../chat/turnText";
import type { MediaRef } from "../../core/types";
import {
  applyLiveFrame,
  liveTurn,
  type GroupActivity,
  type GroupLiveTurn,
  type GroupStreamFrame,
} from "./liveTurn";

// The live turn and its frame fold live in `liveTurn.ts` — pure, so the
// join-back-to-the-body invariant can be tested without a stream. Re-exported
// here because this module is the store's public face.
export type { GroupActivity, GroupLiveTurn, GroupStreamFrame } from "./liveTurn";

/**
 * One person in a group's room, never a bot. `role` says how they got here:
 * they made it, they were shared into it, or they are an admin who posted.
 * `principal` is the `human-<handle>` the transcript labels their turns with,
 * which is also what the agents are told, so the panel and the briefing can
 * never name a person two ways.
 */
export interface GroupPerson {
  id: string;
  username: string;
  name: string;
  principal: string;
  role: "creator" | "shared" | "spoke";
  is_admin?: boolean;
}

/** A group as the list/detail endpoints return it. */
export interface GroupSummary {
  id: number;
  name: string;
  /** Roster in speaking order — members[0] answers a message that names nobody. */
  members: string[];
  message_count: number;
  created_at?: string;
  updated_at?: string;
  created_by?: string;
  /** Team accounts (user ids) shared into the group. Absent on older backends. */
  shared_with?: string[];
  /** Everyone in the room who is not a bot. Absent on older backends. */
  people?: GroupPerson[];
}

/**
 * A room with more than one person in it, the group was shared.
 *
 * This is the one rule a share changes about turn-taking: in such a room a
 * message that names no agent is for the other people and wakes nobody, so
 * the composer's line under the box and the optimistic "typing" guess both
 * read it. Mirrors `pick_addressees(require_mention=...)` server-side.
 */
export function isShared(group: Pick<GroupSummary, "shared_with"> | undefined): boolean {
  return (group?.shared_with?.length ?? 0) > 0;
}

/** Someone a group could be shared with, as the candidates endpoint lists them. */
export interface ShareCandidate {
  id: string;
  username: string;
  name: string;
}

/** One line of a group's transcript. Attribution is never inferred client-side. */
export interface GroupMessage {
  id: number;
  group_id: number;
  ts: string;
  /** `human-<slug>` for a person, the agent name for a bot. */
  author: string;
  author_kind: "human" | "agent" | "system";
  /** Which member a message was aimed at ("" when it named nobody). */
  addressee: string;
  /** For a bot-to-bot turn, the member whose message it answers ("" for a
      human-addressed turn). Absent on older backends. */
  reply_to?: string;
  body: string;
  /** Viewer-relative: stamped per request by the server, because "You" is a
      different row for every reader. Absent on older backends. */
  mine?: boolean;
  /** The reply split at its tool boundaries, so the cards land between the
      paragraphs they belong to. Absent when the turn never interleaved (and on
      older backends), which the renderer handles by falling back to the
      stacked layout. */
  segments?: TurnSegment[];
  /** Files this turn named, re-derived per read against the AUTHOR's own
      workspace — a group has one root per member. */
  media?: MediaRef[];
}

/** A human turn shown before the server has confirmed it. */
export interface PendingMessage {
  /** Negative, so it can never collide with a server row id. */
  id: number;
  body: string;
  addressee: string;
  at: number;
}

export interface GroupsSnapshot {
  /** "loading" only before the FIRST answer; a later failure keeps the data. */
  status: "idle" | "loading" | "ready" | "error";
  groups: GroupSummary[];
  /** Last message of each group, for the rail's preview line. */
  previews: Record<number, GroupMessage | undefined>;
  /** Full transcript of every group opened this session, oldest → newest. */
  messages: Record<number, GroupMessage[] | undefined>;
  /** Optimistic human turns, per group, not yet echoed back by the server. */
  pending: Record<number, PendingMessage[] | undefined>;
  /** The member currently composing a reply (the POST is still in flight). */
  awaiting: Record<number, string | undefined>;
  /** A reply arriving word by word: the member, its text so far, and its work. */
  streaming: Record<number, GroupLiveTurn | undefined>;
  /** Settled tool/subagent work, per group, keyed by the message it produced. */
  activity: Record<number, Record<number, GroupActivity | undefined> | undefined>;
}

const EMPTY: GroupsSnapshot = {
  status: "idle",
  groups: [],
  previews: {},
  messages: {},
  pending: {},
  awaiting: {},
  streaming: {},
  activity: {},
};

// How many groups get a preview fetch. The rail shows a last-message line per
// row, and the list endpoint does not carry one — so this is a fan-out of one
// tiny request per group, and it needs a ceiling. Rows past it fall back to
// their member list, which is still a useful subtitle.
const PREVIEW_LIMIT = 12;

// How much transcript an open group holds. Same ceiling the backend clamps to.
const MESSAGE_LIMIT = 200;

let snapshot: GroupsSnapshot = EMPTY;
const listeners = new Set<() => void>();
let unsubStream: (() => void) | null = null;
let unsubResume: (() => void) | null = null;
let refCount = 0;
// Which group's transcript is on screen — the only one worth refetching in full
// on every frame. Everything else settles for a refreshed preview.
let openId: number | null = null;
let listSeq = 0;
const messageSeq = new Map<number, number>();
let listDebounce: ReturnType<typeof setTimeout> | null = null;

function emit(): void {
  for (const l of listeners) l();
}

function set(patch: Partial<GroupsSnapshot>): void {
  snapshot = { ...snapshot, ...patch };
  emit();
}

/** Human-readable text for a failed group call — the backend's own words win. */
export function groupErrorText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return e.detail || fallback;
  return e instanceof Error && e.message ? e.message : fallback;
}

// ── reads ────────────────────────────────────────────────────────────────────

/** Refresh the roster. A failure keeps the last good list (never blanks it). */
export async function refreshGroups(): Promise<void> {
  const seq = ++listSeq;
  if (snapshot.status === "idle") set({ status: "loading" });
  try {
    const res = await api.get<{ groups: GroupSummary[] }>("/api/groups");
    if (seq !== listSeq) return; // a newer refresh already answered
    const groups = res?.groups ?? [];
    set({ status: "ready", groups });
    // Drop transcripts/previews of groups that no longer exist, so a deleted
    // group can't leave its messages addressable in memory.
    const live = new Set(groups.map((g) => g.id));
    const messages = { ...snapshot.messages };
    const previews = { ...snapshot.previews };
    const activity = { ...snapshot.activity };
    let pruned = false;
    for (const bucket of [messages, previews, activity] as Record<number, unknown>[]) {
      for (const key of Object.keys(bucket)) {
        if (!live.has(Number(key))) {
          delete bucket[Number(key)];
          pruned = true;
        }
      }
    }
    if (pruned) set({ messages, previews, activity });
    await Promise.all(groups.slice(0, PREVIEW_LIMIT).map((g) => refreshPreview(g.id)));
  } catch {
    if (seq !== listSeq) return;
    // Keep whatever is on screen: an expired session or a blip must not read
    // as "you have no groups".
    set({ status: snapshot.groups.length ? "ready" : "error" });
  }
}

/** The one-line preview the rail shows under a group's name. */
async function refreshPreview(id: number): Promise<void> {
  try {
    const res = await api.get<{ messages: GroupMessage[] }>(
      `/api/groups/${id}/messages?limit=1`,
    );
    const last = (res?.messages ?? [])[0];
    set({ previews: { ...snapshot.previews, [id]: last } });
  } catch {
    // A preview is decoration — a failure leaves the member list showing.
  }
}

/** Load (or reload) a group's transcript. Safe to call repeatedly. */
export async function refreshMessages(id: number): Promise<void> {
  const seq = (messageSeq.get(id) ?? 0) + 1;
  messageSeq.set(id, seq);
  try {
    const res = await api.get<{ group: GroupSummary; messages: GroupMessage[] }>(
      `/api/groups/${id}/messages?limit=${MESSAGE_LIMIT}`,
    );
    if (messageSeq.get(id) !== seq) return;
    const rows = res?.messages ?? [];
    const groups = snapshot.groups.some((g) => g.id === id)
      ? snapshot.groups.map((g) => (g.id === id && res?.group ? res.group : g))
      : snapshot.groups;
    set({
      groups,
      messages: { ...snapshot.messages, [id]: rows },
      previews: { ...snapshot.previews, [id]: rows[rows.length - 1] },
      // Anything the server has now echoed back stops being pending.
      pending: { ...snapshot.pending, [id]: reconcile(snapshot.pending[id], rows) },
    });
  } catch {
    // Keep the transcript we have; the next frame or resume tries again.
  }
}

/** Drop optimistic turns the server has confirmed (matched on author + text). */
function reconcile(pending: PendingMessage[] | undefined, rows: GroupMessage[]): PendingMessage[] {
  if (!pending?.length) return [];
  const confirmed = new Set(rows.filter((m) => m.author_kind === "human").map((m) => m.body));
  return pending.filter((p) => !confirmed.has(p.body));
}

/** Tell the store which transcript is on screen (null when none is). */
export function setOpenGroup(id: number | null): void {
  openId = id;
  if (id !== null && snapshot.messages[id] === undefined) void refreshMessages(id);
}

// ── writes ───────────────────────────────────────────────────────────────────

/** Create a group. Throws the backend's message on a refused roster. */
export async function createGroup(name: string, members: string[]): Promise<GroupSummary> {
  const res = await api.post<{ group: GroupSummary }>("/api/groups", { name, members });
  await refreshGroups();
  return res.group;
}

/**
 * Promote the open 1:1 conversation into a group.
 *
 * The bot being talked to leads the roster server-side, so the next message
 * still goes where it was already going. `carryOver` copies a bounded tail of
 * the conversation into the new group's transcript — declining it makes an
 * empty room with the same members, which is occasionally what you want and
 * never what you want by default.
 */
export async function createGroupFromThread(
  agent: string,
  threadId: string,
  members: string[],
  opts: { name?: string; carryOver?: boolean } = {},
): Promise<GroupSummary> {
  const res = await api.post<{ group: GroupSummary }>("/api/groups/from-thread", {
    agent,
    thread_id: threadId,
    members,
    ...(opts.name ? { name: opts.name } : {}),
    ...(opts.carryOver === false ? { carry_over: false } : {}),
  });
  await refreshGroups();
  return res.group;
}

export async function deleteGroup(id: number): Promise<void> {
  await api.del(`/api/groups/${id}`);
  const messages = { ...snapshot.messages };
  const previews = { ...snapshot.previews };
  const activity = { ...snapshot.activity };
  delete messages[id];
  delete previews[id];
  delete activity[id];
  set({ groups: snapshot.groups.filter((g) => g.id !== id), messages, previews, activity });
  void refreshGroups();
}

/**
 * Rename a group. The roster, the transcript and every member's memory of the
 * conversation are untouched — a group's name is a label, and its identity is
 * the numeric id every thread derives from. That is what makes this safe as a
 * right-click on a rail row.
 *
 * Applied locally from the server's copy rather than optimistically: the name
 * that sticks is the one the backend trimmed and stored, and showing anything
 * else for a beat teaches the wrong rule about what a rename accepts.
 */
export async function renameGroup(id: number, name: string): Promise<GroupSummary> {
  const res = await api.patch<{ group: GroupSummary }>(`/api/groups/${id}`, { name });
  applyGroup(res.group);
  return res.group;
}

export async function addMember(id: number, agent: string): Promise<GroupSummary> {
  const res = await api.post<{ group: GroupSummary }>(`/api/groups/${id}/members`, { agent });
  applyGroup(res.group);
  return res.group;
}

export async function removeMember(id: number, agent: string): Promise<GroupSummary> {
  const res = await api.del<{ group: GroupSummary }>(
    `/api/groups/${id}/members/${encodeURIComponent(agent)}`,
  );
  applyGroup(res.group);
  return res.group;
}

/**
 * The team accounts this group could be shared with: active, not an admin,
 * not already in, and holding access to every agent in the roster, the
 * server's own rule, so the picker never offers a name it would refuse.
 */
export async function fetchShareCandidates(id: number): Promise<ShareCandidate[]> {
  const res = await api.get<{ candidates: ShareCandidate[] }>(`/api/groups/${id}/share-candidates`);
  return res?.candidates ?? [];
}

/** Let a team account (id, username or email) read and post in the group. */
export async function shareGroup(id: number, user: string): Promise<GroupSummary> {
  const res = await api.post<{ group: GroupSummary }>(`/api/groups/${id}/shares`, { user });
  applyGroup(res.group);
  return res.group;
}

/**
 * Take a person back out of a group, or leave it yourself (the same call
 * with your own id; the server lets a shared person remove only themselves).
 */
export async function unshareGroup(id: number, userId: string): Promise<GroupSummary> {
  const res = await api.del<{ group: GroupSummary }>(
    `/api/groups/${id}/shares/${encodeURIComponent(userId)}`,
  );
  applyGroup(res.group);
  return res.group;
}

function applyGroup(group: GroupSummary): void {
  set({
    groups: snapshot.groups.some((g) => g.id === group.id)
      ? snapshot.groups.map((g) => (g.id === group.id ? group : g))
      : [group, ...snapshot.groups],
  });
}

let pendingSeq = 0;

/**
 * A turn ended in a way the human should be told about, but the message they
 * sent WAS delivered.
 *
 * The distinction is the whole point: a refusal before the human turn is saved
 * means retyping, so the composer hands the draft back; a reply that failed
 * after it means the message is in the transcript and handing the draft back
 * would invite sending it twice.
 */
export class GroupReplyError extends Error {}

/**
 * Post a human message and render the reply (or replies) as they are written.
 *
 * The human's own turn is echoed locally first — a minute of silence with your
 * own message missing reads as a dropped send, which is exactly what it is
 * not. Then the turn streams:
 *
 *   `start`         — the persisted human message, and who was addressed
 *   `speaker`       — a member begins its turn (a message naming several
 *                     members gives each of them one, in order)
 *   `text`          — deltas growing that member's live bubble
 *   `tool_call` /
 *   `tool_result` /
 *   `subagent_text` — the work behind the reply, kept per message so the
 *                     transcript can show as much of it as the reader asked for
 *   `message`       — the reply, settled into the transcript
 *
 * A backend without the stream endpoint (an older runtime behind a newer
 * dashboard mid-deploy) answers 404/405 before any frame, and the send falls
 * back to the blocking POST — same transcript, just no live words.
 *
 * An `error` frame after `start` is one member's turn failing: the human
 * message is already saved server-side and other members may still answer, so
 * the stream continues and the failure is raised as a `GroupReplyError` at the
 * end — "your message was sent, this reply was not".
 */
export async function sendGroupMessage(id: number, text: string, to = ""): Promise<void> {
  const body = text.trim();
  if (!body) return;
  const group = snapshot.groups.find((g) => g.id === id);
  // Best-effort local guess at who will answer, purely so the "typing" line can
  // name someone; the server decides for real and the `start` frame corrects it.
  // In a shared room a message that names nobody is for the other people, so
  // the guess is nobody and no typing line is drawn.
  const guess =
    to ||
    mentionedMember(body, group?.members ?? []) ||
    (isShared(group) ? "" : group?.members[0] || "");
  const echo: PendingMessage = {
    id: -(++pendingSeq),
    body,
    addressee: guess,
    at: Date.now(),
  };
  set({
    pending: { ...snapshot.pending, [id]: [...(snapshot.pending[id] ?? []), echo] },
    awaiting: { ...snapshot.awaiting, [id]: guess || undefined },
  });
  let failed = false;
  const turnErrors: string[] = [];
  try {
    // The live turn is held here rather than read back out of the snapshot on
    // every frame: tool frames mutate arrays in place (the same accumulation
    // the 1:1 chat uses), and a fan-out replaces this wholesale per speaker.
    let live = liveTurn(guess);
    const publish = () => set({ streaming: { ...snapshot.streaming, [id]: { ...live } } });
    // The turn runs detached server-side (olano/core/runs.py) and this is a view
    // of it, so a dropped socket costs the viewer and not the exchange — which
    // matters more in a group than anywhere else, because several people may be
    // watching one transcript. `cursor` is where a reconnect resumes.
    let cursor = 0;
    let sawTerminal = false;
    const onFrame = (frame: GroupStreamFrame<GroupMessage> & { seq?: number }) => {
        if (typeof frame.seq === "number" && frame.seq > cursor) cursor = frame.seq;
        if (frame.type === "done") sawTerminal = true;
        const author = frame.author || live.author;
        switch (frame.type) {
          case "start":
            if (frame.addressee) {
              live = liveTurn(frame.addressee);
              set({ awaiting: { ...snapshot.awaiting, [id]: frame.addressee } });
            }
            break;
          case "speaker":
            // A new member takes the floor. Its predecessor's work has already
            // been filed against its settled message, so start clean — and put
            // the typing line back, because this member has said nothing yet.
            live = liveTurn(author);
            set({
              streaming: { ...snapshot.streaming, [id]: undefined },
              awaiting: { ...snapshot.awaiting, [id]: author },
            });
            break;
          case "text":
          case "tool_call":
          case "tool_result":
          case "media":
          case "subagent_text":
            if (applyLiveFrame(live, frame)) publish();
            // The live bubble replaces the typing line the moment words exist.
            // Guarded, because this runs per token and an unguarded write would
            // mint a new `awaiting` object — and re-render every subscriber —
            // on every delta of every turn.
            if (frame.type === "text" && frame.text && snapshot.awaiting[id] !== undefined) {
              set({ awaiting: { ...snapshot.awaiting, [id]: undefined } });
            }
            break;
          case "message": {
            // The reply is now a real transcript row, so its work moves from
            // the live turn to that row's id — where the view joins the two.
            const msg = frame.message;
            if (msg && (live.tools.length || live.subs.length)) {
              const forGroup = { ...(snapshot.activity[id] ?? {}) };
              forGroup[msg.id] = { author: live.author, tools: live.tools, subs: live.subs };
              set({ activity: { ...snapshot.activity, [id]: forGroup } });
            }
            live = liveTurn(live.author);
            set({
              streaming: { ...snapshot.streaming, [id]: undefined },
              awaiting: { ...snapshot.awaiting, [id]: undefined },
            });
            break;
          }
          case "error":
            turnErrors.push(frame.detail || "The reply failed.");
            break;
          // `cascade`/`done` need no handling here: the refresh below reads the
          // settled transcript, which those frames already describe.
        }
    };
    await streamPost<GroupStreamFrame<GroupMessage>>(
      `/api/groups/${id}/messages/stream`,
      { text: body, ...(to ? { to } : {}) },
      onFrame,
    );
    // No terminal frame means the connection dropped, not the turn. Rejoin from
    // the cursor, once — a second failure is a real one, and the refresh in the
    // finally below still reconciles against the persisted transcript.
    if (!sawTerminal) {
      try {
        const res = await fetch(`/api/groups/${id}/messages/attach?cursor=${cursor}`, {
          credentials: "include",
          headers: { Accept: "text/event-stream" },
        });
        if (res.ok && res.body) await readSSE<GroupStreamFrame<GroupMessage> & { seq?: number }>(res, onFrame);
      } catch {
        // Still offline. The refresh below shows whatever was persisted.
      }
    }
    // Always surface a turn error, sawText or not: the backend does not
    // persist a half-written reply, so text the person watched being typed is
    // about to vanish on the refresh — vanishing WITH an error is a failed
    // turn, vanishing without one is gaslighting.
    if (turnErrors.length) throw new GroupReplyError(turnErrors.join(" "));
  } catch (e) {
    if (e instanceof StreamRequestError && (e.status === 404 || e.status === 405)) {
      // Older backend: same turn, blocking shape.
      try {
        await api.post(`/api/groups/${id}/messages`, { text: body, ...(to ? { to } : {}) });
      } catch (e2) {
        failed = true;
        throw e2;
      }
    } else {
      failed = e instanceof StreamRequestError; // refused before the human turn saved
      throw e;
    }
  } finally {
    set({
      awaiting: { ...snapshot.awaiting, [id]: undefined },
      streaming: { ...snapshot.streaming, [id]: undefined },
    });
    // The backend persists the human turn BEFORE the model runs, so a failed
    // reply still leaves the message in the transcript — the refresh reconciles
    // the echo away. A request refused before that (a bad addressee, a deleted
    // group — a StreamRequestError) never persisted anything, so the echo has
    // to be dropped here or it sits in the thread forever, looking sent.
    await refreshMessages(id);
    if (failed) {
      set({
        pending: {
          ...snapshot.pending,
          [id]: (snapshot.pending[id] ?? []).filter((p) => p.id !== echo.id),
        },
      });
    }
    // The roster's updated_at moved, which is what orders the rail.
    void refreshGroups();
  }
}

/** First `@name` in the text that names a member — mirrors the backend's rule. */
export function mentionedMember(text: string, members: string[]): string {
  return mentionedMembers(text, members)[0] ?? "";
}

/**
 * Every member named in the draft, in the order they were named and without
 * repeats — which is the order the backend gives them their turns in.
 *
 * The composer says who is about to answer, and for "@a @b what do you two
 * think?" the honest answer is both of them in that order, not just the first.
 */
export function mentionedMembers(text: string, members: string[]): string[] {
  const byLower = new Map(members.map((m) => [m.toLowerCase(), m]));
  const found: string[] = [];
  // Mirror of MENTION_RE in olano/groups/cascade.py — the same rule computed
  // twice, so keep them in step. The last captured character must be a word
  // character: "over to you, @beta." names beta, not "beta.".
  for (const match of text.matchAll(/@([A-Za-z0-9_.-]*[A-Za-z0-9_])/g)) {
    const hit = byLower.get(match[1].toLowerCase());
    if (hit && !found.includes(hit)) found.push(hit);
  }
  return found;
}

// ── live ─────────────────────────────────────────────────────────────────────

function onFrame(frame: { type?: string; data?: unknown }): void {
  if (frame.type !== "group_updated") return;
  const data = (frame.data ?? {}) as { id?: number; kind?: string };
  const id = typeof data.id === "number" ? data.id : null;
  // The roster line-up and its ordering changed on every kind of frame.
  if (listDebounce) clearTimeout(listDebounce);
  listDebounce = setTimeout(() => {
    listDebounce = null;
    void refreshGroups();
  }, 250);
  if (id === null) return;
  // The open transcript is pulled in full; everything else settles for the
  // preview line, which is all the rail draws.
  if (id === openId) void refreshMessages(id);
  else void refreshPreview(id);
}

function start(): void {
  if (unsubStream) return;
  void refreshGroups();
  unsubStream = subscribeStream(onFrame);
  // SSE has no replay, so frames pushed while the tab slept are simply gone.
  unsubResume = subscribeResume((e) => {
    if (!shouldRefetch(e)) return;
    void refreshGroups();
    if (openId !== null) void refreshMessages(openId);
  });
}

function stop(): void {
  unsubStream?.();
  unsubStream = null;
  unsubResume?.();
  unsubResume = null;
  if (listDebounce) {
    clearTimeout(listDebounce);
    listDebounce = null;
  }
}

function subscribeStore(l: () => void): () => void {
  listeners.add(l);
  if (++refCount === 1) start();
  return () => {
    listeners.delete(l);
    if (--refCount === 0) stop();
  };
}

// A subscription that never fires — what a caller gets while groups are off, so
// the hook below can be called unconditionally without opening a connection or
// issuing a request on a deployment that has the feature disabled.
function noopSubscribe(): () => void {
  return () => {};
}

/**
 * Whether this deployment serves group chats.
 *
 * TRI-STATE: `undefined` while /api/status is still in flight, and that must
 * NOT read as false — a flash of "no groups" followed by a rail that grows a
 * section is worse than a beat of nothing. The feature is off by default, so
 * every group surface renders only on an explicit `true`.
 *
 * The key is `groups_enabled`, matching `playground_enabled` / `stt_enabled` /
 * `terminal_enabled` — the convention already in `_shared_capabilities()`. An
 * earlier draft accepted a bare `groups` as well; that was dropped rather than
 * kept as a kindness, because two spellings for one flag means the next person
 * to add a group surface has to discover which one the backend actually sends,
 * and a fallback that never fires is indistinguishable from one that is load
 * bearing.
 */
export function useGroupsEnabled(): boolean | undefined {
  const caps = useCapabilities();
  if (caps === undefined) return undefined;
  return caps.groups_enabled === true;
}

/** The live group snapshot. Fetches nothing while `enabled` is false. */
export function useGroups(enabled: boolean): GroupsSnapshot {
  return useSyncExternalStore(
    enabled ? subscribeStore : noopSubscribe,
    enabled ? () => snapshot : () => EMPTY,
    () => EMPTY,
  );
}
