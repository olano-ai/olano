// App-level chat session store.
//
// The chat experience used to live entirely inside the ChatView component:
// the streaming loop, the message list, the `streaming` flag and the
// AbortController were all component state. React Router unmounts a route's
// component when you navigate away (left-nav tabs), so any frame that arrived
// after unmount updated a dead component (a no-op) and was lost — switching to
// "Files" mid-turn looked like the agent had stopped. Switching agent reset
// everything too, and only one conversation could run at a time.
//
// This module owns all live session state at module scope, keyed by
// agent → session, so:
//   1. streams keep running (and accumulating) regardless of which view is
//      mounted — the loop writes to this store, not to component state;
//   2. many sessions can stream in parallel for the same agent;
//   3. switching agent never tears down another agent's in-flight sessions.
//
// Components read the store through `useSyncExternalStore` (see ChatView): a
// single `version` counter is bumped on every mutation, so a mounted view
// re-renders on change while unmounted views simply don't (their subscription
// is gone) — the data keeps accumulating either way. A full page reload starts
// fresh, matching usePersistentState semantics.

import { needsPersonEveryTime } from "../browser/approval";
import { api, ApiError } from "../../core/api";
import { shouldRefetch, subscribeResume } from "../../core/lifecycle";
import { attachChat, stopChatRun, streamChat, StreamRequestError } from "../../core/sse";
import { composeWithShell, shellResultMarkdown, type ShellRunResult } from "../../core/shell";
import { toWire, type Attachment } from "./attachments";
import { parseTodos, settleTodos, type TodoItem } from "./todos";
import { applySteerFrame, dropDeliveredSteers, newSteerId, takeBackQueued } from "./steering";
import { restoreModelFromThread } from "./chatModel";
import type { ApprovalAction, ChatFrame, ErrorRemediation, MediaRef } from "../../core/types";
import {
  appendText,
  applyToolResult,
  mergeMedia,
  subagentPath,
  upsertToolCall,
  type Sub,
  type ToolCall,
  type TurnSegment,
} from "./turnText";
import type { ApprovalChoice, ApprovalState } from "./ApprovalCard";
import type { Usage } from "./ChatHeader";

// ── message model (was inlined in ChatView) ────────────────────────────────
// One row of the /context usage breakdown. `tokens` is an estimate (chars/4 over
// the real source files) for the static buckets; `count` is the item tally
// (tools, skills, …). The backend (GET /api/agents/{name}/context) owns these.
export interface ContextCategory {
  key: string;
  tokens: number;
  count?: number;
  onDemand?: boolean;
}
// The full payload behind a rendered /context card: the server-side static
// breakdown plus the live conversation usage the card needs to derive the
// "Messages" / "Free space" remainder against the model's context window.
export interface ContextReport {
  model: string;
  categories: ContextCategory[];
  /** Last turn's context-window fill (usage.context), or null on a fresh thread. */
  usedContext: number | null;
}
// One span of an assistant reply between tool boundaries. `after` is the
// tool_call_id of the LAST tool call that ran before the span — null for the
// opening span. The spans concatenate to the message body. Defined in
// `turnText`, which the Groups store shares; re-exported here because every
// existing importer reaches for it through this module.
export type { TurnSegment };
// The turn-accumulation model itself — a tool call, a subagent transcript, and
// the three folds that grow them — lives in `turnText` beside the text spans,
// because the Groups store folds the same frames and node's test runner has to
// be able to import that without pulling a browser store in behind it.
// Re-exported here because every existing importer reaches for them through
// this module.
export type { Sub, ToolCall };
export { applyToolResult, subagentPath, upsertToolCall };
export interface Msg {
  id: number;
  role: "user" | "assistant" | "system";
  body: string;
  tools: ToolCall[];
  subs: Sub[];
  // Epoch ms the message was sent/produced. Set from Date.now() for live turns
  // and parsed from the persisted `ts` for reloaded threads; drives the "time
  // ago" label rendered beside a message's actions.
  ts?: number;
  attachments?: Attachment[];
  // Workspace files this turn produced or named (a tool's screenshot, a report
  // the reply mentions), rendered as cards beneath the message regardless of
  // the tool-detail level and — via their `mentions` — linked inside the
  // text. ALWAYS a fresh array when it changes (see mergeMedia): renderers
  // key their caches on its identity.
  media?: MediaRef[];
  // The assistant's text split at tool boundaries, in order (TurnSegment).
  // Built chunk by chunk from the live stream (appendText) and served by the
  // backend on reloaded rows; absent → one span drawn after the tool cards,
  // the pre-segment layout. Rendered through turnBlocks.ts.
  segments?: TurnSegment[];
  // Live-stream bookkeeping only: the id of the latest tool_call frame, so the
  // next text chunk opens a new span after it. Never persisted.
  pendingAfter?: string;
  approvals?: ApprovalState[];
  todos?: TodoItem[];
  // Set on a /context system message to render the rich usage card (see ContextCard).
  context?: ContextReport;
  // Set on a magic-link outcome system message, to render ConnectResultCard —
  // the completion half of the ConnectCard the same conversation started with.
  connectResult?: MagicLinkResult;
  // Set on an assistant message produced in Plan mode, so the chat can offer
  // "Approve & run" / "Request changes" actions under the drafted plan.
  planProposal?: boolean;
  // Set when the turn ended in an actionable error (e.g. an expired provider
  // sign-in), so the chat can render a remediation call-to-action.
  remediation?: ErrorRemediation | null;
  // Which surface this turn arrived on ("telegram", "dashboard", …), stamped by
  // the runner from the live request context. Only set on reloaded threads —
  // a live turn is, by definition, the surface you are looking at. The chat
  // badges it only when it differs from the thread's own channel, so a
  // conversation that moved between surfaces shows exactly where it moved.
  channel?: string;
  // Platform display name of the human who sent this turn, when the runtime
  // knew one. Replaces the generic "You" on a message that came from elsewhere.
  sender?: string;
  // A message steered INTO a running turn (the model read it at its next step,
  // mid-reply) rather than one that started a turn. Labelled in the chat and
  // skipped by Retry, which resends the turn's own prompt. See steering.ts.
  steer?: boolean;
  // The Langfuse trace of this reply, when the turn was traced (from the
  // reloaded row or the live `done` frame). Admins get an "Open in Langfuse"
  // link from it; see views/observability/links.ts.
  traceId?: string;
  // The turn this reply closed (runner._TURN_ID), from the reloaded row or the
  // live `done` frame: what a thumbs vote names. Absent on replies recorded
  // before turns were stamped, which get no thumbs.
  turnId?: string;
  // This person's own vote on the reply (POST /api/chat/feedback), read back
  // with the thread. Nobody else's vote ever reaches this tab.
  feedback?: ReplyVote;
}

/** One person's thumbs on one reply: 1 up, -1 down. */
export interface ReplyVote {
  value: 1 | -1;
  comment: string;
}

/**
 * A magic link (or an MCP OAuth sign-in) that finished out-of-band, reported
 * back into the thread it was started from. The backend delivers it twice —
 * persisted as a marked `system` row in the transcript, and pushed live as a
 * `magic_link_updated` SSE frame — so the same card draws whether you were
 * watching when it landed or opened the thread an hour later. `linkId` is the
 * dedupe key across both paths.
 */
export interface MagicLinkResult {
  linkId: string;
  /**
   * Store status: "done" for a completed sign-in, else it failed or expired.
   * Older MCP OAuth rows carry "authorized" for success; `resultSucceeded`
   * reads both.
   */
  status: string;
  /** Flow id, e.g. "channel-connect" / "secret-entry" / "mcp-oauth". */
  flow: string;
  /** Human label for the flow ("Connect channels", "API key"). */
  label: string;
  /** The agent the link CONFIGURED — often not the agent whose chat asked. */
  targetAgent: string;
  /**
   * What stands between the finished link and the agent using it:
   * "restarted" / "reloaded" — already applied, just say so;
   * "restart_needed" — offer the button;
   * "self" / "none" — nothing to offer.
   */
  apply: string;
  /** The ✅/⚠️ sentence the backend wrote. */
  text: string;
}

// One projected transcript row, exactly as `GET /api/agents/{name}/threads/{id}`
// serves it — and, since the backend runs the live `thread_updated` frame
// through the same projection, exactly what `threadSync.ts` receives per row.
export interface ThreadMessage {
  role: string;
  content: string;
  // A magic-link outcome row (runtime/api.py stamps `event: "magic_link"` and
  // these fields; every other `system` row stays filtered out server-side).
  event?: string;
  link_id?: string;
  status?: string;
  flow?: string;
  label?: string;
  target_agent?: string;
  apply?: string;
  // Provenance the server records per turn (runtime/api.py's get_thread).
  channel?: string;
  sender?: string;
  name?: string;
  args?: string;
  tool_call_id?: string;
  media?: MediaRef[];
  // Assistant rows: the reply split at tool boundaries (see TurnSegment). A
  // row without it is one span.
  segments?: TurnSegment[];
  // ISO timestamp the server persisted for this turn (see runtime/api.py's
  // get_thread). Parsed into Msg.ts for the "time ago" label.
  ts?: string | null;
  // A user row the runner wrote mid-turn for a steered message, with the id
  // the chip was posted under (runtime/api.py's `_transcript_message`).
  steer?: boolean;
  steer_id?: string;
  /** The Langfuse trace this reply belongs to, when the turn was traced. */
  trace_id?: string;
  /** The turn this reply closed: what a feedback vote names. */
  turn_id?: string;
  /** The caller's own vote on this reply, when they cast one. */
  feedback?: ReplyVote;
}

