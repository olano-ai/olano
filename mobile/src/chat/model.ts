/**
 * A conversation as the app draws it: the persisted transcript folded into
 * messages, and a live turn grown frame by frame.
 *
 * Mirrors the dashboard's `foldRow` (views/chat/sessionStore.ts): a turn's
 * tool rows precede its reply row and build one assistant message, which the
 * reply then completes, so a reloaded turn reads exactly like the one that
 * streamed. The text and tool bookkeeping is the dashboard's own
 * (shared/dashboard.ts), not a copy.
 *
 * Pure: no React, no network, so `node --test` covers it.
 */

import {
  appendText,
  applyToolResult,
  mergeMedia,
  upsertToolCall,
  type ChatFrame,
  type MediaRef,
  type ToolCall,
  type TurnSegment,
} from "../shared/dashboard.ts";
import type { ThreadRow } from "../net/types.ts";
import { inlinedFileNames, stripAttachmentNotes, type MessagePhoto } from "./photos.ts";
import { isVoiceMedia, splitVoiceNotes, type MessageVoice } from "./voice.ts";

export interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed" | string;
}

export interface ApprovalAction {
  name: string;
  args?: unknown;
  description?: string;
  interrupt_id?: string;
  risk?: "low" | "medium" | "high" | string;
}

/** How a magic link the agent sent ended (a sign-in finished, a key was saved...). */
export interface ConnectResult {
  linkId: string;
  /** "done" when it worked ("authorized" on older rows); anything else did not complete. */
  status: string;
  flow: string;
  label: string;
  /** The agent the link set up, often not the one that sent it. */
  targetAgent: string;
  /** "restarted" / "reloaded" (already live), "restart_needed", or "none". */
  apply: string;
  text: string;
}

export interface ChatMessage {
  key: string;
  role: "user" | "assistant" | "system";
  body: string;
  segments?: TurnSegment[];
  pendingAfter?: string;
  tools: ToolCall[];
  todos?: Todo[];
  media?: MediaRef[];
  /** Photos this phone just sent, drawn from the phone's own copies until a reload. */
  photos?: MessagePhoto[];
  /** A voice note this phone just sent, played from the phone's own copy until a reload. */
  voice?: MessageVoice[];
  /** What the runtime heard in the message's voice notes. */
  transcripts?: string[];
  /** A voice note the runtime could not transcribe (speech-to-text off or failing). */
  untranscribed?: boolean;
  /** The phone is turning the voice note into words before it is sent. */
  transcribing?: boolean;
  /** The message (and its photos or voice note) is still on its way to the deployment. */
  sending?: boolean;
  /** Typed while the agent was working, and read by it mid-reply. */
  steered?: boolean;
  /** A plan drafted in Plan mode, waiting for "Approve & run". */
  planProposal?: boolean;
  /**
   * Files sent with the message that have nothing on the deployment to open
   * (yet): shown by name. A document this phone just sent comes back as a file
   * on reload; a text file is read into the message itself and stays a name.
   */
  sentFiles?: { name: string; size?: number }[];
  /** Milliseconds since the epoch. */
  ts?: number;
  sender?: string;
  channel?: string;
  /** The reply is still arriving. */
  streaming?: boolean;
  error?: string;
  /** "Stopped", "Chose not to reply"... shown under the reply. */
  note?: string;
  approval?: { actions: ApprovalAction[]; state: "pending" | "sending" | "approved" | "rejected" };
  /** A system line reporting how a magic link ended. */
  connectResult?: ConnectResult;
  /** The turn a reply answered: lets the person report it (`POST /api/chat/feedback`). */
  turnId?: string;
}

let seq = 0;
const nextKey = () => `m${++seq}`;

function parseTs(iso?: string | null): number | undefined {
  if (!iso) return undefined;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? undefined : ms;
}

/** Persisted tool rows may carry the name only as a `name: payload` prefix. */
export function parseToolRow(row: Pick<ThreadRow, "name" | "args" | "content">): { name: string; payload: string } {
  if (row.name) return { name: row.name, payload: row.args || row.content || "" };
  const raw = row.content || "";
  const colon = raw.indexOf(": ");
  if (colon > 0) {
    const name = raw.slice(0, colon);
    if (/^[\w.-]+$/.test(name)) return { name, payload: raw.slice(colon + 2) };
  }
  return { name: "", payload: row.args || raw };
}

