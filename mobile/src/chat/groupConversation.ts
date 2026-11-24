/**
 * One open group chat: its transcript, the person's messages on their way,
 * and the reply being written right now.
 *
 * A message to a group goes to the members it names with @ (or the first
 * member), and each answers in turn, seeing the replies before its own; the
 * stream says who is speaking (`speaker`) and every frame after that names
 * its author. The turn runs on the deployment and the stream is a view of it
 * (olano/core/runs.py), so a dropped connection rejoins from the last frame
 * seen. Afterwards the agents may hand the conversation to each other by
 * themselves (a "cascade"); those replies arrive as `group_updated` events on
 * the deployment's event stream, and the transcript is read again.
 *
 * The transcript (`GET /api/groups/{id}/messages`) carries words, files and
 * where tool steps went, not the steps themselves, so the steps of a reply
 * watched live are kept here by message id for as long as the app runs.
 */

import { ApiError } from "../net/api.ts";
import type { CascadeState, GroupMessage, GroupSummary } from "../net/types.ts";
import type { OpenStream } from "../net/transport.ts";
import { messageOf } from "../native/shell.ts";
import { applyLiveFrame, liveTurn, type GroupLiveTurn, type GroupStreamFrame, type ToolCall } from "../shared/dashboard.ts";
import { currentApi } from "../state/session.ts";
import { createStore, type Store } from "../state/store.ts";
import { addressees } from "./groups.ts";

/** A message the person sent that the deployment has not echoed back yet. */
export interface PendingGroupMessage {
  key: string;
  body: string;
  addressees: string[];
  ts: number;
}

export interface GroupConversationState {
  id: number;
  group: GroupSummary | null;
  messages: GroupMessage[];
  pending: PendingGroupMessage[];
  /** The member writing right now, before their first word. */
  awaiting: string | null;
  /** The reply being written: its author, words so far and steps. */
  live: GroupLiveTurn | null;
  /** Steps of replies watched live, by the message they produced. */
  steps: Record<number, ToolCall[]>;
  /** A message is being answered (the stream is open or rejoining). */
  busy: boolean;
  loading: boolean;
  loadError: string | null;
  sendError: string | null;
  /** Agents answering each other after the reply. */
  cascade: CascadeState | null;
}

/** Same ceiling as the runtime's. */
const MESSAGE_LIMIT = 200;

const open = new Map<number, GroupConversation>();

export function groupConversationFor(id: number): GroupConversation {
  let conversation = open.get(id);
  if (!conversation) {
    conversation = new GroupConversation(id);
    open.set(id, conversation);
  }
  return conversation;
}

export function findGroupConversation(id: number): GroupConversation | undefined {
  return open.get(id);
}

export function allGroupConversations(): GroupConversation[] {
  return [...open.values()];
}

export function closeAllGroupConversations(): void {
  for (const conversation of open.values()) conversation.dispose();
  open.clear();
}

let pendingSeq = 0;

export class GroupConversation {
  readonly store: Store<GroupConversationState>;
  private stream: OpenStream | null = null;
  private cursor = 0;
  private sawDone = false;
  private turnErrors: string[] = [];
  private parked = false;
  private disposed = false;
  private loadSeq = 0;
  private flushQueued = false;
  private live: GroupLiveTurn | null = null;

  constructor(id: number) {
    this.store = createStore<GroupConversationState>({
      id,
      group: null,
      messages: [],
      pending: [],
      awaiting: null,
      live: null,
      steps: {},
      busy: false,
      loading: true,
      loadError: null,
      sendError: null,
      cascade: null,
    });
  }

  get state(): GroupConversationState {
    return this.store.get();
  }

  /** Reads the transcript (and whether agents are still answering each other). */
  async load(): Promise<void> {
    if (this.disposed) return;
    const seq = ++this.loadSeq;
    const { id } = this.state;
    this.store.set((s) => ({ ...s, loading: !s.messages.length && !s.group, loadError: null }));
    try {
      const res = await currentApi().groupMessages(id, MESSAGE_LIMIT);
      if (seq !== this.loadSeq || this.disposed) return;
      const messages = res.messages ?? [];
      this.store.set((s) => ({
        ...s,
        group: res.group ?? s.group,
        messages,
        pending: reconcile(s.pending, messages),
        loading: false,
      }));
      void this.readCascade();
    } catch (error) {
      if (seq !== this.loadSeq || this.disposed) return;
      const gone = error instanceof ApiError && error.status === 404;
      this.store.set((s) => ({
        ...s,
        loading: false,
        loadError: gone ? "This group chat isn't on the deployment any more." : messageOf(error),
      }));
    }
  }