// A loaded thread's payload: its messages plus the cached cumulative token/cost
// totals (absent until the thread is indexed by the threadcost cache).
interface ThreadDetail {
  messages: ThreadMessage[];
  /** The model the thread last answered on (runner._model_stamp), absent for
   *  a thread recorded before the stamp existed — it then opens on the
   *  agent's default like a new chat. */
  model?: string | null;
  reasoning_effort?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  usd?: number | null;
  /** Olano-managed spend accumulated by this thread (gateway charge). */
  credits?: number | null;
  /** Model calls mined for this thread (one per model sub-call). */
  calls?: number | null;
  /** Tool calls this thread's transcript recorded, MCP tools included. */
  tool_calls?: number | null;
  /** A turn running on this thread right now, if any (olano/core/runs.py).
   * The transcript holds only persisted rows, so it cannot say a turn is still
   * in flight — this is what lets a freshly loaded page attach instead of
   * showing a conversation that looks finished and silently is not. */
  run?: { run_id: string; turn_id: string; cursor: number; running: boolean } | null;
}

/** An empty running tally — the base for the first counter that lands. */
function blankUsage(): Usage {
  return { input: 0, output: 0 };
}

// Restore the running token/cost tally for a reopened thread so the header picks
// up where the conversation left off (and keeps accumulating). `context` (the
// live window fill) is unknown until the next turn streams, so it stays unset.
function seedUsage(d: ThreadDetail): Usage | null {
  const input = d.input_tokens ?? 0;
  const output = d.output_tokens ?? 0;
  const usd = d.usd ?? 0;
  const credits = d.credits ?? 0;
  const calls = d.calls ?? 0;
  const toolCalls = d.tool_calls ?? 0;
  if (!input && !output && !usd && !credits && !calls && !toolCalls) return null;
  return {
    input,
    output,
    usd: usd || undefined,
    credits: credits || undefined,
    calls: calls || undefined,
    toolCalls: toolCalls || undefined,
  };
}

// One conversation. `key` is a stable local handle: the thread id for sessions
// opened from the rail, or a `draft:<n>` token for a new chat that hasn't been
// assigned a thread id yet (the backend assigns one in the `start` frame).
export interface Session {
  key: string;
  agent: string;
  threadId: string | null;
  // The backend's handle on the turn streaming right now (from the `start`
  // frame; null when idle). `POST /api/chat/steer` names it so a message typed
  // against THIS reply is never handed to a later turn on the same thread.
  turnId: string | null;
  // The backend's handle on the RUN behind this turn — the detached task that
  // keeps working when this socket dies (olano/core/runs.py). Together with
  // `cursor` (the last frame `seq` this tab saw) it is everything needed to
  // rejoin: `GET /api/chat/attach`. Null when nothing is running here.
  runId: string | null;
  cursor: number;
  // An attach is in flight — the turn is alive on the server and this tab is
  // catching up. Distinct from `interrupted`, which now means the run is gone.
  attaching: boolean;
  messages: Msg[];
  streaming: boolean;
  usage: Usage | null;
  streamModel: string;
  // THIS conversation's model override ("" = the agent's configured model)
  // and reasoning effort ("" = the model's default). Per conversation, never
  // per agent: a new chat always starts at "", a reopened thread is seeded
  // with the model it last answered on (see chatModel.ts), and the header's
  // picker writes here. Every turn this session sends carries both.
  model: string;
  reasoningEffort: string;
  abort: AbortController | null;
  autoApprove: boolean;
  unread: number;
  lastActivity: number;
  input: string;
  staged: Attachment[];
  // Messages typed while this session was still streaming. A `queue` chip is
  // drained FIFO when the current turn ends (one per follow-up turn). A `steer`
  // chip was handed to the running turn and leaves the queue when the model
  // reads it (the `steer` frame) — or, if the turn ends first, drains like any
  // other chip, so a steer is never lost. See steering.ts.
  queue: QueuedMsg[];
  // Results of shell commands ("!" or inline "Run Now") run since the last
  // message, merged into the next turn (or flushed on demand) — never auto-sent
  // to the agent.
  shellBuffer: ShellRunResult[];
  // Set when the last turn ended without a terminal frame (done|error) and
  // wasn't user-aborted: the connection dropped mid-turn (phone lock, network
  // blip) and the reply below may be truncated. Cleared by send()/reconcile().
  interrupted: { at: number; hadPendingApproval: boolean } | null;
  // A reconcile refetch is in flight — the view keeps the partial visible.
  reconciling: boolean;
  // Last reconcile replaced local content with fuller server state, so the view
  // can show a "recovered the full reply" note. Cleared on the next send().
  recoveredAt: number | null;
  // Opts of the last send() — lets Retry and the post-reconcile queue drain
  // reuse the turn's mode/model/error label.
  lastOpts: SendOpts | null;
  error?: string;
  // A turn running on this thread from ANOTHER surface (Telegram, a scheduled
  // job, the OpenAI connector, a second browser) — reported by the runtime's
  // `thread_activity` frames, never by this tab's own stream (`streaming`
  // covers that). `text` is the reply streamed so far, throttled server-side,
  // so a phone and a laptop watching the same thread see the answer forming.
  activity: RemoteActivity | null;
  // When this tab's own last turn finished (0 = never). The runner announces
  // that turn's rows on /api/stream too, on a different connection from the
  // turn's stream, so its final row can land here just AFTER `done` — this
  // is what tells it apart from a genuinely new row from another surface.
  ownTurnEndedAt: number;
  // Bumped on every mutation of THIS session — a cheap dep for effects that
  // should react to this conversation's content (e.g. auto-scroll).
  rev: number;
}

// How long after this tab's own turn ends its broadcast echoes may still arrive.
const OWN_TURN_ECHO_MS = 2_000;

export interface RemoteActivity {
  status: "thinking" | "streaming";
  /** The surface the turn arrived on ("telegram", "scheduler", …), if known. */
  channel: string;
  text: string;
  at: number;
}

export interface SendOpts {
  text: string;
  attachments: Attachment[];
  mode: string;
  model?: string;
  reasoning_effort?: string;
  errorLabel: string; // localized "Error" label (the store has no i18n context)
  // Explicit shell results to merge into this turn (the "Send to Agent now"
  // path). When omitted, send() drains the session's own shellBuffer.
  shellResults?: ShellRunResult[];
}

// A message the user composed while a turn was still streaming. `kind` says
// what happens to it: a `queue` chip waits and is sent as the next turn when
// the current reply finishes; a `steer` chip has been handed to the running
// turn (`POST /api/chat/steer`) and the model reads it at its next step. Every
// chip offers Steer / Edit / Cancel. `mode` is captured at enqueue time so a
// queued message runs in the mode it was typed in.
export interface QueuedMsg {
  id: number;
  kind: "queue" | "steer";
  // Client-minted id a steer chip was posted under — matched against the
  // `steer` frame that reports the model read it, and used to cancel it.
  steerId?: string;
  text: string;
  attachments: Attachment[];
  mode: string;
}
// What a composer hands the store: the chip minus what the store assigns.
export type QueuedDraft = Omit<QueuedMsg, "id" | "kind" | "steerId">;

// ── module state ───────────────────────────────────────────────────────────
const sessions = new Map<string, Session>(); // skey(agent, key) → Session
const activeKey = new Map<string, string>(); // agent → key
let viewing: { agent: string; key: string } | null = null;
let version = 0;
let draftSeq = 0;
let msgSeq = 0;

const listeners = new Set<() => void>();
const threadsListeners = new Set<() => void>();
const errorListeners = new Set<(code: ErrorCode) => void>();

export type ErrorCode = "chat" | "approval" | "open";

function skey(agent: string, key: string): string {
  return `${agent}\0${key}`;
}
function enc(s: string): string {
  return encodeURIComponent(s);
}

// Bump the global version (re-render mounted subscribers); optionally bump one
// session's rev too.
function bump(s?: Session): void {
  if (s) s.rev++;
  version++;
  for (const l of listeners) l();
}

// Streaming delivers one frame per token, and a synchronous bump() per frame
// re-renders every subscriber (the whole chat) per token. bumpCoalesced()
// advances version/rev immediately — getSnapshot stays fresh — but folds all
// bumps landing in the same frame into a single listener notification on the
// next animation frame. The timeout backstop covers hidden tabs (rAF is paused
// there), so a background stream is at most ~250 ms stale on return. User
// mutations (typing, sends, deletes) keep the synchronous bump().
let coalescePending = false;
let coalesceTimer: ReturnType<typeof setTimeout> | null = null;
function flushCoalesced(): void {
  if (!coalescePending) return;
  coalescePending = false;
  if (coalesceTimer !== null) {
    clearTimeout(coalesceTimer);
    coalesceTimer = null;
  }
  for (const l of listeners) l();
}
function bumpCoalesced(s: Session): void {
  s.rev++;
  version++;
  if (coalescePending) return;
  coalescePending = true;
  // One scheduler per flush window, not two: rAF while the tab is visible
  // (fires next frame), a timer only when rAF is paused/unavailable — arming
  // both would create and cancel a timer per frame for the whole stream. A
  // tab hidden AFTER the rAF was scheduled is covered by the module-level
  // visibilitychange flush below (rAF freezes until the tab returns).
  if (typeof requestAnimationFrame === "function" && !document.hidden) {
    requestAnimationFrame(flushCoalesced);
  } else {
    coalesceTimer = setTimeout(flushCoalesced, 250);
  }
}
// Flush a pending coalesce the moment the tab hides, so no notification waits
// on a frozen rAF; a flush with nothing pending is a no-op.
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) flushCoalesced();
  });
}
function notifyThreads(): void {
  for (const l of threadsListeners) l();
}
function emitError(code: ErrorCode): void {
  for (const l of errorListeners) l(code);
}

function isViewing(s: Session): boolean {
  return viewing !== null && viewing.agent === s.agent && viewing.key === s.key;
}

function producedContent(m: Msg | undefined): boolean {
  if (!m) return false;
  return !!(
    m.body ||
    m.tools.some((c) => c.name) ||
    m.subs.length ||
    m.todos?.length ||
    m.approvals?.length ||
    m.media?.length
  );
}