/** The checklist out of a `write_todos` call's arguments, or null. */
export function parseTodos(payload: string): Todo[] | null {
  try {
    const value = JSON.parse(payload) as { todos?: unknown };
    if (!value || !Array.isArray(value.todos)) return null;
    const todos = value.todos
      .filter((t): t is Record<string, unknown> => !!t && typeof t === "object")
      .map((t) => ({ content: String(t.content ?? ""), status: String(t.status ?? "pending") }))
      .filter((t) => t.content);
    return todos.length ? todos : null;
  } catch {
    return null;
  }
}

function isToolHost(m: ChatMessage): boolean {
  return m.role === "assistant" && !m.body && (m.tools.length > 0 || !!m.todos?.length);
}

function lastToolId(m: ChatMessage): string | null {
  return m.tools.length ? m.tools[m.tools.length - 1].id : null;
}

export function newAssistant(ts?: number): ChatMessage {
  return { key: nextKey(), role: "assistant", body: "", tools: [], ts };
}

export function newUser(body: string, ts = Date.now(), photos?: MessagePhoto[], voice?: MessageVoice[]): ChatMessage {
  const message: ChatMessage = { key: nextKey(), role: "user", body, tools: [], ts };
  if (photos?.length) message.photos = photos;
  if (voice?.length) message.voice = voice;
  return message;
}

/** A message's photos: the ones it was sent with, then its image media. */
export function photosOf(message: Pick<ChatMessage, "photos" | "media">): MessagePhoto[] {
  const out: MessagePhoto[] = [...(message.photos ?? [])];
  for (const item of message.media ?? []) {
    if (item.kind === "image" && item.url) out.push({ key: item.url, name: item.name || "Photo", url: item.url });
  }
  return out;
}

/** A message's voice notes: the one it was sent with, then its audio media. */
export function voicesOf(message: Pick<ChatMessage, "voice" | "media">): MessageVoice[] {
  const out: MessageVoice[] = [...(message.voice ?? [])];
  for (const item of message.media ?? []) {
    if (isVoiceMedia(item) && item.url) out.push({ key: item.url, name: item.name || "Voice note", url: item.url });
  }
  return out;
}

/** A message's other files (documents, video...), drawn as cards. */
export function filesOf(message: Pick<ChatMessage, "media">): MediaRef[] {
  return (message.media ?? []).filter((item) => item.kind !== "image" && !isVoiceMedia(item));
}

/** Folds one persisted row into the list. */
export function foldRow(out: ChatMessage[], row: ThreadRow): void {
  const ts = parseTs(row.ts);
  if (row.role === "user" || row.role === "peer_inbound") {
    // The runtime's notes about attachments are for the agent: the photos
    // and recordings come back as media, a voice note's transcript is shown
    // with it.
    const voice = splitVoiceNotes(row.content || "");
    const message: ChatMessage = {
      key: nextKey(),
      role: "user",
      body: stripAttachmentNotes(voice.text),
      tools: [],
      ts,
      sender: row.sender || row.from || undefined,
      channel: row.channel || undefined,
    };
    if (voice.transcripts.length) message.transcripts = voice.transcripts;
    if (voice.untranscribed) message.untranscribed = true;
    if (row.steer) message.steered = true;
    const inlined = inlinedFileNames(voice.text);
    if (inlined.length) message.sentFiles = inlined.map((name) => ({ name }));
    mergeMedia(message, row.media);
    out.push(message);
  } else if (row.role === "assistant") {
    const last = out[out.length - 1];
    let target: ChatMessage;
    if (last && isToolHost(last)) {
      target = last;
      if (ts !== undefined) target.ts = ts;
    } else {
      target = newAssistant(ts);
      out.push(target);
    }
    target.body = row.content || "";
    target.segments = row.segments ?? [{ text: target.body, after: lastToolId(target) }];
    target.channel = row.channel || target.channel;
    if (row.turn_id) target.turnId = row.turn_id;
    mergeMedia(target, row.media);
  } else if (row.role === "system") {
    const message: ChatMessage = { key: nextKey(), role: "system", body: row.content || "", tools: [], ts };
    if (row.event === "magic_link") message.connectResult = connectResultFromRow(row);
    out.push(message);
  } else if (row.role === "tool_call" || row.role === "tool_result") {
    const last = out[out.length - 1];
    let host = last && last.role === "assistant" ? last : null;
    if (!host) {
      host = newAssistant(ts);
      out.push(host);
    }
    const { name, payload } = parseToolRow(row);
    const todos = name === "write_todos" && row.role === "tool_call" ? parseTodos(payload) : null;
    if (todos) host.todos = todos;
    else if (name === "write_todos") return;
    else if (row.role === "tool_call") upsertToolCall(host.tools, row.tool_call_id || "", name, payload);
    else {
      applyToolResult(host.tools, row.tool_call_id || "", name, payload);
      mergeMedia(host, row.media);
    }
  }
}

