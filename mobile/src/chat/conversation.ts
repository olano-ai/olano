/**
 * One open conversation: its messages, the turn in flight and the stream
 * that carries it.
 *
 * A turn runs on the deployment, not in the app (olano/core/runs.py): the
 * stream is only a view of it. So leaving the screen keeps the conversation
 * (and its stream) alive, the app going to the background closes the stream
 * on purpose (`park`), and coming back rejoins from the last frame it saw
 * (`resume`, `GET /api/chat/attach?cursor=N`). Closing the stream while the
 * phone is in a pocket is also what lets the deployment send a push
 * notification when the reply lands: it only notifies when nobody watches.
 */

import { ApiError } from "../net/api.ts";
import type { OpenStream } from "../net/transport.ts";
import { messageOf } from "../native/shell.ts";
import { speechEngine } from "../native/speech.ts";
import { currentApi } from "../state/session.ts";
import { createStore, type Store } from "../state/store.ts";
import { readsAloud, voicePrefs } from "../state/voicePrefs.ts";
import { toWire, type ChatFrame, type FileAttachment } from "../shared/dashboard.ts";
import { readAloud } from "../ui/readAloud.ts";
import { PLAN_APPROVED, saveMode, savedMode, type ChatMode } from "./modes.ts";
import { applyFrame, foldTranscript, newAssistant, newUser, type ChatMessage, type ConnectResult } from "./model.ts";
import { hasMore, PAGE_SIZE, withNewest, withOlder, type History } from "./pages.ts";
import { wireAttachment, type PreparedPhoto } from "./photos.ts";
import { splitVoiceNotes, voiceAttachment, voiceTranscriptMessage, type PreparedVoice } from "./voice.ts";
import { transcribeOn } from "../state/appLayout.ts";
import { appLayout } from "../state/layoutStore.ts";
import { unavailableFromError, type Unavailable } from "../agents/health.ts";
import { invalidate, markStale, patchResource } from "../state/resources.ts";
import type { ThreadPage } from "../net/types.ts";
import { withStartedThread } from "./threadPages.ts";

let phoneListens: Promise<boolean> | null = null;

/**
 * Whether voice notes are heard on this phone: it can (an iPhone, not
 * refused permission) and the person has not turned it off in Settings.
 * What the phone can do is asked once a session.
 */
async function hearsOnPhone(): Promise<boolean> {
  // The person's choice where the admin shows the switch, else the deployment's default.
  if (transcribeOn(voicePrefs.get(), appLayout.get()) !== "phone") return false;
  phoneListens ??= speechEngine()
    .recognitionStatus()
    .then((status) => status.available && status.permission !== "denied" && status.permission !== "restricted")
    .catch(() => false);
  return phoneListens;
}

/**
 * A voice note's words, heard on the phone, or null when it heard none or
 * could not (no permission after all, a language it lacks), in which case the
 * recording goes to the deployment to be transcribed.
 */
async function wordsOnPhone(voice: PreparedVoice): Promise<string | null> {
  try {
    const heard = await speechEngine().transcribe({ data: voice.data, durationMs: voice.durationMs });
    const words = (heard.text || "").replace(/\s+/g, " ").trim();
    return words || null;
  } catch {
    return null;
  }
}

export interface ConversationState {
  agent: string;
  /** Null for a new chat until the deployment names it (the `start` frame). */
  threadId: string | null;
  messages: ChatMessage[];
  loading: boolean;
  loadError: string | null;
  /** A turn is running on the deployment and this conversation follows it. */
  busy: boolean;
  /** The deployment refused the last send because the agent is not running (`agent_not_running`). */
  unavailable: Unavailable | null;
  /** The last send failed before a turn started. */
  sendError: string | null;
  /**
   * This conversation's model and reasoning level ("" = the agent's own),
   * sent with every message (chat/models.ts).
   */
  model: string;
  reasoningEffort: string;
  /**
   * Messages typed while the agent was working: a `steer` one was handed to
   * the running turn (the agent reads it at its next step), a `queue` one
   * sends as the next message when the turn ends.
   */
  queue: QueuedMessage[];
  /** How the agent goes about this conversation's requests (chat/modes.ts). */
  mode: ChatMode;
  /** There are earlier messages than the ones held (chat/pages.ts). */
  hasOlder: boolean;
  loadingOlder: boolean;
  /** Reading the earlier messages failed; scrolling no longer retries, a tap does. */
  olderError: string | null;
  /** Earlier messages were read, up to the first one: the top says so. */
  reachedStart: boolean;
}