// An assistant message that so far holds only what tool rows put there (cards,
// a plan) and no text: the reply row that follows completes it.
function isToolHost(m: Msg): boolean {
  return m.role === "assistant" && !m.body && (m.tools.length > 0 || !!m.todos?.length);
}

function lastToolId(m: Msg): string | null {
  return m.tools.length ? m.tools[m.tools.length - 1].id : null;
}

// ── tool-call accumulation (moved verbatim from ChatView) ───────────────────
// Merge a tool_call frame/entry into the list. Prefer matching by id; with no id
// (reloaded partials), fold into the most recent still-open call of the same name.
//
// Exported because the group transcript folds the SAME frames (the group stream
// forwards tool_call/tool_result/subagent_text now, so a member reaching a peer
// or a subagent is visible where the collaboration happens). A second copy of
// this matching is how the two shells would start disagreeing about what a
// tool call with no id folds into.
// Persisted tool entries are stored as a single "name: payload" string (see
// runner `_append_tool_session`), with no separate name field. Recover the name
// so reloaded threads show "Talking with X" / "Calling X" like the live stream.
function parseSessionTool(m: ThreadMessage): { name: string; payload: string } {
  if (m.name) return { name: m.name, payload: m.args || m.content || "" };
  const raw = m.content || "";
  const colon = raw.indexOf(": ");
  if (colon > 0) {
    const name = raw.slice(0, colon);
    if (/^[\w.-]+$/.test(name)) return { name, payload: raw.slice(colon + 2) };
  }
  return { name: "", payload: m.args || raw };
}

// Parse a persisted ISO timestamp into epoch ms (undefined if absent/invalid),
// so a reopened thread can show the same "time ago" labels the live stream did.
function parseTs(iso?: string | null): number | undefined {
  if (!iso) return undefined;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? undefined : ms;
}

/** Map a persisted magic-link row onto the same shape the SSE frame carries. */
function magicLinkFromRow(m: ThreadMessage): MagicLinkResult {
  return {
    linkId: m.link_id || "",
    status: m.status || "",
    flow: m.flow || "",
    label: m.label || "",
    targetAgent: m.target_agent || "",
    apply: m.apply || "",
    text: m.content || "",
  };
}

// Fold ONE persisted row into a Msg list (tool_call/result fold into the
// preceding assistant turn as tool cards; write_todos folds into a checklist).
//
// The single reducer behind both a reload (`fromThread`) and a live
// `thread_updated` row (`applyThreadRow`): a turn that ran on Telegram or from
// the scheduler reaches an open tab row by row, through this, so what the tab
// shows as it happens is what a reload would rebuild — never a second,
// slightly different rendering of the same conversation.
function foldRow(out: Msg[], m: ThreadMessage): void {
  const ts = parseTs(m.ts);
  if (m.role === "user" || m.role === "peer_inbound") {
    const um: Msg = {
      id: ++msgSeq,
      role: "user",
      body: m.content,
      tools: [],
      subs: [],
      ts,
      channel: m.channel || undefined,
      sender: m.sender || undefined,
      steer: m.steer || undefined,
    };
    // Chat-uploaded files (saved server-side under uploads/, referenced by a
    // path note in the message) re-render as inline media on reload.
    mergeMedia(um, m.media);
    out.push(um);
  } else if (m.role === "assistant") {
    // A turn's tool rows precede its reply row and have already built a host
    // message (the tool branch below). The reply completes THAT message —
    // body, spans, media — instead of opening a second one, so a reloaded
    // turn is one message with its tool cards and text interleaved, exactly
    // as the live stream drew it. A row without `segments` is one span that
    // follows every tool the host holds (the pre-segment layout).
    const last = out[out.length - 1];
    let target: Msg;
    if (last && isToolHost(last)) {
      target = last;
      if (ts !== undefined) target.ts = ts;
      if (m.channel) target.channel = m.channel;
    } else {
      target = { id: ++msgSeq, role: "assistant", body: "", tools: [], subs: [], ts, channel: m.channel || undefined };
      out.push(target);
    }
    target.body = m.content;
    target.segments = m.segments ?? [{ text: m.content, after: lastToolId(target) }];
    if (m.trace_id) target.traceId = m.trace_id;
    if (m.turn_id) target.turnId = m.turn_id;
    if (m.feedback) target.feedback = m.feedback;
    mergeMedia(target, m.media);
  } else if (m.role === "system") {
    // The server only lets ONE kind of system row through (a magic-link
    // outcome); anything else here is a plain note. Rebuilding the card from
    // the row — rather than rendering its text — is what makes a reload look
    // identical to having watched it land, Restart button included.
    out.push({
      id: ++msgSeq,
      role: "system",
      body: m.content,
      tools: [],
      subs: [],
      ts,
      connectResult: m.event === "magic_link" ? magicLinkFromRow(m) : undefined,
    });
  } else if (m.role === "tool_call" || m.role === "tool_result") {
    const last = out[out.length - 1];
    const target = last && last.role === "assistant" ? last : null;
    const host =
      target ??
      (() => {
        const a: Msg = { id: ++msgSeq, role: "assistant", body: "", tools: [], subs: [], ts };
        out.push(a);
        return a;
      })();
    const { name, payload } = parseSessionTool(m);
    const todos = name === "write_todos" ? parseTodos(payload) : null;
    if (todos) host.todos = todos;
    else if (m.role === "tool_call") upsertToolCall(host.tools, m.tool_call_id || "", name, payload);
    else {
      applyToolResult(host.tools, m.tool_call_id || "", name, payload);
      mergeMedia(host, m.media);
    }
  }
}

// Rebuild the Msg list from a persisted thread.
function fromThread(messages: ThreadMessage[]): Msg[] {
  const out: Msg[] = [];
  for (const m of messages) foldRow(out, m);
  // Persisted turns are settled history — finalize any dangling in_progress step
  // so a reloaded thread doesn't render a completed turn as "stuck on step N".
  for (const msg of out) {
    if (msg.role === "assistant" && msg.todos) msg.todos = settleTodos(msg.todos);
  }
  return out;
}

// A session that mirrors a persisted thread and is not running its own turn:
// the only kind a live row from another surface may be folded into. A
// streaming session writes the same rows through its own stream, so folding
// the broadcast copy too would show every tool call and reply twice — and the
// turn's last row (the reply, persisted in the runner's finally) can arrive on
// /api/stream a beat after the turn's own `done`, so the moment right after a
// local turn is treated the same way. An ATTACH counts as streaming here for
// exactly the same reason: a rejoined turn is this tab's own view of the run
// (olano/core/runs.py), writing the same rows through the attach stream, so its
// broadcast copies are echoes too — `isEcho` would catch most of them, but only
// most, and a turn drawn twice is the failure this guard exists to prevent.
function remoteTarget(agent: string, threadId: string): Session | undefined {
  const s = sessionsFor(agent).find((x) => x.threadId === threadId);
  if (!s || s.streaming || s.attaching) return undefined;
  if (s.ownTurnEndedAt && Date.now() - s.ownTurnEndedAt < OWN_TURN_ECHO_MS) return undefined;
  return s;
}

// Whether a live row merely repeats what the session already shows — the
// second belt against a local turn's echo (the window above is the first):
// the reply the stream just rendered, arriving again as its persisted row.
function isEcho(s: Session, row: ThreadMessage): boolean {
  if (row.role !== "assistant" && row.role !== "user" && row.role !== "peer_inbound") return false;
  const role = row.role === "assistant" ? "assistant" : "user";
  for (let i = s.messages.length - 1, seen = 0; i >= 0 && seen < 3; i--) {
    const m = s.messages[i];
    if (m.role !== role) continue;
    seen++;
    if (m.body.trim() === (row.content || "").trim() && m.body.trim()) return true;
  }
  return false;
}

// ── session helpers ─────────────────────────────────────────────────────────
function getSession(agent: string, key: string | undefined | null): Session | undefined {
  if (!key) return undefined;
  return sessions.get(skey(agent, key));
}

function createSession(agent: string, key: string): Session {
  const s: Session = {
    key,
    agent,
    threadId: key.startsWith("draft:") ? null : key,
    turnId: null,
    runId: null,
    cursor: 0,
    attaching: false,
    messages: [],
    streaming: false,
    usage: null,
    streamModel: "",
    model: "",
    reasoningEffort: "",
    abort: null,
    autoApprove: false,
    unread: 0,
    // 0 until a turn actually streams — so opening an old thread sorts by its
    // real last-message time (updated_at), not by the act of opening it.
    lastActivity: 0,
    input: "",
    staged: [],
    queue: [],
    shellBuffer: [],
    interrupted: null,
    reconciling: false,
    recoveredAt: null,
    lastOpts: null,
    activity: null,
    ownTurnEndedAt: 0,
    rev: 0,
  };
  sessions.set(skey(agent, key), s);
  return s;
}

function sessionsFor(agent: string): Session[] {
  const out: Session[] = [];
  for (const s of sessions.values()) if (s.agent === agent) out.push(s);
  return out;
}

// ── approvals ────────────────────────────────────────────────────────────────
async function postDecisions(threadId: string, decisions: { type: string }[]): Promise<void> {
  try {
    await api.post("/api/chat/approval", { thread_id: threadId, decisions });
  } catch {
    emitError("approval");
  }
}