/** A magic link's outcome as the transcript row (or the live event, same fields) carries it. */
export function connectResultFromRow(row: Partial<Record<"link_id" | "status" | "flow" | "label" | "target_agent" | "apply" | "content" | "text", string>>): ConnectResult {
  return {
    linkId: row.link_id || "",
    status: row.status || "",
    flow: row.flow || "",
    label: row.label || "",
    targetAgent: row.target_agent || "",
    apply: row.apply || "",
    text: row.text || row.content || "",
  };
}

export function foldTranscript(rows: ThreadRow[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (const row of rows) foldRow(out, row);
  // Persisted turns are history: nothing in them is still running.
  for (const m of out) {
    for (const t of m.tools) if (!t.hasResult) t.hasResult = true;
  }
  return out;
}

/** What a frame did to the turn, for the controller. */
export type FrameEffect =
  | { kind: "none" }
  | { kind: "start"; threadId: string; runId?: string; turnId?: string }
  | { kind: "thread_switch"; threadId: string }
  | { kind: "done"; threadId?: string }
  | { kind: "resync" };

const STATUS_NOTES: Record<string, string> = {
  stopped: "Stopped",
  no_reply: "Chose not to reply",
};

/**
 * Applies one stream frame to the live assistant message (mutating it) and
 * reports what the controller has to do about it.
 */
export function applyFrame(live: ChatMessage, frame: ChatFrame): FrameEffect {
  switch (frame.type) {
    case "start":
      if (frame.turn_id) live.turnId = frame.turn_id;
      return { kind: "start", threadId: frame.thread_id, runId: frame.run_id, turnId: frame.turn_id };
    case "text":
      appendText(live, frame.text);
      return { kind: "none" };
    case "tool_call": {
      const todos = frame.name === "write_todos" ? parseTodos(frame.args) : null;
      if (todos) {
        live.todos = todos;
      } else if (frame.name !== "write_todos") {
        upsertToolCall(live.tools, frame.id, frame.name, frame.args);
        live.pendingAfter = frame.id;
      }
      return { kind: "none" };
    }
    case "tool_result":
      if (frame.name !== "write_todos") {
        applyToolResult(live.tools, frame.id, frame.name, frame.content);
        mergeMedia(live, frame.media);
      }
      return { kind: "none" };
    case "media":
      mergeMedia(live, frame.items);
      return { kind: "none" };
    case "approval":
      live.approval = { actions: frame.actions as ApprovalAction[], state: "pending" };
      return { kind: "none" };
    case "status":
      live.note = STATUS_NOTES[frame.kind] ?? frame.message;
      return { kind: "none" };
    case "error":
      live.error = frame.message || "The agent ran into an error.";
      return { kind: "none" };
    case "thread_switch":
      return { kind: "thread_switch", threadId: frame.new_thread_id };
    case "resync":
      return { kind: "resync" };
    case "done":
      live.streaming = false;
      for (const t of live.tools) if (!t.hasResult) t.hasResult = true;
      if (live.approval?.state === "pending") live.approval = { ...live.approval, state: "rejected" };
      return { kind: "done", threadId: frame.thread_id };
    default:
      return { kind: "none" };
  }
}

/** The text a thread row shows as its title. */
export function threadTitle(t: { name?: string; title?: string; preview?: string }): string {
  return (t.name || t.title || t.preview || "New chat").trim();
}