export interface QueuedMessage {
  /** Also the steer id the deployment reports back in its `steer` frame. */
  id: string;
  text: string;
  kind: "steer" | "queue";
}

/** A steer id inside the deployment's `[A-Za-z0-9_-]{8,64}` rule. */
function newSteerId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
}

const open = new Map<string, Conversation>();
const keyOf = (agent: string, threadId: string) => `${agent}\u0000${threadId}`;

/** The conversation for a thread, kept while the app runs so going back and forth is instant. */
export function conversationFor(agent: string, threadId: string): Conversation {
  const key = keyOf(agent, threadId);
  let conversation = open.get(key);
  if (!conversation) {
    conversation = new Conversation(agent, threadId);
    open.set(key, conversation);
  }
  return conversation;
}

/** The conversation for a thread if one is open, without creating it. */
export function findConversation(agent: string, threadId: string): Conversation | undefined {
  return open.get(keyOf(agent, threadId));
}

/** A fresh, unsaved chat. It registers itself under its thread id once it has one. */
export function newConversation(agent: string): Conversation {
  return new Conversation(agent, null);
}

export function allConversations(): Conversation[] {
  return [...open.values()];
}

/** Forget every conversation (sign out, switch deployment). */
export function closeAllConversations(): void {
  for (const conversation of open.values()) conversation.dispose();
  open.clear();
}

export class Conversation {
  readonly store: Store<ConversationState>;
  private stream: OpenStream | null = null;
  private live: ChatMessage | null = null;
  private runId: string | undefined;
  private turnId: string | undefined;
  private cursor = 0;
  private parked = false;
  private flushQueued = false;
  private loaded = false;
  private disposed = false;
  /** The voice note just sent, until its transcript has been read back. */
  private awaitingTranscript: string | null = null;
  /** The turn in flight was asked with a voice note: its reply may be read aloud. */
  private askedByVoice = false;
  /** Set once the person picks a model here; a reload then leaves it alone. */
  private modelPicked = false;
  /** The pages read so far (chat/pages.ts); null before the first. */
  private history: History | null = null;
  private olderInFlight = false;
  /** The turn in flight lost frames off the deployment's buffer: reread the transcript once it ends. */
  private resynced = false;
  /** Consecutive failed catch-up reads, for the retry's backoff. */
  private catchUpFailures = 0;
  private catchUpTimer: ReturnType<typeof setTimeout> | null = null;
  /** Hands-free mode is on: every reply is read aloud, whatever Settings says. */
  readEveryReply = false;

  constructor(agent: string, threadId: string | null) {
    this.store = createStore<ConversationState>({
      agent,
      threadId,
      messages: [],
      loading: threadId !== null,
      loadError: null,
      busy: false,
      sendError: null,
      unavailable: null,
      model: "",
      reasoningEffort: "",
      queue: [],
      mode: savedMode(agent),
      hasOlder: false,
      loadingOlder: false,
      olderError: null,
      reachedStart: false,
    });
  }

  /** Auto, plan first, or ask before each step; remembered for this agent on this phone. */
  setMode(mode: ChatMode): void {
    saveMode(this.state.agent, mode);
    this.store.set((s) => ({ ...s, mode }));
  }

  /** "Approve & run" under a drafted plan: carry it out, then back to auto. */
  approvePlan(): Promise<boolean> {
    this.setMode("auto");
    this.store.set((s) => ({ ...s, messages: s.messages.map((m) => (m.planProposal ? { ...m, planProposal: false } : m)) }));
    return this.send(PLAN_APPROVED, [], undefined, "act");
  }

  /** The agent was started (or the list says it runs again): drop the refusal banner. */
  clearUnavailable(): void {
    if (this.state.unavailable) this.store.set((s) => ({ ...s, unavailable: null }));
  }

  /** Picks this conversation's model ("" = the agent's own) and reasoning level. */
  setModel(model: string, reasoningEffort: string): void {
    this.modelPicked = true;
    this.store.set((s) => ({ ...s, model, reasoningEffort }));
  }