// ── frame application ─────────────────────────────────────────────────────────
function applyFrame(s: Session, f: ChatFrame, errorLabel: string): void {
  const m = s.messages[s.messages.length - 1];
  // Every frame is numbered within its run; remembering the highest one seen is
  // what lets a reconnect ask for exactly what it missed. Frames replayed on an
  // attach carry their original seq, so this only ever moves forward.
  if (typeof f.seq === "number" && f.seq > s.cursor) s.cursor = f.seq;
  switch (f.type) {
    case "start":
      s.threadId = f.thread_id;
      s.turnId = f.turn_id ?? null;
      s.runId = f.run_id ?? null;
      if (f.model) s.streamModel = f.model;
      // The new thread now exists server-side — refresh the rail so it shows.
      notifyThreads();
      break;
    case "thread_switch":
      // Auto-compaction (input >= ~80k tokens) forks a successor thread
      // server-side and the predecessor is hidden from the thread list. Adopt
      // the successor's id so this live session maps onto the persisted thread
      // instead of its now-hidden predecessor — otherwise the rail shows the
      // same conversation twice (a stale live row beside the backend thread).
      if (f.new_thread_id) {
        s.threadId = f.new_thread_id;
        notifyThreads();
      }
      break;
    case "text":
      if (m) appendText(m, f.text);
      break;
    case "subagent_text": {
      const path = subagentPath(f.agents, f.namespace);
      if (m) {
        const found = m.subs.find((x) => x.path === path);
        if (found) found.text += f.text;
        else m.subs.push({ path, text: f.text });
      }
      break;
    }
    case "tool_call":
      if (m) {
        // The next text chunk opens a new span after this call, which is
        // where turnBlocks draws the cards. Every announced call counts,
        // write_todos included — an id the tool list doesn't hold simply
        // flushes the cards still pending, so nothing is ever dropped.
        m.pendingAfter = f.id || "";
        if (f.name === "write_todos") {
          const todos = parseTodos(f.args || "");
          if (todos) m.todos = todos;
        } else {
          upsertToolCall(m.tools, f.id || "", f.name || "", f.args || "");
        }
        // Running tool-call tally for the header chip — counted here (not from
        // the rendered tool list) so a re-render can't double it, and counting
        // every announced call including MCP tools. write_todos is the agent's
        // own plan bookkeeping, not work it did, so it stays out.
        if (f.name !== "write_todos") {
          s.usage = { ...(s.usage ?? blankUsage()), toolCalls: (s.usage?.toolCalls ?? 0) + 1 };
        }
      }
      break;
    case "tool_result":
      if (m) {
        if (f.name === "write_todos") {
          const todos = parseTodos(f.content || "");
          if (todos) m.todos = todos;
        } else {
          applyToolResult(m.tools, f.id || "", f.name || "", f.content || "");
          mergeMedia(m, f.media);
        }
      }
      break;
    case "media":
      // The files the reply's own text named, resolved once the text is final
      // (one frame per turn, before `done`).
      if (m) mergeMedia(m, f.items);
      break;
    case "steer": {
      // The model just read a message steered into this turn: its chip leaves
      // the queue and the transcript splits — the aside as a user bubble, then
      // a fresh assistant bubble the continuation streams into. Exactly the
      // shape foldRow rebuilds from the persisted rows on a reload.
      const r = applySteerFrame(s.messages, s.queue, f, () => ++msgSeq, Date.now());
      s.messages = r.messages;
      s.queue = r.queue;
      break;
    }
    case "approval": {
      const actions = (f.actions || []) as ApprovalAction[];
      if (!m) break;
      // "Accept all" never covers a browser submit/purchase/send: those are
      // asked every time (views/browser/approval.ts).
      if (s.autoApprove && !needsPersonEveryTime(actions)) {
        (m.approvals ||= []).push({ actions, resolved: true, choice: "all", auto: true });
        if (s.threadId) void postDecisions(s.threadId, actions.map(() => ({ type: "approve" })));
      } else {
        (m.approvals ||= []).push({ actions, resolved: false });
      }
      break;
    }
    case "usage": {
      // Accumulate into the running thread total (seeded on load) so the header
      // keeps adding up turn after turn — matching the per-thread totals shown in
      // the rail. `input_tokens` is the per-turn SUM across every model sub-call
      // (right for billing), so it feeds the running total — NOT the context bar.
      // `context` (the live window fill) is `context_tokens`: the last sub-call's
      // prompt size. Without it the bar would show e.g. 433k against a 128k window
      // after a tool-heavy turn. Older backends omit it → fall back to input_tokens.
      const prev = s.usage;
      // `credits` is the Olano gateway's authoritative per-turn charge; BYOK
      // turns never send it. Kept as its own running total — a different
      // accounting system from `usd`, never summed into it.
      const turnCredits = (prev?.credits ?? 0) + (f.credits ?? 0);
      s.usage = {
        input: (prev?.input ?? 0) + (f.input_tokens ?? 0),
        output: (prev?.output ?? 0) + (f.output_tokens ?? 0),
        usd: (prev?.usd ?? 0) + (f.usd ?? 0),
        credits: turnCredits || undefined,
        // Model sub-calls this turn (a tool-heavy turn is several). Older
        // backends omit the field — fall back to 1, the turn itself.
        calls: (prev?.calls ?? 0) + (f.model_calls ?? 1),
        toolCalls: prev?.toolCalls,
        context: f.context_tokens ?? f.input_tokens ?? f.total_tokens ?? prev?.context,
      };
      break;
    }
    case "status":
      // The agent declined under its reply policy (`message_policy.ignore_rules`)
      // and the runner kept the marker off the stream, so this turn has no
      // text of its own. On a messenger that silence is the feature; on the
      // operator's console it would read as a dropped connection, so name the
      // rule. Only this kind is drawn: "stopped" is already handled by the
      // stop action's own local state, and drawing it twice would duplicate.
      if (m && f.kind === "no_reply" && f.message) {
        // Asterisk emphasis, not underscore: the renderer applies CommonMark's
        // word-boundary rule to `_`, and this message ends in `).` — the
        // closing underscore was drawn literally in the first cut.
        appendText(m, `${m.body ? "\n\n" : ""}*${f.message}*`);
      }
      break;
    case "error":
      if (m) {
        appendText(m, `\n\n**${errorLabel}:** ${f.message}`);
        // Attach a remediation so the chat can render a fix-it CTA (reconnect a
        // provider, open Providers, …) beneath the message.
        if (f.remediation) m.remediation = f.remediation;
      }
      break;
    case "done":
      // Terminal frame: send() tracks its receipt to tell a clean completion
      // from a mid-turn connection drop (a dead socket also ends the read
      // loop, indistinguishably, without this frame). It also names the
      // turn's Langfuse trace when the turn was traced.
      if (m && m.role === "assistant" && f.trace_id) m.traceId = f.trace_id;
      if (m && m.role === "assistant" && f.turn_id) m.turnId = f.turn_id;
      break;
    case "resync":
      // The run evicted the frames this tab's cursor pointed at (a very long
      // tool-heavy turn while the phone was away). Rendering the gap would be
      // a hole nobody can see, so take the persisted transcript instead — the
      // attach loop calls refreshThread when it sees this.
      break;
    default:
      break;
  }
}

// Adopt the live run a thread payload reported (`GET /threads/{id}` → `run`).
//
// **From cursor 0, deliberately** — not from the run's current head, which the
// payload also reports. A tab that has just loaded has seen no frames of this
// turn, and the transcript it rendered does not contain them either: the
// assistant's row is written when the turn ENDS, so a reply in flight lives
// only in the run's buffer. Resuming from the head would therefore draw the
// second half of a reply under a missing first half — which is what an
// end-to-end reload test caught. There is no double-render risk to trade
// against: while a run is live its turn has no persisted assistant row, and a
// finished run is never reported here (`for_thread` is live-only).
function adoptRun(s: Session, run: ThreadDetail["run"]): void {
  if (!run || !run.running) {
    s.runId = null;
    return;
  }
  s.runId = run.run_id;
  s.turnId = run.turn_id || null;
  s.cursor = 0;
}

// ── interrupted-turn reconcile helpers ───────────────────────────────────────
// Content of the conversation tail (everything after the last user message),
// summarized for comparison. Message counts are NOT comparable between a live
// session and a fromThread() rebuild (a cut turn can end on a tool host whose
// reply row never came; older transcripts fold differently), so compare
// content instead.
function tailStats(msgs: Msg[]): { bodyLen: number; toolsDone: number } {
  let bodyLen = 0;
  let toolsDone = 0;
  for (let i = msgs.length - 1; i >= 0 && msgs[i].role !== "user"; i--) {
    const m = msgs[i];
    if (m.role !== "assistant") continue;
    bodyLen += m.body.length;
    toolsDone += m.tools.filter((c) => c.hasResult).length;
  }
  return { bodyLen, toolsDone };
}

// Did the server persist more of the interrupted turn than the stream
// delivered? The runner's finally writes whatever streamed (and, if the turn
// actually finished server-side, the full reply) to the thread JSONL — so a
// longer assistant body or more completed tool calls means there is content
// worth adopting.
function serverIsRicher(server: Msg[], local: Msg[]): boolean {
  if (server.length === 0) return false;
  const sv = tailStats(server);
  const lc = tailStats(local);
  return sv.bodyLen > lc.bodyLen || sv.toolsDone > lc.toolsDone;
}

// Send the next queued message as a follow-up turn once the session is idle.
// Fires from send()'s finally (natural completion) and from a steer's abort —
// the common path for both "type-ahead queue" and "interrupt + resend". The
// per-turn opts (error label, model) are carried forward from the turn that just
// finished so a queued message inherits the same context.
function drainQueue(s: Session, opts: SendOpts): void {
  if (s.streaming || s.attaching || s.queue.length === 0) return;
  const next = s.queue.shift();
  bump(s);
  if (!next) return;
  void chatStore.send(s.agent, s.key, {
    text: next.text,
    attachments: next.attachments,
    mode: next.mode,
    model: opts.model,
    reasoning_effort: opts.reasoning_effort,
    errorLabel: opts.errorLabel,
  });
}