  private async readCascade(): Promise<void> {
    try {
      const cascade = await currentApi().groupCascade(this.state.id);
      if (!this.disposed) this.store.set((s) => ({ ...s, cascade: cascade?.running ? cascade : null }));
    } catch {
      // Older runtimes have no cascade endpoint: nothing to show.
    }
  }

  /** Sends a message; replies stream in. False when the deployment refused it before saving it. */
  async send(text: string): Promise<boolean> {
    const body = text.trim();
    if (!body || this.state.busy) return false;
    const members = this.state.group?.members ?? [];
    const to = addressees(body, members);
    const echo: PendingGroupMessage = { key: `p${++pendingSeq}`, body, addressees: to, ts: Date.now() };
    this.cursor = 0;
    this.sawDone = false;
    this.turnErrors = [];
    this.live = liveTurn(to[0] ?? "");
    this.store.set((s) => ({
      ...s,
      pending: [...s.pending, echo],
      awaiting: to[0] ?? null,
      live: null,
      busy: true,
      sendError: null,
    }));
    try {
      this.stream = await currentApi().groupStream({ kind: "send", id: this.state.id, text: body }, (frame) => this.onFrame(frame));
      void this.follow(this.stream);
      return true;
    } catch (error) {
      const busyHere = error instanceof ApiError && error.status === 409;
      this.store.set((s) => ({
        ...s,
        // A refusal saved nothing; a reply already running did save the message.
        pending: busyHere ? s.pending : s.pending.filter((p) => p.key !== echo.key),
        awaiting: null,
        busy: false,
        sendError: busyHere ? "An agent is still answering the last message here. Yours will be seen next time." : messageOf(error, "The message could not be sent."),
      }));
      if (busyHere) void this.load();
      // A message saved behind a running reply stays sent; a refused one goes back to the composer.
      return busyHere;
    }
  }

  private async follow(stream: OpenStream): Promise<void> {
    const end = await stream.done;
    if (this.stream !== stream) return;
    this.stream = null;
    if (this.parked || this.disposed) return;
    if (!this.sawDone) {
      // The connection dropped, not the turn: rejoin where it left off.
      window.setTimeout(() => void this.attach(), end.error && !end.cancelled ? 1500 : 0);
      return;
    }
    this.finish();
  }

  /** Rejoins the turn in flight from the last frame seen; when it is over, reads the transcript. */
  private async attach(): Promise<void> {
    if (this.stream || this.disposed) return;
    try {
      this.stream = await currentApi().groupStream({ kind: "attach", id: this.state.id, cursor: this.cursor }, (frame) => this.onFrame(frame));
      void this.follow(this.stream);
    } catch {
      this.stream = null;
      this.finish();
    }
  }

  private finish(): void {
    this.live = null;
    const errors = this.turnErrors;
    this.turnErrors = [];
    this.store.set((s) => ({
      ...s,
      busy: false,
      awaiting: null,
      live: null,
      sendError: errors.length ? `Your message was sent, but a reply failed: ${errors.join(" ")}` : s.sendError,
    }));
    void this.load();
  }