  get state(): ConversationState {
    return this.store.get();
  }

  /**
   * Loads the transcript once (or again with `force`), and rejoins a turn
   * that is still running. `keepStream` rereads it under a stream that stays
   * open (after a `resync`): the stream goes on feeding the reply in flight.
   */
  async load(force = false, keepStream = false): Promise<void> {
    const { agent, threadId } = this.state;
    if (!threadId || (this.loaded && !force) || this.disposed) return;
    if (this.stream && force && !keepStream) return; // A live stream already keeps it current.
    this.store.set((s) => ({ ...s, loading: !s.messages.length, loadError: null }));
    try {
      // The newest page, without the tool output the app never draws; the
      // pages above it load as the person scrolls up (`loadOlder`).
      const detail = await currentApi().thread(agent, threadId, { limit: PAGE_SIZE, toolOutput: false });
      if (this.disposed) return;
      const history = withNewest(this.history, detail, foldTranscript);
      this.history = history;
      let messages = foldTranscript(history.newestRows);
      this.loaded = true;
      this.store.set((s) => ({
        ...s,
        hasOlder: hasMore(history),
        reachedStart: s.reachedStart && !hasMore(history),
        olderError: hasMore(history) ? s.olderError : null,
      }));
      // Continue on the model the conversation last ran on, unless one was
      // picked here since.
      if (!this.modelPicked) {
        this.store.set((s) => ({
          ...s,
          model: (detail.model || "").trim(),
          reasoningEffort: (detail.reasoning_effort || "").trim(),
        }));
      }
      const run = detail.run;
      this.catchUpFailures = 0;
      if (keepStream && this.live) {
        // The reply in flight carries on in `live`, under the transcript.
        const live = this.live;
        while (messages.length && messages[messages.length - 1].role !== "user") messages = messages.slice(0, -1);
        this.store.set((s) => ({ ...s, messages: [...history.older, ...messages, live], loading: false }));
      } else if (run?.running && !this.stream) {
        // The reply in flight is only in the run's buffer: replay it from the
        // start into a fresh message, after the turn's opening message.
        while (messages.length && messages[messages.length - 1].role !== "user") messages = messages.slice(0, -1);
        this.store.set((s) => ({ ...s, messages: [...history.older, ...messages], loading: false }));
        this.runId = run.run_id;
        void this.attach(0);
      } else if (!this.stream) {
        this.store.set((s) => ({ ...s, messages: [...history.older, ...messages], loading: false, busy: false }));
      }
    } catch (error) {
      if (this.disposed) return;
      if (error instanceof ApiError && error.status === 404 && threadId.startsWith("cortex:")) {
        // Not written yet: a conversation the app named itself ("Talk it over"
        // on a Cortex item uses `cortex:<id>`) exists from its first message.
        // Any other thread came from a list, so its absence is worth saying.
        this.loaded = true;
        this.store.set((s) => ({ ...s, loading: false, loadError: null }));
        return;
      }
      if (error instanceof ApiError && error.status === 404) {
        this.store.set((s) => ({
          ...s,
          loading: false,
          loadError: "This conversation isn't on the deployment any more. It may have been deleted.",
        }));
        return;
      }
      this.store.set((s) => ({ ...s, loading: false, loadError: messageOf(error) }));
      // A refresh that failed (the first request after the phone wakes often
      // meets a dead pooled connection) is tried again, not left stale.
      if (force) this.scheduleCatchUp();
    }
  }

  /** Rereads the transcript after a failed catch-up: 2 s, 4 s, ... up to 30 s apart. */
  private scheduleCatchUp(): void {
    if (this.disposed || this.parked || this.catchUpTimer) return;
    const delay = Math.min(30000, 2000 * 2 ** this.catchUpFailures);
    this.catchUpFailures += 1;
    this.catchUpTimer = setTimeout(() => {
      this.catchUpTimer = null;
      if (!this.disposed && !this.parked && !this.stream) void this.load(true);
    }, delay);
  }