// ── mid-turn steering (network half; the rules live in steering.ts) ─────────
// Hand a chip to the running turn. Optimistic: the chip already reads "at the
// agent's next step". A refusal — the turn ended meanwhile (409 turn_ended), or
// any failure — demotes it to a plain queued chip, which drainQueue sends as
// the next turn. The chip may already be gone (delivered, or cancelled) by the
// time the answer lands; then there is nothing to demote.
async function postSteer(s: Session, q: QueuedMsg): Promise<void> {
  try {
    const res = await api.post<{ accepted: boolean; steer_id: string; turn_id: string }>("/api/chat/steer", {
      agent: s.agent,
      thread_id: s.threadId,
      turn_id: s.turnId,
      message: q.text,
      steer_id: q.steerId,
    });
    if (res?.steer_id && res.steer_id !== q.steerId) q.steerId = res.steer_id;
  } catch {
    if (!s.queue.includes(q)) return;
    q.kind = "queue";
    q.steerId = undefined;
    bump(s);
  }
}

// Withdraw a steer the model has not read yet. "delivered" means it already
// did — the transcript shows it, there is nothing to take back.
async function cancelSteer(s: Session, q: QueuedMsg): Promise<"cancelled" | "delivered" | "unknown"> {
  if (!s.threadId || !q.steerId) return "unknown";
  try {
    await api.post("/api/chat/steer/cancel", { agent: s.agent, thread_id: s.threadId, steer_id: q.steerId });
    return "cancelled";
  } catch (e) {
    return e instanceof ApiError && e.code === "already_delivered" ? "delivered" : "unknown";
  }
}