  private onFrame(frame: GroupStreamFrame<GroupMessage> & { seq?: number }): void {
    if (typeof frame.seq === "number" && frame.seq > this.cursor) this.cursor = frame.seq;
    const author = frame.author || this.live?.author || "";
    switch (frame.type) {
      case "start": {
        const saved = (frame as { message?: GroupMessage }).message;
        const to = frame.addressees?.length ? frame.addressees : frame.addressee ? [frame.addressee] : [];
        this.live = liveTurn(to[0] ?? author);
        this.store.set((s) => ({
          ...s,
          awaiting: to[0] ?? s.awaiting,
          // The person's message, as the deployment saved it, replaces its echo.
          messages: saved && !s.messages.some((m) => m.id === saved.id) ? [...s.messages, saved] : s.messages,
          pending: saved ? s.pending.filter((p) => p.body !== saved.body) : s.pending,
        }));
        return;
      }
      case "speaker":
        this.live = liveTurn(author);
        this.store.set((s) => ({ ...s, live: null, awaiting: author }));
        return;
      case "text":
      case "tool_call":
      case "tool_result":
      case "media":
      case "subagent_text":
        if (!this.live) this.live = liveTurn(author);
        if (applyLiveFrame(this.live, frame)) this.scheduleFlush();
        return;
      case "message": {
        const saved = frame.message;
        const live = this.live;
        this.live = liveTurn(author);
        this.store.set((s) => ({
          ...s,
          live: null,
          awaiting: null,
          messages: saved && !s.messages.some((m) => m.id === saved.id) ? [...s.messages, saved] : s.messages,
          steps: saved && live?.tools.length ? { ...s.steps, [saved.id]: live.tools.map((t) => ({ ...t, hasResult: true })) } : s.steps,
        }));
        return;
      }
      case "error":
        this.turnErrors.push(frame.detail || "A reply failed.");
        return;
      case "cascade": {
        const c = frame as unknown as { running?: boolean; hops?: number; max_hops?: number };
        this.store.set((s) => ({ ...s, cascade: c.running ? { running: true, hops: c.hops ?? 0, max_hops: c.max_hops } : null }));
        return;
      }
      case "done":
        this.sawDone = true;
        return;
      default:
        if ((frame as { type: string }).type === "resync") {
          // Too far behind to replay: the transcript has the outcome.
          this.sawDone = true;
          this.stream?.cancel();
        }
    }
  }

  /** Words arrive per token: redraw at most once per animation frame. */
  private scheduleFlush(): void {
    if (this.flushQueued) return;
    this.flushQueued = true;
    const run = () => {
      this.flushQueued = false;
      const live = this.live;
      if (!live) return;
      const copy: GroupLiveTurn = { ...live, tools: live.tools.map((t) => ({ ...t })), media: live.media ? [...live.media] : undefined };
      this.store.set((s) => ({ ...s, live: copy, awaiting: copy.body ? null : s.awaiting }));
    };
    if (typeof requestAnimationFrame === "function" && !document.hidden) requestAnimationFrame(run);
    else setTimeout(run, 16);
  }

  /** Stops the agents answering each other (a reply being written finishes first). */
  async stopCascade(): Promise<void> {
    this.store.set((s) => (s.cascade ? { ...s, cascade: { ...s.cascade, stopping: true } } : s));
    try {
      await currentApi().stopGroupCascade(this.state.id);
    } catch {
      // It may have just settled by itself.
    }
    void this.readCascade();
  }

  /** The group changed somewhere else (a new message, an agent answering an agent, a rename). */
  onUpdated(kind: string): void {
    if (this.disposed) return;
    if (kind === "delete") {
      this.store.set((s) => ({ ...s, loadError: "This group chat was deleted." }));
      return;
    }
    if (kind === "cascade_end") this.store.set((s) => ({ ...s, cascade: null }));
    if (kind === "cascade_start") this.store.set((s) => ({ ...s, cascade: { running: true, hops: 0 } }));
    // While a reply streams, the transcript is read once it is done.
    if (!this.stream) void this.load();
  }

  /** The group's own details changed (renamed, a member added or removed). */
  setGroup(group: GroupSummary): void {
    this.store.set((s) => ({ ...s, group }));
  }

  park(): void {
    if (!this.stream) return;
    this.parked = true;
    const stream = this.stream;
    this.stream = null;
    stream.cancel();
  }

  resume(): void {
    if (this.disposed) return;
    const wasParked = this.parked;
    this.parked = false;
    if (wasParked && this.state.busy) void this.attach();
    else if (!this.stream) void this.load();
  }

  dispose(): void {
    this.disposed = true;
    this.stream?.cancel();
    this.stream = null;
  }
}

/** Echoes the deployment has now saved stop being pending (matched on their words). */
function reconcile(pending: PendingGroupMessage[], rows: GroupMessage[]): PendingGroupMessage[] {
  if (!pending.length) return pending;
  const saved = new Set(rows.filter((m) => m.author_kind === "human").map((m) => m.body.trim()));
  return pending.filter((p) => !saved.has(p.body));
}