  /**
   * Sends a message, with photos or a voice note if any. The message shows at
   * once, its photos and recording from the phone's own copies; it is marked
   * as sending until the deployment has taken it. On failure it is taken back
   * out, so the screen can return the words and photos to the composer.
   */
  async send(
    text: string,
    photos: PreparedPhoto[] = [],
    voice?: PreparedVoice,
    modeOverride?: string,
    files: FileAttachment[] = [],
  ): Promise<boolean> {
    const body = text.trim();
    if ((!body && !photos.length && !voice && !files.length) || this.state.busy) return false;
    // A voice note on its own is heard on the phone first, when it can be.
    const hearOnPhone = !!voice && !body && !photos.length && (await hearsOnPhone());
    if (this.state.busy || this.disposed) return false;
    const { agent, threadId } = this.state;
    const user = newUser(
      body,
      Date.now(),
      photos.map((p, i) => ({ key: `${p.url}#${i}`, name: p.name, src: p.url, width: p.width, height: p.height })),
      voice ? [{ key: voice.url, name: voice.name, src: voice.url, durationMs: voice.durationMs, peaks: voice.peaks }] : undefined,
    );
    user.sending = photos.length > 0 || !!voice || files.length > 0;
    if (files.length) user.sentFiles = files.map((f) => ({ name: f.name, size: f.size }));
    if (hearOnPhone) user.transcribing = true;
    this.awaitingTranscript = voice && !hearOnPhone ? user.key : null;
    this.askedByVoice = !!voice;
    const live = newAssistant(Date.now());
    live.streaming = true;
    this.live = live;
    this.cursor = 0;
    // The previous turn's ids would name a finished run the deployment still
    // holds for five minutes: a rejoin before this turn's `start` frame then
    // replayed the OLD reply under the new message and stopped there. Without
    // an id the deployment serves whatever is running on the thread.
    this.runId = undefined;
    this.turnId = undefined;
    this.resynced = false;
    this.store.set((s) => ({ ...s, messages: [...s.messages, user, live], busy: true, sendError: null, unavailable: null }));
    const update = (change: Partial<ChatMessage>) =>
      this.store.set((s) => ({ ...s, messages: s.messages.map((m) => (m.key === user.key ? { ...m, ...change } : m)) }));
    let message = body;
    let sendVoice = voice;
    if (voice && hearOnPhone) {
      const words = await wordsOnPhone(voice);
      if (this.disposed) return false;
      if (words) {
        // The words go instead of the recording: no upload, and nothing for
        // the deployment to transcribe before the agent can start.
        message = voiceTranscriptMessage(words);
        sendVoice = undefined;
        update({ transcribing: false, transcripts: [words] });
      } else {
        this.awaitingTranscript = user.key;
        update({ transcribing: false });
      }
    }
    // `client` labels the conversation MOBILE on the deployment (older ones ignore it).
    const request: Record<string, unknown> = { agent, message, thread_id: threadId, mode: modeOverride ?? this.state.mode, client: "mobile" };
    if (this.state.model) request.model = this.state.model;
    if (this.state.reasoningEffort) request.reasoning_effort = this.state.reasoningEffort;
    const attachments = [
      ...photos.map(wireAttachment),
      ...(sendVoice ? [voiceAttachment(sendVoice)] : []),
      ...(toWire(files) ?? []),
    ];
    if (attachments.length) request.attachments = attachments;
    try {
      const stream = await currentApi().chatStream({ kind: "send", body: request }, (frame) => this.onFrame(frame));
      if (user.sending) update({ sending: false });
      // A new chat learns its thread from the stream's first frame; without
      // one there is nothing to rejoin, so that stream is kept.
      if (this.disposed || (this.parked && this.state.threadId)) {
        // The app left while the deployment was taking the message: the turn
        // runs without us, and `resume` rejoins it from the cursor. A stream
        // kept open here would be cut by the OS mid-reply and, still held,
        // block every later refresh of this conversation.
        stream.cancel();
        return true;
      }
      this.stream = stream;
      void this.follow(stream);
      return true;
    } catch (error) {
      const dropped = (m: ChatMessage) => m !== live && m.key !== user.key;
      this.live = null;
      const refusal = error instanceof ApiError && error.status === 409 ? unavailableFromError(this.state.agent, error.detail) : null;
      if (refusal) {
        // Stopped, failed to start or held back: a banner says so (and the
        // agent list is reread, so every screen agrees); the words go back
        // to the composer rather than into a red line under it.
        this.store.set((s) => ({ ...s, messages: s.messages.filter(dropped), busy: false, sendError: null, unavailable: refusal }));
        invalidate("agents");
        return false;
      }
      if (error instanceof ApiError && error.status === 409 && error.code === "run_in_progress") {
        // Another device (or the scheduler) is mid-turn here: watch that turn instead.
        this.store.set((s) => ({ ...s, messages: s.messages.filter(dropped), sendError: "The agent is still working on the last message." }));
        void this.load(true);
        return false;
      }
      this.store.set((s) => ({
        ...s,
        messages: s.messages.filter(dropped),
        busy: false,
        sendError: messageOf(error, "The message could not be sent."),
      }));
      return false;
    }
  }