// ── public store API ──────────────────────────────────────────────────────────
export const chatStore = {
  // useSyncExternalStore wiring (stable references).
  subscribe(l: () => void): () => void {
    listeners.add(l);
    return () => listeners.delete(l);
  },
  getSnapshot(): number {
    return version;
  },
  // Fires when the backend thread list should be re-fetched (new thread created,
  // turn completed). ChatView re-runs its loadThreads on this.
  subscribeThreads(l: () => void): () => void {
    threadsListeners.add(l);
    return () => threadsListeners.delete(l);
  },
  subscribeError(l: (code: ErrorCode) => void): () => void {
    errorListeners.add(l);
    return () => errorListeners.delete(l);
  },

  sessionsFor,
  // Aggregate live stats across every agent's sessions — drives the sidebar
  // Chat indicator: `busy` is true while any turn is streaming (thinking
  // pulse), `unread` is the total of replies that landed while their thread
  // wasn't on screen. Call from render via the `version` snapshot, never store
  // the returned object in getSnapshot (a fresh object would loop the store).
  chatStats(): { unread: number; busy: boolean } {
    let unread = 0;
    let busy = false;
    for (const s of sessions.values()) {
      unread += s.unread;
      if (s.streaming || s.activity) busy = true;
    }
    return { unread, busy };
  },
  // The same two signals for ONE agent — what the sidebar's bot rows show
  // beside a name. Same rules as chatStats, and the same warning: read it from
  // render via the `version` snapshot, never store the object.
  agentStats(agent: string): { unread: number; busy: boolean } {
    let unread = 0;
    let busy = false;
    for (const s of sessionsFor(agent)) {
      unread += s.unread;
      if (s.streaming || s.activity) busy = true;
    }
    return { unread, busy };
  },
  getSession(agent: string, key: string | null | undefined): Session | undefined {
    return getSession(agent, key);
  },
  activeKeyOf(agent: string): string | undefined {
    return activeKey.get(agent);
  },
  // Peek the currently-active session for an agent (no creation — safe in render).
  activeSession(agent: string): Session | undefined {
    return getSession(agent, activeKey.get(agent));
  },

  // Ensure an agent has an active session (call from an effect, never in render).
  ensureActive(agent: string): Session {
    const existing = getSession(agent, activeKey.get(agent));
    if (existing) return existing;
    return this.newChat(agent);
  },

  // Start (or reuse) an empty draft conversation and make it active.
  newChat(agent: string): Session {
    const reuse = sessionsFor(agent).find(
      (s) => s.threadId === null && !s.streaming && s.messages.length === 0,
    );
    const s = reuse ?? createSession(agent, `draft:${++draftSeq}`);
    activeKey.set(agent, s.key);
    bump(s);
    return s;
  },

  setActive(agent: string, key: string): void {
    activeKey.set(agent, key);
    bump();
  },

  // The header's model picker: changes THIS conversation only ("" = back to
  // the agent's configured model). Nothing is remembered per agent — the next
  // new chat opens on the default again.
  setModel(agent: string, key: string, model: string): void {
    const s = getSession(agent, key);
    if (!s || s.model === model) return;
    s.model = model;
    bump(s);
  },

  setReasoningEffort(agent: string, key: string, effort: string): void {
    const s = getSession(agent, key);
    if (!s || s.reasoningEffort === effort) return;
    s.reasoningEffort = effort;
    bump(s);
  },

  // Open a thread from the rail. Reuses the live in-memory session if one exists
  // (preserving an in-flight stream), otherwise creates one and loads history.
  //
  // `allowMissing` is for a thread this browser is about to CREATE: the Cortex
  // hand-off opens `pulse:<id>` before any turn has run on it, and the runtime
  // has no transcript to serve until one does. A 404 there is the expected
  // answer, not a failure to report — the session simply starts empty and the
  // first send brings the thread into existence under that id. Without it the
  // hand-off would greet every new discussion with "Couldn't open".
  async openThread(agent: string, threadId: string, opts?: { allowMissing?: boolean }): Promise<void> {
    const live = sessionsFor(agent).find((x) => x.threadId === threadId || x.key === threadId);
    if (live) {
      activeKey.set(agent, live.key);
      this.markRead(agent, live.key);
      return;
    }
    const s = createSession(agent, threadId);
    activeKey.set(agent, s.key);
    this.markRead(agent, s.key);
    bump(s);
    try {
      const res = await api.get<ThreadDetail>(`/api/agents/${enc(agent)}/threads/${enc(threadId)}`);
      s.messages = fromThread(res.messages || []);
      s.usage = seedUsage(res);
      // Continue on the model the conversation last ran on, not on whatever
      // was picked for some other chat. ChatView drops it back to the default
      // (and says so) if the catalog no longer offers it.
      const restored = restoreModelFromThread(res);
      s.model = restored.model;
      s.reasoningEffort = restored.reasoningEffort;
      // The agent may be mid-turn on this thread right now — a run this tab
      // started before it reloaded, or one from another device. Pick it up from
      // the transcript's end so the reply keeps drawing instead of appearing to
      // have stopped. This is what makes a page reload during a turn survivable.
      adoptRun(s, res.run);
      bump(s);
      if (s.runId) void this.attach(agent, s.key);
    } catch {
      if (!opts?.allowMissing) emitError("open");
    }
  },

  // Append a local-only system note (slash output like /help) to a session.
  pushSystem(agent: string, key: string, body: string): void {
    const s = getSession(agent, key);
    if (!s) return;
    s.messages.push({ id: ++msgSeq, role: "system", body, tools: [], subs: [] });
    bump(s);
  },

  // Append a /context usage card (a system message carrying a ContextReport that
  // renders as <ContextCard> instead of plain markdown).
  pushContextCard(agent: string, key: string, report: ContextReport): void {
    const s = getSession(agent, key);
    if (!s) return;
    s.messages.push({ id: ++msgSeq, role: "system", body: "", tools: [], subs: [], context: report });
    bump(s);
  },

  // Re-seed the header's context bar after a server-side compaction.
  // `usage.context` is otherwise the last STREAMED turn's measured fill, and
  // `/compact` streams nothing — so without this the bar (and the next
  // /context card) kept reporting the pre-compaction number until another
  // turn ran, which read as "compact did nothing". The value is the server's
  // estimate of the post-compaction fill (static footprint + effective
  // messages); the next real turn replaces it with a measured one.
  setContextTokens(agent: string, threadId: string, tokens: number): void {
    const s = sessionsFor(agent).find((x) => x.threadId === threadId);
    if (!s) return;
    s.usage = { ...(s.usage ?? blankUsage()), context: tokens };
    bump(s);
  },

  // Append a magic-link outcome pushed live over SSE (see connectNotices.ts).
  // Deduped on linkId because the same outcome can reach one open thread twice:
  // the frame arrives, and the persisted row is already in the transcript if
  // the thread was (re)loaded in between. Delivery is once-per-link server-side
  // (magic_links.store.claim_finalize), so a second sighting is always the
  // same event, never a new one.
  // Addressed by THREAD id, not session key: the frame comes from a server that
  // knows the thread the link was minted in and nothing about this tab's
  // sessions. A thread that isn't loaded here is simply not updated — the
  // persisted row draws the same card when it is next opened.
  pushConnectResult(agent: string, threadId: string, result: MagicLinkResult): void {
    const s = sessionsFor(agent).find((x) => x.threadId === threadId);
    if (!s) return;
    if (result.linkId && s.messages.some((m) => m.connectResult?.linkId === result.linkId)) return;
    s.messages.push({
      id: ++msgSeq,
      role: "system",
      body: result.text,
      tools: [],
      subs: [],
      ts: Date.now(),
      connectResult: result,
    });
    bump(s);
  },

  // Fold a transcript row that another surface just persisted (a `thread_updated`
  // frame — see threadSync.ts) into the open session for that thread. Rows for a
  // thread this tab has not loaded are dropped: the persisted row draws itself
  // when the thread is next opened, and the rail refresh (also driven by the
  // frame) is what surfaces a brand-new conversation.
  applyThreadRow(agent: string, threadId: string, row: ThreadMessage): void {
    const s = remoteTarget(agent, threadId);
    if (!s) return;
    if (row.role === "system" && row.event === "magic_link") {
      // Also delivered as its own `magic_link_updated` frame — reuse that
      // path's linkId dedupe instead of drawing the card twice.
      this.pushConnectResult(agent, threadId, magicLinkFromRow(row));
      return;
    }
    if (isEcho(s, row)) return;
    foldRow(s.messages, row);
    if (s.activity && row.role === "assistant") {
      // The reply landed in full; the streamed preview has nothing left to add
      // (the `idle` frame that follows clears the indicator itself).
      s.activity = { ...s.activity, text: "" };
    }
    s.lastActivity = Date.now();
    // Count a reply that landed while the thread wasn't on screen, exactly as
    // send() does for this tab's own turns — never a tool row or the human's
    // own message echoed back from Telegram. No length check: a reply row
    // always lands (pushed, or merged into the turn's tool host — see
    // foldRow), and isEcho has already dropped a repeat.
    if (!isViewing(s) && row.role === "assistant") s.unread += 1;
    bump(s);
  },

  // A detached run started on a thread (`run_started`). If this tab has that
  // thread open and is not already following the run, attach: a turn kicked off
  // on a phone then draws live on the laptop too, tool cards and all, instead of
  // waiting for the throttled `thread_activity` preview or a reload.
  //
  // Its own turns are skipped — `remoteTarget` already ignores a session that is
  // streaming, and the run this tab started is the one it is streaming.
  // This person's thumbs on one reply, as the server recorded it (`null`: the
  // vote was taken back). The chat's ReplyFeedback calls it after the POST.
  setFeedback(agent: string, threadId: string, msgId: number, vote: ReplyVote | null): void {
    const s = sessionsFor(agent).find((x) => x.threadId === threadId);
    const m = s?.messages.find((x) => x.id === msgId);
    if (!s || !m) return;
    if (vote) m.feedback = vote;
    else delete m.feedback;
    bump(s);
  },

  noteRunStarted(agent: string, threadId: string, runId: string, turnId: string): void {
    const s = remoteTarget(agent, threadId);
    if (!s || s.runId === runId || s.attaching) return;
    s.runId = runId;
    s.turnId = turnId || null;
    // A run this tab never saw starts at its beginning: replay it whole.
    s.cursor = 0;
    bump(s);
    void chatStore.attach(agent, s.key);
  },

  // …and its end. Only the run named is cleared, so a `run_ended` arriving late
  // (after a newer turn began on the same thread) cannot silence the live one.
  noteRunEnded(agent: string, threadId: string, runId: string): void {
    const s = sessionsFor(agent).find((x) => x.threadId === threadId);
    if (!s || s.runId !== runId) return;
    s.runId = null;
    bump(s);
  },

  // Mirror the turn state of a thread being worked from another surface
  // (`thread_activity` frames). `idle` clears it and settles a dangling plan
  // step, exactly as the end of a local turn does.
  setRemoteActivity(
    agent: string,
    threadId: string,
    status: string,
    detail: { channel?: string; text?: string } = {},
  ): void {
    const s = remoteTarget(agent, threadId);
    if (!s) return;
    if (status === "idle") {
      if (!s.activity) return;
      s.activity = null;
      const last = s.messages[s.messages.length - 1];
      if (last && last.role === "assistant" && last.todos) last.todos = settleTodos(last.todos);
      bump(s);
      return;
    }
    if (status !== "thinking" && status !== "streaming") return;
    const prev = s.activity;
    s.activity = {
      status,
      channel: detail.channel || prev?.channel || "",
      // A `thinking` frame mid-turn (the model calling tools between two text
      // spans) keeps the words already previewed instead of blanking them.
      text: status === "streaming" ? detail.text ?? prev?.text ?? "" : prev?.text ?? "",
      at: Date.now(),
    };
    // Preview frames arrive at stream cadence — coalesce like token frames.
    if (status === "streaming") bumpCoalesced(s);
    else bump(s);
  },

  // Re-pull every loaded, idle thread from the server. Runs when the event
  // stream comes back after a drop and when a tab resumes from a long sleep —
  // the two windows in which `thread_updated` rows can have been missed
  // (a phone in a pocket keeps no EventSource alive). One GET per open thread,
  // so it is gated on those signals rather than run on a timer.
  async resyncOpenThreads(): Promise<void> {
    const targets = [...sessions.values()].filter(
      (s) => s.threadId && !s.streaming && !s.attaching && !s.reconciling,
    );
    await Promise.all(
      targets.map(async (s) => {
        if (!s.threadId) return;
        try {
          const res = await api.get<ThreadDetail>(`/api/agents/${enc(s.agent)}/threads/${enc(s.threadId)}`);
          const server = fromThread(res.messages || []);
          // A turn may have started on this thread from somewhere else while
          // this tab was asleep. Learn about it here so the resume path can
          // attach rather than wait for the next broadcast frame.
          if (!s.runId) adoptRun(s, res.run);
          // Adopt the server copy only when it says more than this tab knows —
          // a resync must never wipe a reply the tab already rendered, and an
          // unchanged thread must not re-key every bubble for nothing.
          if (server.length > s.messages.length || serverIsRicher(server, s.messages)) {
            s.messages = server;
            s.usage = seedUsage(res) ?? s.usage;
            bump(s);
          }
        } catch {
          // Unreachable server: the next reconnect tries again.
        }
      }),
    );
  },

  // Record a shell run (composer "!" or inline "Run Now"): show it as a
  // local-only block in the timeline AND queue it for the next turn. Never sent
  // to the agent until the user acts.
  pushShell(agent: string, key: string, result: ShellRunResult): void {
    const s = getSession(agent, key);
    if (!s) return;
    s.messages.push({ id: ++msgSeq, role: "system", body: shellResultMarkdown(result), tools: [], subs: [] });
    s.shellBuffer = [...s.shellBuffer, result];
    bump(s);
  },

  shellBufferOf(agent: string, key: string | null | undefined): ShellRunResult[] {
    return getSession(agent, key)?.shellBuffer ?? [];
  },

  clearShell(agent: string, key: string): void {
    const s = getSession(agent, key);
    if (!s || s.shellBuffer.length === 0) return;
    s.shellBuffer = [];
    bump(s);
  },

  // Re-fetch a thread's history into its live session (after /compact rewrites it).
  async refreshThread(agent: string, threadId: string): Promise<void> {
    const s = sessionsFor(agent).find((x) => x.threadId === threadId);
    if (!s) return;
    try {
      const res = await api.get<{ messages: ThreadMessage[] }>(
        `/api/agents/${enc(agent)}/threads/${enc(threadId)}`,
      );
      s.messages = fromThread(res.messages || []);
      bump(s);
    } catch {
      emitError("open");
    }
  },

  // Rejoin a turn still running on the server.
  //
  // This is the recovery the "Connection lost" state used to stand in for. The
  // turn no longer dies with the socket (olano/core/runs.py), so a dropped
  // stream is a viewer that left, not work that stopped: reconnect from the
  // last frame `seq` this tab saw and the reply continues where it broke off —
  // tool cards, usage and approvals included, not just the text.
  //
  // Returns whether it rejoined. `false` means the run is genuinely gone (it
  // finished long enough ago to be swept, or the runtime restarted), and the
  // caller falls back to reconcile()/interrupted, which is what always happened.
  async attach(agent: string, key: string, opts?: { errorLabel?: string }): Promise<boolean> {
    const s = getSession(agent, key);
    if (!s || !s.threadId || s.streaming || s.attaching) return false;
    const errorLabel = opts?.errorLabel ?? s.lastOpts?.errorLabel ?? "Error";
    // Somewhere to stream into. `applyFrame` appends text to the LAST message,
    // and after a page reload the transcript ends on the user's row — the
    // assistant's is written only when the turn finishes — so without this the
    // rejoined reply would be appended to the reader's own message.
    let last = s.messages[s.messages.length - 1];
    if (!last || last.role !== "assistant") {
      last = {
        id: ++msgSeq,
        role: "assistant",
        body: "",
        tools: [],
        subs: [],
        approvals: [],
        ts: Date.now(),
      };
      s.messages.push(last);
    }
    // The bubble this rejoin streams into — the settle below must reach it and
    // anything a steer splits off after it, and nothing from an earlier turn.
    const attachId = last.id;
    const ctrl = new AbortController();
    s.attaching = true;
    s.abort = ctrl;
    bump(s);
    let sawTerminal = false;
    let needsResync = false;
    let gone = false;
    try {
      await attachChat(
        { agent, thread_id: s.threadId, run_id: s.runId || undefined, cursor: s.cursor },
        (f) => {
          if (f.type === "done" || f.type === "error") sawTerminal = true;
          else if (f.type === "resync") needsResync = true;
          applyFrame(s, f, errorLabel);
          s.lastActivity = Date.now();
          bumpCoalesced(s);
        },
        ctrl.signal,
      );
    } catch (e) {
      // A 404 is the definitive answer: there is no such run, so stop believing
      // in one. Leaving `runId` set would have every later resume re-attach to
      // the same missing run instead of falling back to reconcile().
      if (e instanceof StreamRequestError && e.status === 404) gone = true;
      // Anything else (offline again, a 5xx) is not a rejoin either, but it is
      // not proof the run ended — keep `runId` so the next resume tries again.
      return false;
    } finally {
      s.attaching = false;
      s.abort = null;
      s.turnId = null;
      s.lastActivity = Date.now();
      if (gone) {
        s.runId = null;
        // The run is gone, so any approval still open on it was auto-rejected
        // server-side — mark it so the card can't offer a decision that 404s.
        // (`send()` skipped this when it deferred to the attach.)
        let hadPending = false;
        for (const mm of s.messages) {
          for (const ap of mm.approvals ?? []) {
            if (!ap.resolved) {
              ap.resolved = true;
              ap.expired = true;
              hadPending = true;
            }
          }
        }
        if (s.interrupted) s.interrupted = { ...s.interrupted, hadPendingApproval: hadPending };
      }
      if (sawTerminal) {
        s.runId = null;
        s.interrupted = null;
        s.recoveredAt = Date.now();
        const turnMsgs = s.messages.filter((mm) => mm.role === "assistant" && mm.id >= attachId);
        for (const mm of turnMsgs) {
          if (!mm.remediation && mm.todos) mm.todos = settleTodos(mm.todos);
        }
        s.ownTurnEndedAt = Date.now();
      }
      bump(s);
      // Held messages are released only once the thread is consistent again —
      // same rule reconcile() follows.
      if (sawTerminal && !s.interrupted && s.lastOpts) drainQueue(s, s.lastOpts);
    }
    if (needsResync && s.threadId) await this.refreshThread(agent, s.threadId);
    // The stream ended without a terminal frame: the socket dropped a second
    // time. Still a live run as far as we know — the next resume attaches again.
    return sawTerminal || needsResync;
  },

  // After an interrupted turn, pull the authoritative thread history and adopt
  // it when it holds more of the reply than the stream delivered (the runner
  // persists whatever streamed before the cut — and the full reply when the
  // turn finished server-side). When the server has nothing more, the partial
  // and the Interrupted state stay so the view can offer Retry.
  async reconcile(agent: string, threadId: string): Promise<void> {
    const s = sessionsFor(agent).find((x) => x.threadId === threadId);
    // Not during an attach either: replacing `s.messages` would orphan the
    // bubble the attach is streaming into.
    if (!s || s.streaming || s.attaching || s.reconciling || !s.interrupted) return;
    s.reconciling = true;
    bump(s);
    try {
      const res = await api.get<ThreadDetail>(`/api/agents/${enc(agent)}/threads/${enc(threadId)}`);
      // Steers the model read while this tab was cut off are in the server's
      // rows; their chips must not drain as new turns (see steering.ts).
      s.queue = dropDeliveredSteers(s.queue, res.messages || []);
      const server = fromThread(res.messages || []);
      if (serverIsRicher(server, s.messages)) {
        s.messages = server;
        s.usage = seedUsage(res) ?? s.usage;
        s.interrupted = null;
        s.recoveredAt = Date.now();
      }
    } catch {
      // Server unreachable — keep the interrupted state; Retry stays available.
    } finally {
      s.reconciling = false;
      bump(s);
      // Messages queued behind the broken turn were held back — release them
      // only once the thread is known-consistent again.
      if (!s.interrupted && s.lastOpts) drainQueue(s, s.lastOpts);
    }
  },

  // Resend the last user message of an interrupted OR hard-errored turn as a
  // NEW turn (a request that failed before the stream opened previously left a
  // dead error bubble with no recovery affordance). The original user message
  // and any partial reply are already persisted server-side (no history
  // surgery); the model keeps context via the thread checkpoint.
  retryLast(agent: string, key: string): void {
    const s = getSession(agent, key);
    if (!s || s.streaming || (!s.interrupted && !s.error)) return;
    let lastUser: Msg | undefined;
    for (let i = s.messages.length - 1; i >= 0; i--) {
      // A steered aside is not the turn's prompt — Retry resends the message
      // that started the turn.
      if (s.messages[i].role === "user" && !s.messages[i].steer) {
        lastUser = s.messages[i];
        break;
      }
    }
    const opts = s.lastOpts;
    if (!lastUser || !opts) return;
    s.interrupted = null;
    s.error = undefined;
    bump(s);
    void this.send(agent, key, {
      ...opts,
      text: lastUser.body,
      attachments: lastUser.attachments ?? [],
      // Shell output was already merged into the original turn's wire message
      // (and the buffer drained) — don't re-merge anything on the retry.
      shellResults: [],
    });
  },

  setInput(agent: string, key: string, value: string): void {
    const s = getSession(agent, key);
    if (!s) return;
    s.input = value;
    bump(s);
  },

  setStaged(agent: string, key: string, next: Attachment[] | ((prev: Attachment[]) => Attachment[])): void {
    const s = getSession(agent, key);
    if (!s) return;
    s.staged = typeof next === "function" ? next(s.staged) : next;
    bump(s);
  },

  // Mark a session as the one the user is looking at: clears its unread badge.
  markRead(agent: string, key: string): void {
    viewing = { agent, key };
    const s = getSession(agent, key);
    if (s && s.unread !== 0) s.unread = 0;
    bump(s);
  },

  // The user left the chat view entirely — completions now count as unread.
  clearViewing(): void {
    viewing = null;
  },

  // Zero every unread badge an agent's sessions carry, WITHOUT claiming the
  // viewing pointer — this is "dismiss the badges" (the rail's context menu),
  // not "I am looking at this conversation", which stays markRead's job.
  clearUnread(agent: string): void {
    for (const s of sessionsFor(agent)) {
      if (s.unread !== 0) {
        s.unread = 0;
        bump(s);
      }
    }
  },

  // Remove a deleted thread's session; reset the active pointer if needed.
  dropThread(agent: string, threadId: string): void {
    const s = sessionsFor(agent).find((x) => x.threadId === threadId || x.key === threadId);
    if (s) {
      s.abort?.abort();
      sessions.delete(skey(agent, s.key));
      if (activeKey.get(agent) === s.key) activeKey.delete(agent);
    }
    bump();
  },

  // Stop the turn — for real, on the server.
  //
  // Aborting the local reader stopped a turn only while the turn WAS the socket.
  // A detached run outlives it (olano/core/runs.py), so without the POST the
  // button would quietly stop meaning stop: the agent would keep working (and
  // billing) behind a UI that says it stopped.
  stop(agent: string, key: string): void {
    const s = getSession(agent, key);
    if (!s) return;
    if (s.threadId) void stopChatRun({ agent, thread_id: s.threadId, run_id: s.runId });
    s.runId = null;
    // A stop is not a lost connection: `send()` sets `interrupted` ahead of an
    // attach expecting the attach to clear it, and Stop must not leave that
    // flag (and its Retry) standing over a turn the person ended on purpose.
    s.interrupted = null;
    s.abort?.abort();
  },

  // Queued messages (typed while a turn was streaming).
  queueOf(agent: string, key: string | null | undefined): QueuedMsg[] {
    return getSession(agent, key)?.queue ?? [];
  },

  // Add a message to the session's queue. If the session isn't streaming (the
  // turn finished between the user typing and hitting send), drain immediately so
  // the message isn't stranded. Returns true when it was queued behind a live turn.
  enqueue(agent: string, key: string, item: QueuedDraft, opts: SendOpts): boolean {
    const s = getSession(agent, key) ?? createSession(agent, key);
    s.queue.push({ id: ++msgSeq, kind: "queue", ...item });
    bump(s);
    if (!s.streaming && !s.attaching) {
      drainQueue(s, opts);
      return false;
    }
    return true;
  },

  // Steer = hand the draft to the turn that is running, without stopping it: the
  // model reads it at its next step (after the tool calls in flight, before its
  // next model call). Nothing is aborted and nothing is re-run. With no live turn
  // to steer — it ended between typing and clicking, the `start` frame hasn't
  // landed yet, or files are attached (v1 steers are text-only) — the draft is
  // queued instead, which sends it at once when nothing is streaming.
  steer(agent: string, key: string, item: QueuedDraft, opts: SendOpts): void {
    const s = getSession(agent, key) ?? createSession(agent, key);
    if (!s.streaming || !s.threadId || !s.turnId || item.attachments.length > 0 || !item.text.trim()) {
      this.enqueue(agent, key, item, opts);
      return;
    }
    const q: QueuedMsg = { id: ++msgSeq, kind: "steer", steerId: newSteerId(), ...item };
    s.queue.push(q);
    bump(s);
    void postSteer(s, q);
  },

  // Promote a queued chip to a steer (the chip's Steer button). Same rules as
  // steer(); a chip that can't be steered is left as it is.
  steerQueued(agent: string, key: string, id: number): void {
    const s = getSession(agent, key);
    if (!s) return;
    const q = s.queue.find((x) => x.id === id);
    if (!q || q.kind === "steer" || !s.streaming || !s.threadId || !s.turnId || q.attachments.length > 0) return;
    q.kind = "steer";
    q.steerId = newSteerId();
    bump(s);
    void postSteer(s, q);
  },

  // Take a chip back into the composer (the chip's Edit button). A steer chip
  // is withdrawn from the turn first; if the model has already read it there
  // is nothing to edit — the chip goes and the transcript shows the aside.
  async editQueued(agent: string, key: string, id: number): Promise<void> {
    const s = getSession(agent, key);
    if (!s) return;
    const q = s.queue.find((x) => x.id === id);
    if (!q) return;
    if (q.kind === "steer" && q.steerId) {
      const outcome = await cancelSteer(s, q);
      if (outcome === "delivered") {
        s.queue = s.queue.filter((x) => x !== q);
        bump(s);
        return;
      }
    }
    const r = takeBackQueued(s.queue, id, s.input, s.staged);
    if (!r) return;
    s.queue = r.queue;
    s.input = r.input;
    s.staged = r.staged;
    bump(s);
  },

  // Drop a chip (the chip's Cancel button). A steer chip is also withdrawn from
  // the turn; if the model already read it, the transcript keeps the aside.
  cancelQueued(agent: string, key: string, id: number): void {
    const s = getSession(agent, key);
    if (!s) return;
    const q = s.queue.find((x) => x.id === id);
    if (!q) return;
    s.queue = s.queue.filter((x) => x !== q);
    bump(s);
    if (q.kind === "steer" && q.steerId) void cancelSteer(s, q);
  },

  clearQueue(agent: string, key: string): void {
    const s = getSession(agent, key);
    if (!s || s.queue.length === 0) return;
    const gone = s.queue;
    s.queue = [];
    bump(s);
    for (const q of gone) if (q.kind === "steer" && q.steerId) void cancelSteer(s, q);
  },

  resolveApproval(agent: string, key: string, msgId: number, idx: number, choice: ApprovalChoice): void {
    const s = getSession(agent, key);
    if (!s) return;
    let count = 0;
    const m = s.messages.find((mm) => mm.id === msgId);
    const ap = m?.approvals?.[idx];
    // An approval that expired with its dropped stream can't be decided anymore
    // (the backend already auto-rejected it; POSTing now would just 404).
    if (ap?.expired) return;
    if (ap) {
      ap.resolved = true;
      ap.choice = choice;
      count = ap.actions.length;
    }
    if (choice === "all") s.autoApprove = true;
    bump(s);
    if (!s.threadId || !count) return;
    const type = choice === "reject" ? "reject" : "approve";
    void postDecisions(s.threadId, Array.from({ length: count }, () => ({ type })));
  },

  // Stream one user turn into a session. Runs to completion independently of any
  // mounted component — this is what keeps a conversation alive across nav.
  async send(agent: string, key: string, opts: SendOpts): Promise<void> {
    const s = getSession(agent, key) ?? createSession(agent, key);
    // An attach is a live turn too (olano/core/runs.py): a send during one would
    // push bubbles the attach then streams into, overwrite its AbortController,
    // and remove those bubbles again on the 409 — losing the replayed text.
    if (s.streaming || s.attaching) return;
    // Shell results to fold into this turn: an explicit set ("Send to Agent
    // now") or, by default, whatever's been buffered from "!" / "Run Now"
    // commands since the last message.
    const shell = opts.shellResults ?? s.shellBuffer;
    const hasShell = shell.length > 0;
    if ((!opts.text && opts.attachments.length === 0 && !hasShell) || !agent) return;
    // The agent receives prose + shell context as one human turn; the user's
    // bubble shows only what they typed (or the shell context, if they sent
    // shell output with no prose). No thread-history surgery needed.
    const wireMessage = hasShell ? composeWithShell(opts.text, shell) : opts.text;
    const visibleBody = opts.text || (hasShell ? wireMessage : "");
    // Drain the session buffer only when WE owned it (not the explicit path),
    // before the stream starts so an abort/retry can't re-append it.
    if (opts.shellResults === undefined && hasShell) s.shellBuffer = [];

    const userId = ++msgSeq;
    const assistantId = ++msgSeq;
    s.autoApprove = false;
    s.error = undefined;
    s.interrupted = null;
    s.recoveredAt = null;
    s.lastOpts = opts;
    const turnTs = Date.now();
    s.messages.push({ id: userId, role: "user", body: visibleBody, tools: [], subs: [], attachments: opts.attachments, ts: turnTs });
    s.messages.push({ id: assistantId, role: "assistant", body: "", tools: [], subs: [], approvals: [], ts: turnTs });
    s.streaming = true;
    s.lastActivity = Date.now();
    const wasNew = s.threadId === null;
    const ctrl = new AbortController();
    s.abort = ctrl;
    bump(s);

    let aborted = false;
    // Terminal-frame tracking: the server always ends a delivered turn with a
    // `done` (or `error`) frame, but a dead socket ends the read loop the same
    // way a finished stream does — so "stream returned without a terminal
    // frame" is the only reliable signal that the turn was cut mid-flight
    // (phone lock, network drop) rather than completed.
    let sawStart = false;
    let sawTerminal = false;
    // The server refused this turn because one is already running here.
    let conflicted = false;
    try {
      await streamChat(
        {
          message: wireMessage,
          agent,
          thread_id: s.threadId,
          mode: opts.mode,
          model: opts.model || undefined,
          reasoning_effort: opts.reasoning_effort || undefined,
          attachments: toWire(opts.attachments),
        },
        (f) => {
          if (f.type === "start") sawStart = true;
          else if (f.type === "done" || f.type === "error") sawTerminal = true;
          applyFrame(s, f, opts.errorLabel);
          s.lastActivity = Date.now();
          bumpCoalesced(s);
        },
        ctrl.signal,
      );
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") {
        aborted = true;
      } else if (e instanceof StreamRequestError && e.status === 409) {
        // A turn is already running on this thread — from another device, or
        // from this one before a reconnect made it look idle. The server
        // refuses a second one rather than braid two replies into one
        // transcript, and it names the live run. Watching it is what the person
        // wanted anyway, so join it instead of showing them a conflict; the
        // message they just typed goes back into the queue, where a turn in
        // flight always puts it.
        conflicted = true;
        s.messages = s.messages.filter((m) => m.id !== userId && m.id !== assistantId);
        s.queue.push({
          id: ++msgSeq,
          kind: "queue",
          text: opts.text,
          attachments: opts.attachments,
          mode: opts.mode,
        });
      } else if (!sawStart) {
        // The request failed before the stream was established (HTTP error,
        // server unreachable) — a real error, shown as one.
        const msg = e instanceof Error ? e.message : "chat failed";
        const last = s.messages[s.messages.length - 1];
        if (last) appendText(last, `\n\n**${opts.errorLabel}:** ${msg}`);
        s.error = msg;
        emitError("chat");
      }
      // else: the stream broke mid-turn — handled as `interrupted` in the
      // finally below (partial kept, reconciled on resume), not as an error.
    } finally {
      s.abort = null;
      s.streaming = false;
      s.turnId = null;
      s.lastActivity = Date.now();
      const last = s.messages[s.messages.length - 1];
      // Every assistant bubble this turn produced: a steer splits the reply,
      // so the settling below must reach the bubbles BEFORE the aside too, or
      // one of them keeps a "stuck on step N" dot or a live-looking approval.
      const turnMsgs = s.messages.filter((mm) => mm.role === "assistant" && mm.id >= assistantId);
      // The stream established but ended without a terminal frame and without a
      // user abort: the connection died mid-turn. Flag the session so the view
      // shows the Interrupted state and the resume subscription reconciles it.
      const truncated = !aborted && !s.error && !conflicted && sawStart && !sawTerminal;
      if (truncated && s.runId) {
        // The socket died but the RUN did not — it is still working on the
        // server. Rejoin it rather than declaring the turn lost: this is the
        // whole reason the run was detached. `attach` clears `interrupted` on
        // success and leaves it set when the run really is gone, so the
        // Interrupted/Retry affordance still appears when it should.
        s.interrupted = { at: Date.now(), hadPendingApproval: false };
        bump(s);
        void this.attach(agent, s.key, { errorLabel: opts.errorLabel });
      } else if (truncated) {
        // The backend auto-rejects a pending approval when its stream dies (the
        // handler's finally resolves the slot empty) — mirror that here so the
        // card can't offer a decision that would only 404.
        let hadPendingApproval = false;
        for (const mm of turnMsgs) {
          for (const ap of mm.approvals ?? []) {
            if (!ap.resolved) {
              ap.resolved = true;
              ap.expired = true;
              hadPendingApproval = true;
            }
          }
        }
        s.interrupted = { at: Date.now(), hadPendingApproval };
      }
      // A reply landed while the user was looking elsewhere — badge it.
      if (!aborted && !isViewing(s) && producedContent(last)) s.unread += 1;
      // The turn finished normally but the model may have left its last step
      // marked in_progress (it often skips the closing write_todos). Settle it so
      // the plan doesn't render a static "stuck on step N" dot. Aborted/errored/
      // interrupted turns are left as-is — an unfinished step there is honest.
      if (!aborted && !s.error && !s.interrupted) {
        for (const mm of turnMsgs) {
          if (!mm.remediation && mm.todos) mm.todos = settleTodos(mm.todos);
        }
      }
      // A completed Plan-mode turn is a drafted plan awaiting the user's call —
      // flag it so the chat shows the plan action bar (unless a tool is still
      // pending approval).
      if (
        !aborted &&
        !s.interrupted &&
        opts.mode === "plan" &&
        last &&
        last.role === "assistant" &&
        last.body.trim()
      ) {
        const pendingApproval = (last.approvals || []).some((a) => !a.resolved);
        if (!pendingApproval) last.planProposal = true;
      }
      // Rows this turn persisted are still in flight on /api/stream — see
      // remoteTarget for why the next moment must ignore them.
      s.ownTurnEndedAt = Date.now();
      bump(s);
      if (wasNew) notifyThreads(); // surface the freshly-created thread in the rail
      // Type-ahead / steer: if the user queued messages while this turn ran (or
      // steered, which aborts into here), send the next one now. Runs for both
      // natural completion and abort so a steer fires immediately. An
      // interrupted turn holds the queue until reconcile() restores a
      // known-consistent thread (or Retry clears the flag).
      if (!s.interrupted && !conflicted) drainQueue(s, opts);
    }
    // Join the turn the server said was already running. Outside the finally so
    // the session is settled (not streaming) before `attach` claims it.
    if (conflicted) void this.attach(agent, s.key, { errorLabel: opts.errorLabel });
  },
};