  /** Rejoins the running turn from `cursor`. */
  async attach(cursor: number): Promise<void> {
    const { agent, threadId } = this.state;
    if (!threadId || this.stream || this.disposed) return;
    if (!this.live || cursor === 0) {
      // Replaying from the first frame rebuilds the reply from nothing.
      const stale = this.live;
      const live = newAssistant(Date.now());
      live.streaming = true;
      this.live = live;
      this.store.set((s) => ({ ...s, messages: [...s.messages.filter((m) => m.key !== stale?.key), live], busy: true }));
    } else {
      this.store.set((s) => ({ ...s, busy: true }));
    }
    this.cursor = cursor;
    try {
      const stream = await currentApi().chatStream(
        { kind: "attach", agent, threadId, cursor, runId: this.runId },
        (frame) => this.onFrame(frame),
      );
      if (this.disposed || this.parked || this.stream) {
        // Left (or rejoined some other way) while this one was opening.
        stream.cancel();
        return;
      }
      this.stream = stream;
      void this.follow(stream);
    } catch {
      // The run finished (404) or the network is gone: the transcript has the
      // outcome. The half-drawn reply stops looking live, and the conversation
      // stays one a later refresh may update (`loaded` is left as it was, so
      // `refreshIfIdle` still answers a `thread_updated` event).
      const stale = this.live;
      this.stream = null;
      this.live = null;
      this.store.set((s) => ({
        ...s,
        busy: false,
        messages: stale ? s.messages.map((m) => (m.key === stale.key ? { ...m, streaming: false } : m)) : s.messages,
      }));
      void this.load(true);
    }
  }

  private async follow(stream: OpenStream): Promise<void> {
    const end = await stream.done;
    if (this.stream !== stream) return;
    this.stream = null;
    this.flush();
    if (this.parked || this.disposed) return;
    const live = this.live;
    if (live?.streaming) {
      // The stream ended without `done`: the connection dropped mid-turn.
      // The run is still going; rejoin where we left off.
      if (end.error && !end.cancelled) {
        setTimeout(() => void this.attach(this.cursor), 1500);
      } else {
        void this.attach(this.cursor);
      }
      return;
    }
    this.live = null;
    this.store.set((s) => ({ ...s, busy: false }));
    if (this.resynced) {
      // Part of this reply was never streamed here: the transcript has all of it.
      this.resynced = false;
      void this.load(true);
    }
    this.drain();
  }

  /** The turn ended: what was queued (or steered too late to be read) goes next. */
  private drain(): void {
    const [next, ...rest] = this.state.queue;
    if (!next || this.state.busy || this.disposed || this.parked) return;
    this.store.set((s) => ({ ...s, queue: rest }));
    void this.send(next.text);
  }