// Reconcile interrupted conversations when the tab resumes (phone unlock,
// network back). Lives at module scope because the store outlives any mounted
// view — symmetric with the "streams survive navigation" design above. No
// shouldRefetch gate here: `interrupted` is itself rare and self-clearing, so
// every resume is worth one thread GET per flagged conversation (unlike the
// store-wide refetches, which the gate keeps cheap).
subscribeResume((e) => {
  for (const s of sessions.values()) {
    if (!s.threadId || s.streaming || s.attaching) continue;
    // A turn left running when the tab went away is rejoined, not mourned: the
    // server kept working (olano/core/runs.py) and `cursor` says what we missed.
    // Only when there is no run to rejoin does the old path apply — refetch the
    // transcript and, failing that, offer Retry.
    if (s.runId) {
      const key = s.key;
      void chatStore.attach(s.agent, key).then((rejoined) => {
        if (!rejoined && s.interrupted && s.threadId) {
          void chatStore.reconcile(s.agent, s.threadId);
        }
      });
    } else if (s.interrupted) {
      void chatStore.reconcile(s.agent, s.threadId);
    }
  }
  // A long sleep (or a dropped stream) can have swallowed `thread_updated`
  // rows for a turn that ran from Telegram or the scheduler meanwhile; this
  // one is gated, since it costs a GET per open thread.
  if (shouldRefetch(e)) void chatStore.resyncOpenThreads();
});