  /**
   * A message typed while the agent works. `steer` hands it to the running
   * turn (the agent reads it at its next step, after the tools in flight);
   * `queue` keeps it for when the turn ends. A steer that arrives as the turn
   * ends stays as a queued message, so nothing typed is lost.
   */
  async addWhileWorking(text: string, kind: "steer" | "queue"): Promise<void> {
    const body = text.trim();
    const { agent, threadId } = this.state;
    if (!body) return;
    const item: QueuedMessage = { id: newSteerId(), text: body, kind };
    this.store.set((s) => ({ ...s, queue: [...s.queue, item] }));
    if (kind === "queue") return;
    if (!threadId || !this.state.busy) {
      this.requeue(item.id);
      return;
    }
    try {
      await currentApi().steer(agent, threadId, body, this.turnId, item.id);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        this.requeue(item.id);
        return;
      }
      this.store.set((s) => ({ ...s, queue: s.queue.filter((q) => q.id !== item.id), sendError: messageOf(error, "The message could not be sent.") }));
    }
  }

  private requeue(id: string): void {
    this.store.set((s) => ({ ...s, queue: s.queue.map((q) => (q.id === id ? { ...q, kind: "queue" as const } : q)) }));
    if (!this.state.busy) this.drain();
  }

  /** Takes a waiting message back (Cancel, or Edit to change it). Returns its words. */
  takeBack(id: string): string {
    const item = this.state.queue.find((q) => q.id === id);
    if (!item) return "";
    this.store.set((s) => ({ ...s, queue: s.queue.filter((q) => q.id !== id) }));
    const { agent, threadId } = this.state;
    if (item.kind === "steer" && threadId) void currentApi().cancelSteer(agent, threadId, id).catch(() => undefined);
    return item.text;
  }

  /** The agent just read a steered message: it joins the transcript, and the reply carries on below it. */
  private steerRead(frame: { id?: string; text?: string }): void {
    const live = this.live;
    if (!live) return;
    const queue = this.state.queue;
    let idx = queue.findIndex((q) => q.kind === "steer" && q.id === frame.id);
    if (idx < 0) idx = queue.findIndex((q) => q.kind === "steer");
    const text = frame.text || (idx >= 0 ? queue[idx].text : "");
    this.flush();
    const done: ChatMessage = { ...live, streaming: false, tools: live.tools.map((t) => ({ ...t })) };
    const aside = newUser(text, Date.now());
    aside.steered = true;
    const next = newAssistant(Date.now());
    next.streaming = true;
    this.live = next;
    this.store.set((s) => ({
      ...s,
      queue: idx < 0 ? s.queue : s.queue.filter((_, i) => i !== idx),
      messages: [...s.messages.map((m) => (m.key === live.key ? done : m)), aside, next],
    }));
  }

  private onFrame(frame: ChatFrame): void {
    if (typeof frame.seq === "number") this.cursor = Math.max(this.cursor, frame.seq);
    const live = this.live;
    if (!live) return;
    if ((frame as { type?: string }).type === "steer") {
      this.steerRead(frame as { id?: string; text?: string });
      return;
    }
    const effect = applyFrame(live, frame);
    switch (effect.kind) {
      case "start":
        this.runId = effect.runId;
        this.turnId = effect.turnId;
        this.adoptThread(effect.threadId);
        // Not read here: `start` goes out before the runtime has transcribed
        // the voice note and written the message, so a read now finds the
        // previous message and showed its transcript under this note. The
        // transcript is read at `done`, and a reload has it either way.
        break;
      case "thread_switch":
        this.adoptThread(effect.threadId);
        break;
      case "resync": {
        // The frames since our cursor are gone from the deployment's buffer,
        // but the stream goes on with the live tail. It used to be closed
        // and the thread reloaded, and that reload rejoined from frame 0,
        // which is always below the buffer again: another `resync`, round
        // and round until the turn ended, with nothing on screen. Now the
        // transcript is reread under the open stream, the tail draws into a
        // fresh reply, and the whole reply is read once the turn ends.
        const fresh = newAssistant(Date.now());
        fresh.streaming = true;
        this.live = fresh;
        this.resynced = true;
        this.store.set((s) => ({ ...s, messages: [...s.messages.filter((m) => m.key !== live.key), fresh] }));
        void this.load(true, true);
        return;
      }
      case "done":
        // A plan drafted in Plan mode waits for "Approve & run" under it.
        if (this.state.mode === "plan" && live.body.trim() && !live.error && live.approval?.state !== "pending") live.planProposal = true;
        if (this.awaitingTranscript) void this.readTranscript();
        this.replied(live);
        break;
      default:
        break;
    }
    this.scheduleFlush();
  }

  /** Text frames arrive per token: redraw at most once per animation frame. */
  private scheduleFlush(): void {
    if (this.flushQueued) return;
    this.flushQueued = true;
    const run = () => {
      this.flushQueued = false;
      this.flush();
    };
    if (typeof requestAnimationFrame === "function" && !document.hidden) requestAnimationFrame(run);
    else setTimeout(run, 16);
  }

  private flush(): void {
    const live = this.live;
    if (!live) return;
    const copy: ChatMessage = { ...live, tools: live.tools.map((t) => ({ ...t })) };
    this.store.set((s) => ({ ...s, messages: s.messages.map((m) => (m.key === live.key ? copy : m)) }));
  }

  private adoptThread(threadId: string): void {
    if (!threadId || threadId === this.state.threadId) return;
    const previous = this.state.threadId;
    this.store.set((s) => ({ ...s, threadId }));
    if (previous) open.delete(keyOf(this.state.agent, previous));
    open.set(keyOf(this.state.agent, threadId), this);
    this.loaded = true;
    // This is now the agent's latest conversation. The agents screen opens
    // its row's thread, and that row used to learn about this one only from a
    // refresh: none ran when the person had already left this screen, and one
    // already in flight answered from before the thread existed, so a tap
    // opened the previous conversation (or a blank one) and this one seemed
    // gone until the turn ended. Written here, the tap finds this
    // conversation (`conversationFor`); the deployment's own answer replaces
    // the row on the next refresh. The conversations list is marked stale,
    // not refreshed: a request now can land before the first message is on disk.
    const agent = this.state.agent;
    const first = this.state.messages.find((m) => m.role === "user")?.body ?? "";
    patchResource<ThreadPage>(`threads:${agent}:latest`, (page) => withStartedThread(page, threadId, first));
    markStale(`threads:${agent}`);
  }

  /** A reply landed: read it aloud when this phone is set to (after a voice note, by default). */
  private replied(reply: ChatMessage): void {
    const askedByVoice = this.askedByVoice;
    this.askedByVoice = false;
    if (!reply.body.trim() || reply.error || document.hidden) return;
    if (!this.readEveryReply && !readsAloud(voicePrefs.get(), askedByVoice)) return;
    void readAloud(this.state.agent, reply.key, reply.body);
  }

  /** Shows what the deployment heard in the voice note just sent, under it. */
  private async readTranscript(): Promise<void> {
    const key = this.awaitingTranscript;
    const { agent, threadId } = this.state;
    if (!key || !threadId) return;
    try {
      const detail = await currentApi().thread(agent, threadId, { limit: 1, toolOutput: false });
      const row = [...(detail.messages || [])].reverse().find((r) => r.role === "user");
      if (!row || this.awaitingTranscript !== key) return;
      const heard = splitVoiceNotes(row.content || "");
      if (!heard.transcripts.length && !heard.untranscribed) return;
      this.awaitingTranscript = null;
      this.store.set((s) => ({
        ...s,
        messages: s.messages.map((m) =>
          m.key === key
            ? { ...m, transcripts: heard.transcripts.length ? heard.transcripts : undefined, untranscribed: heard.untranscribed || undefined }
            : m,
        ),
      }));
    } catch {
      // The next load shows it.
    }
  }

  /**
   * Tells the agent something: into the turn it is working on when there is
   * one (it reads it at its next step), as a new message otherwise.
   */
  async say(text: string): Promise<boolean> {
    const { agent, threadId, busy } = this.state;
    if (busy && threadId) {
      try {
        await currentApi().steer(agent, threadId, text, this.turnId);
        return true;
      } catch {
        // The turn just ended: send it as the next message once it has.
        await new Promise((resolve) => setTimeout(resolve, 800));
      }
    }
    return this.send(text);
  }

  async stop(): Promise<void> {
    const { agent, threadId } = this.state;
    if (!threadId) return;
    try {
      await currentApi().stopRun(agent, threadId, this.runId);
    } catch {
      // The turn may have just ended on its own.
    }
  }

  async answerApproval(approve: boolean): Promise<void> {
    const live = this.live;
    const { threadId } = this.state;
    if (!live?.approval || !threadId) return;
    const decisions = live.approval.actions.map(() => ({ type: approve ? ("approve" as const) : ("reject" as const) }));
    live.approval = { ...live.approval, state: "sending" };
    this.flush();
    try {
      await currentApi().answerApproval(threadId, decisions);
      live.approval = { ...live.approval, state: approve ? "approved" : "rejected" };
    } catch (error) {
      live.approval = { ...live.approval, state: "pending" };
      this.store.set((s) => ({ ...s, sendError: messageOf(error) }));
    }
    this.flush();
  }

  /**
   * The app went to the background: let go of the stream, keep everything else.
   *
   * Parked even when no stream is open yet: a send is often still waiting for
   * the deployment's first bytes when the app leaves (a voice note uploads and
   * is transcribed before the reply begins), and the stream that then opens
   * belongs to a turn nobody is watching. `send` lets it go the moment it
   * arrives, and `resume` rejoins the turn from the cursor.
   */
  park(): void {
    this.parked = true;
    const stream = this.stream;
    if (!stream) return;
    this.stream = null;
    stream.cancel();
  }

  /**
   * Back in the foreground: rejoin the turn in flight, or catch up on what
   * happened meanwhile.
   *
   * A stream still held here opened while the app was away or survived a
   * suspension the OS may have cut underneath it; the frames it would carry
   * are replayed from the cursor anyway, so it is let go rather than trusted.
   * Nothing waits on a dead socket: `load` returns early while a stream is
   * held, which is how a stale one used to freeze the conversation on the
   * last message seen before the app left.
   */
  resume(): void {
    if (this.disposed) return;
    this.parked = false;
    const stream = this.stream;
    if (stream && !this.state.threadId) return; // A new chat's only link to its turn.
    if (stream) {
      this.stream = null;
      stream.cancel();
    }
    if (this.live?.streaming) {
      void this.attach(this.cursor);
    } else {
      void this.load(true);
    }
  }

  /**
   * A magic link sent in this conversation ended (someone finished signing
   * in on the page it opened). Shown once: the transcript carries the same
   * outcome, so a reload never doubles it.
   */
  addConnectResult(result: ConnectResult): void {
    if (this.disposed) return;
    this.store.set((s) =>
      s.messages.some((m) => m.connectResult && m.connectResult.linkId === result.linkId)
        ? s
        : {
            ...s,
            messages: [
              ...s.messages,
              { key: `link-${result.linkId}`, role: "system", body: result.text, tools: [], ts: Date.now(), connectResult: result },
            ],
          },
    );
  }

  /**
   * Reads the page above the messages held and puts it on top. Called as
   * the person nears the top of the conversation, so it is usually there
   * before they reach it. After a failure only an explicit retry (`manual`)
   * tries again: scrolling would otherwise ask on every frame.
   */
  async loadOlder(manual = false): Promise<void> {
    const history = this.history;
    const { agent, threadId } = this.state;
    if (!history || !hasMore(history) || !threadId || this.olderInFlight || this.disposed) return;
    if (this.state.olderError && !manual) return;
    const before = history.first;
    this.olderInFlight = true;
    this.store.set((s) => ({ ...s, loadingOlder: true, olderError: null }));
    try {
      const detail = await currentApi().thread(agent, threadId, { limit: PAGE_SIZE, before, toolOutput: false });
      if (this.disposed || !this.history) return;
      const next = withOlder(this.history, before, detail, foldTranscript);
      // Replaced while it loaded (a refresh found too much new to join up):
      // the new history reads its own pages.
      if (!next) return;
      this.history = next.history;
      const more = hasMore(next.history);
      this.store.set((s) => ({
        ...s,
        messages: [...next.added, ...s.messages],
        hasOlder: more,
        reachedStart: !more,
      }));
    } catch (error) {
      if (!this.disposed) this.store.set((s) => ({ ...s, olderError: messageOf(error, "Couldn't load earlier messages.") }));
    } finally {
      this.olderInFlight = false;
      if (!this.disposed) this.store.set((s) => ({ ...s, loadingOlder: false }));
    }
  }

  /** A row arrived for this thread from somewhere else (another device, a channel). */
  refreshIfIdle(): void {
    if (!this.stream && this.loaded) void this.load(true);
  }

  dispose(): void {
    this.disposed = true;
    if (this.catchUpTimer) clearTimeout(this.catchUpTimer);
    this.catchUpTimer = null;
    this.stream?.cancel();
    this.stream = null;
  }
}
