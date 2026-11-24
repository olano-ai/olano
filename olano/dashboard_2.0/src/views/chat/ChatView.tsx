import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type MouseEvent,
} from "react";
import { ChatBrowserDock } from "../browser/ChatBrowserDock";
import { needsPersonEveryTime } from "../browser/approval";
import { countBrowsingCalls } from "../browser/dockActivity";
import { ArrowDown, Clock, FastForward, History, Info, Paperclip, Pencil, Play, RotateCcw, Slash, Sparkles, Square, Trash2, WifiOff, X } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../../core/api";
import { agentLabel } from "../agents/helpers";
import { useAuth } from "../../core/auth";
import { useMediaQuery } from "../../core/useMediaQuery";
import { useI18n, useT } from "../../core/i18n";
import { fmtCreditsAmount, fmtUsd, useCostDisplay } from "../../core/costDisplay";
import { useCurrentAgent } from "../../core/currentAgent";
import { Markdown } from "../../ui/Markdown";
import { CheckIcon, CopyIcon, MicIcon, PlusIcon, SendIcon, SubIcon, TerminalIcon, ToolsIcon } from "../../ui/icons";
import { runShell } from "../../core/shell";
import { useCapability } from "../../core/capabilities";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import { useDialogA11y } from "../../ui/dialogA11y";
import { ChatHeader } from "./ChatHeader";
import { ApprovalCard, type ApprovalChoice } from "./ApprovalCard";
import { ConnectResultCard } from "./ConnectResultCard";
import { ContextCard } from "./ContextCard";
import { ErrorRemediation } from "./ErrorRemediation";
import { AgentMedia, MessageAttachments, StagedAttachments } from "./AttachmentViews";
import { TodoList } from "./TodoList";
import { fileIndexFor } from "./fileLinks";
import { buildTurnBlocks } from "./buildTurnBlocks";
import { QueuedChip } from "./QueuedChip";
import { ingestFile, type Attachment } from "./attachments";
import { getPrefs, nextDetail, nextMode, savePrefs, type ChatDetail, type ChatMode, type ChatPrefs } from "./prefs";
import { effectiveChatModel, restoredModelUsable } from "./chatModel";
import { useCatalog, modelOptions } from "../agents/catalog";
import type { CommandCatalogResponse } from "../agents/types";
import { modelContextWindow } from "./models";
import { SLASH_COMMANDS, parseSlash, type SlashCommand } from "./slash";
import { ChannelPill, channelLabel, isPlatformChannel, ORIGIN_ICONS, OriginPill } from "./channels";
import { TAP_THRESHOLD_MS, useVoiceInput, type VoiceErrorCode } from "./voice";
import { parseTaskArgs, peekText } from "./toolPeek";
import { ConnectCard } from "./ConnectCard";
import { extractConnectLinks } from "./connectLinks";
import { threadListStore, useThreadList } from "./threadListStore";
import { focusComposerEnd, shouldPreloadDraft, takeChatHandoff, type ChatHandoff } from "./handoff";
import { composeSharedDraft, readSharedPayload, stripShareParams } from "./shareTarget";
import { AddBotToChatDialog } from "../groups/AddBotToChatDialog";
import { useGroupsEnabled } from "../groups/groupsStore";
import { BotProfilePanel } from "../simple/BotProfilePanel";
import {
  chatStore,
  type ContextCategory,
  type ContextReport,
  type Msg,
  type QueuedMsg,
  type SendOpts,
  type Session,
} from "./sessionStore";
import { STR } from "./strings";
import { LangfuseLink } from "../observability/LangfuseLink";
import { ReplyFeedback } from "./ReplyFeedback";
import { traceUrl, useLangfuseLinks } from "../observability/links";

type T = (key: string, vars?: Record<string, string>) => string;

// How long the slash-menu reference data (skills / tools / commands catalogs)
// may be served from the client cache. These back listings the user only sees
// after typing "/", they're expensive server-side, and chat refetched all three
// on every mount and agent switch. The explicit /reload-* commands invalidate
// their entry first, so a deliberate refresh is never served from here.
const CATALOG_TTL_MS = 60_000;

// Stable empty fallbacks so reads on a not-yet-created session don't churn refs.
const EMPTY_MSGS: Msg[] = [];
const EMPTY_ATT: Attachment[] = [];
const EMPTY_QUEUE: QueuedMsg[] = [];

// A row in the conversation rail: a persisted backend thread, possibly fronted
// by a live in-memory session (streaming dot / unread badge), or a brand-new
// session still streaming its first turn before it has a thread id.
interface Row {
  key: string;
  threadId: string | null;
  name: string;
  preview: string;
  // Server-derived label (see core/thread_labels.py). Empty for a live session
  // that has no backend thread yet.
  title: string;
  origin: string;
  channel: string;
  sender: string;
  messageCount: number;
  updatedAt: string | null;
  sortTs: number;
  streaming: boolean;
  unread: number;
  // Per-thread cost, one field per billing book — never summed into each other.
  // `usd` is the BYOK bill, mined from the checkpoint cache (absent until the
  // thread is indexed; Olano-managed calls price to $0 there by design).
  // `credits` is the Olano gateway's own charge, accumulated per turn into the
  // thread's state file (runner._bump_thread_credits) because no store the
  // mining reads carries it. A thread whose model was switched mid-conversation
  // legitimately has both.
  usd?: number | null;
  credits?: number | null;
}

// Live per-server MCP health for the /mcps listing — mirrors the chat MCP
// indicator's popover (GET /api/agents/{name}/mcp/health).
interface McpHealthRow {
  url: string;
  transport: string;
  status: string;
  ok: boolean | null;
  tool_count: number;
  enabled: boolean;
}

function initials(name: string): string {
  const parts = (name || "?").split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0]?.[0] || "?") + (parts[1]?.[0] || "")).toUpperCase();
}

function relTime(iso: string | null | undefined, t: T): string {
  if (!iso) return "";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "";
  const s = (Date.now() - dt.getTime()) / 1000;
  if (s < 60) return t("time.now");
  if (s < 3600) return t("time.minutes", { n: String(Math.floor(s / 60)) });
  if (s < 86400) return t("time.hours", { n: String(Math.floor(s / 3600)) });
  if (s < 604800) return t("time.days", { n: String(Math.floor(s / 86400)) });
  return dt.toLocaleDateString();
}

function parseTs(iso: string | null | undefined): number {
  if (!iso) return 0;
  const n = Date.parse(iso);
  return Number.isNaN(n) ? 0 : n;
}

// First human turn of a session — the rail label for a not-yet-titled new chat.
function firstUserText(s: Session): string {
  const u = s.messages.find((m) => m.role === "user");
  return u?.body || "";
}

// ── row provenance ──────────────────────────────────────────────────────────
// A rail of 100+ rows is only scannable if you can tell at a glance what each
// row IS. The rail is narrow (260px by default) and user-resizable, so the
// budget is tight and the title has first claim on line 1 — it's the thing
// being read. Only automated rows earn a pill there: they're a different KIND
// of row, and their titles (a task name) are short anyway. Platform chats put
// their provenance on line 2 instead, where it costs the title nothing.

// Non-interactive origins only — ORIGIN_ICONS / OriginPill live in channels.tsx
// now, shared with the simple shell's rail so the two can never disagree.

/** "Carlos" — who the conversation is with.
 *
 * The channel used to lead this line ("Telegram · Carlos"), but it is a pill on
 * the title row now, and printing it twice is what made a rail of platform
 * threads read as a wall of truncated "Dashb…". Line 2 carries only what the
 * pill cannot: the person. */
function sourceLabel(row: Row): string {
  return row.sender || "";
}

/**
 * "now · 12 msgs · 46 credits · $0.870" — the row's counters, with one money
 * segment per billing book the thread actually touched.
 *
 * The two are never summed and never converted: `credits` is the Olano
 * gateway's own charge for turns on Olano models, `usd` the customer's provider
 * bill for turns on their own keys (a managed call prices to $0 in the mining
 * that produces it, so the two are disjoint by construction). A thread holds
 * both when the model picker changed the book mid-conversation. Built as one
 * string so the same text can go in the row's `title` — the rail is resizable,
 * and a clipped price must stay recoverable.
 */
function countsLabel(row: Row, timeLabel: string, t: T): string {
  const parts = [timeLabel, `${row.messageCount} ${t(row.messageCount === 1 ? "thread.msg" : "thread.msgs")}`];
  if ((row.credits ?? 0) > 0) parts.push(fmtCreditsAmount(row.credits ?? 0));
  if ((row.usd ?? 0) > 0) parts.push(fmtUsd(row.usd ?? 0));
  return parts.join(" · ");
}

// ── composer auto-grow ──────────────────────────────────────────────────────
// A plain <textarea> keeps a fixed height and scrolls internally; to mirror the
// Claude Code composer we grow it with its content up to COMPOSER_MAX_ROWS rows,
// then let it scroll. There's no broadly-supported pure-CSS equivalent, so the
// height + overflow are driven imperatively off scrollHeight.
const COMPOSER_MAX_ROWS = 10;

// Auto-scroll counts the user as "pinned to the bottom" within this distance —
// generous enough that sub-pixel rounding and momentum-scroll overshoot near
// the bottom don't release the pin.
const PIN_THRESHOLD_PX = 80;

function autosizeComposer(el: HTMLTextAreaElement) {
  const cs = getComputedStyle(el);
  const line = parseFloat(cs.lineHeight) || 20;
  const padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
  const max = line * COMPOSER_MAX_ROWS + padY;
  el.style.height = "auto"; // reset first so the box can also shrink as text is deleted
  const next = el.scrollHeight; // full content height (border-box: content + padding)
  el.style.height = `${Math.min(next, max)}px`;
  el.style.overflowY = next > max ? "auto" : "hidden";
}

// ── threads drawer width (px) ────────────────────────────────────────────
// Persisted to localStorage so the user's chosen width survives reloads
// (mirrors FilesView's tree pane). The drawer overlays the conversation rather
// than splitting it, but CHAT_W_MIN still applies: a panel that covers the whole
// window is a screen, not a drawer, and leaves nothing to dismiss it against.
const RAIL_W_KEY = "lc.chat.railWidth";
const RAIL_W_MIN = 200;
const RAIL_W_MAX = 520;
const RAIL_W_DEFAULT = 300;
const CHAT_W_MIN = 420;

/** Clamp to [MIN, MAX] and to whatever keeps the conversation pane usable. */
function clampRailWidth(px: number): number {
  const max = Math.max(RAIL_W_MIN, Math.min(RAIL_W_MAX, window.innerWidth - CHAT_W_MIN));
  return Math.round(Math.min(max, Math.max(RAIL_W_MIN, px)));
}

function readRailWidth(): number {
  try {
    const raw = localStorage.getItem(RAIL_W_KEY);
    if (raw) return clampRailWidth(parseFloat(raw));
  } catch {
    // storage disabled — fall through to the default.
  }
  return RAIL_W_DEFAULT;
}

function writeRailWidth(px: number): void {
  try {
    localStorage.setItem(RAIL_W_KEY, String(px));
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

// Draggable left edge of the threads drawer. Mirrors the FilesView tree/preview
// handle: brightens while hovered or dragging. It rides the drawer's *left* edge
// because the panel hangs off the right of the window — the grip has to be on
// the side the panel actually grows from, or dragging it reads backwards.
function ResizeHandle({
  active,
  title,
  onMouseDown,
  onDoubleClick,
}: {
  active: boolean;
  title: string;
  onMouseDown: (e: MouseEvent) => void;
  onDoubleClick: () => void;
}) {
  const [hover, setHover] = useState(false);
  const lit = active || hover;
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      title={title}
      onMouseDown={onMouseDown}
      onDoubleClick={onDoubleClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        position: "absolute",
        top: 0,
        bottom: 0,
        left: -3,
        width: 7,
        zIndex: 6,
        cursor: "col-resize",
        display: "flex",
        justifyContent: "center",
        touchAction: "none",
      }}
    >
      {/* Visual line over the border — brightens while hovered/dragging. */}
      <span
        style={{
          width: 2,
          alignSelf: "stretch",
          background: lit ? "var(--accent)" : "transparent",
          transition: "background var(--t-fast)",
        }}
      />
    </div>
  );
}

// Absolute stamp for a message older than a day: "dd/mm/yyyy hh:mm am|pm".
function formatAbsoluteTime(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, "0");
  let h = d.getHours();
  const ampm = h >= 12 ? "pm" : "am";
  h = h % 12 || 12;
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(h)}:${pad(d.getMinutes())} ${ampm}`;
}

// Human "time ago" for a message: "just now" under a minute, then "{n} min(s)
// ago" / "{n} hour(s) ago" within the day, then an absolute dd/mm/yyyy stamp
// once the message is over 24h old. `now` is threaded in from a periodic tick
// so the labels refresh without each message owning its own timer.
function formatMsgTime(ts: number, now: number, t: (k: string, v?: Record<string, string>) => string): string {
  const sec = Math.floor((now - ts) / 1000);
  if (sec < 60) return t("msg.time.now");
  const min = Math.floor(sec / 60);
  if (min < 60) return t(min === 1 ? "msg.time.min.one" : "msg.time.min.many", { n: String(min) });
  const hr = Math.floor(min / 60);
  if (hr < 24) return t(hr === 1 ? "msg.time.hour.one" : "msg.time.hour.many", { n: String(hr) });
  return formatAbsoluteTime(ts);
}

// The "time ago" label rendered beside a message's actions. Carries the full
// absolute time as a tooltip so the exact moment is always one hover away.
function MsgTime({ ts, now, t }: { ts: number; now: number; t: (k: string, v?: Record<string, string>) => string }) {
  return (
    <time className="msg-time" dateTime={new Date(ts).toISOString()} title={formatAbsoluteTime(ts)}>
      {formatMsgTime(ts, now, t)}
    </time>
  );
}

// A quiet "copy this reply" button rendered under every completed agent
// response. Mirrors the code-block copy affordance: it swaps to a check +
// "Copied" for a beat, then reverts. `onFail` surfaces a toast when the
// clipboard is unavailable (insecure origin, denied permission).
function CopyButton({
  text,
  label,
  copiedLabel,
  onFail,
}: {
  text: string;
  label: string;
  copiedLabel: string;
  onFail?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      onFail?.();
    }
  }
  return (
    <button
      className={`msg-copy${copied ? " copied" : ""}`}
      onClick={() => void copy()}
      type="button"
      title={label}
      aria-label={label}
    >
      {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
      {copied ? copiedLabel : label}
    </button>
  );
}

export function ChatView() {
  const toast = useToast();
  const confirm = useConfirm();
  const { user } = useAuth();
  const t = useT(STR);
  // The selected agent is the one the URL names (/chat/<agent>, see
  // core/currentAgent.tsx), so a reload or Back/Forward opens a thread under
  // the agent it belongs to from the very first render.
  const { agent, current, agents, reload } = useCurrentAgent();
  const navigate = useNavigate();
  // "Add a bot" — promote this 1:1 into a group chat. Only offered when it can
  // actually work: groups on, and a second bot to add.
  const groupsEnabled = useGroupsEnabled() === true;
  const [addBotOpen, setAddBotOpen] = useState(false);
  // Shared provider/model catalog (module-cached) — drives the header picker's
  // option list and lets us retire stale model overrides (below).
  const catalog = useCatalog();
  // Subscribed so the rail picks up the credit vocabulary once /api/credits
  // lands. The two per-thread figures are unit-fixed — credits verbatim,
  // dollars as dollars — so neither follows the deployment's display setting.
  useCostDisplay();

  // Subscribe to the app-level session store. The store owns every live
  // conversation (messages, streaming, usage, abort controllers) at module
  // scope, so streams keep running — and accumulating — while this view is
  // unmounted (navigating to another panel) or while the user is talking to a
  // different agent. `version` bumps on any store change to re-render us.
  const version = useSyncExternalStore(chatStore.subscribe, chatStore.getSnapshot);

  const [prefs, setPrefs] = useState<ChatPrefs>(() => getPrefs(agent));
  const [dragging, setDragging] = useState(false);
  const [shellBusy, setShellBusy] = useState(false);
  const [slashIndex, setSlashIndex] = useState(0);
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Inline thread rename: the thread id currently being edited + its draft name.
  // `editingRef` mirrors `editingId` for synchronous reads (see commitRename).
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const editingRef = useRef<string | null>(null);
  // This agent's skills + subagents/tools/MCP servers — surfaced as / commands
  // and the /skills /subagents /tools /mcps listings.
  const [skills, setSkills] = useState<{ name: string; description: string }[]>([]);
  const [subagents, setSubagents] = useState<{ name: string; description: string }[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [mcps, setMcps] = useState<{ url: string; transport: string; enabled: boolean }[]>([]);
  // Per-agent custom commands + which built-ins the owner exposes (Commands tab).
  const [customCmds, setCustomCmds] = useState<{ name: string; prompt: string; description?: string }[]>([]);
  const [cmdEnabled, setCmdEnabled] = useState<Record<string, boolean>>({});
  // Maps each built-in command alias → its canonical name, so the dashboard
  // chat can honor a `commands.enabled` deviation keyed by canonical name.
  // Static — fetched once.
  const cmdAlias = useRef<Record<string, string>>({});
  // name → description map from the global tool catalog, used to enrich the
  // agent's resolved tool names in the /tools listing.
  const toolCatalog = useRef<Map<string, string>>(new Map());
  const dragDepth = useRef(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // Mirrors the textarea's text so the live dictation tail can be drawn gray —
  // a plain textarea can't color part of its own value.
  const mirrorRef = useRef<HTMLDivElement>(null);
  const prevAgentRef = useRef(agent);

  // Touch devices type through an on-screen keyboard: there Return inserts a
  // newline (like every mobile messenger) and only the send button submits.
  // Desktop keeps Enter-to-send / Shift+Enter-for-newline.
  const coarsePointer = useMediaQuery("(pointer: coarse)");

  // Shell-style input history: ArrowUp/ArrowDown in the composer recall the
  // user's previously sent messages. `histIdx` indexes into `userHistory`
  // (-1 = the live draft, not navigating); `histDraftRef` stashes the draft so
  // returning to the bottom restores whatever was being typed.
  const [histIdx, setHistIdx] = useState(-1);
  const histDraftRef = useRef("");

  // Inline edit of a past user message: `editingMsgId` is the id of the message
  // being edited (null = none), `msgEditDraft` holds its working text. Saving
  // resends the edited text as a NEW turn (the model keeps prior context via the
  // thread checkpoint — same no-history-surgery approach as retryLast).
  const [editingMsgId, setEditingMsgId] = useState<number | null>(null);
  const [msgEditDraft, setMsgEditDraft] = useState("");
  const msgEditRef = useRef<HTMLTextAreaElement>(null);

  // A coarse clock that ticks once a minute so every message's "time ago" label
  // ages in place ("just now" → "1 min ago" → …) without each label wiring up
  // its own timer. Minute granularity is plenty for the labels we render.
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  // Draggable width of the threads drawer. During a drag we write the width
  // custom property straight onto the DOM node (via railRef) so the message log
  // doesn't re-render on every mousemove; we only commit to React state +
  // localStorage on mouse-up. (Mirrors FilesView.) A custom property rather than
  // `style.width` because an inline width would outrank the mobile cap below the
  // breakpoint, and a window dragged narrow after a resize would keep it.
  const [railWidth, setRailWidth] = useState<number>(readRailWidth);
  const [resizing, setResizing] = useState(false);

  // The conversation history is an off-canvas drawer on the RIGHT, at every
  // viewport, closed on every visit. Two things were wrong with the column it
  // replaces. It spent 260px of every desktop window — permanently — on a list
  // the reader consults for a second and is then done with, and on a phone it
  // spent ~220px of ~700px of chrome-free height on the same list, letterboxing
  // the messages and pushing the composer toward the fold. Now the conversation
  // owns the whole width at all times and the list is one click away behind the
  // header's history button, arriving from the edge it is anchored to. Transient
  // by design — it opens closed and shuts as soon as a thread is picked — so
  // there is no persisted "open" state that could strand a backdrop over the
  // conversation on load.
  // Open with the thread rail showing when the sidebar's "See all chats" sent
  // us here (`/chat?history=1`). Read once, off the raw URL: this state is
  // declared before the search-params hook below, and a one-shot flag has no
  // business surviving a reload — the effect further down strips it.
  const [historyOpen, setHistoryOpen] = useState(
    () => new URLSearchParams(window.location.search).get("history") === "1",
  );
  const railRef = useRef<HTMLElement>(null);
  // The agent's profile card — the simple shell's "About this agent" panel,
  // the same component — as a second drawer in the same slot as the list.
  // One at a time: opening either closes the other, so the two can never
  // stack on the same edge. Transient like the list, for the same reason:
  // nothing persisted, so no backdrop can be stranded over the chat on load.
  const [profileOpen, setProfileOpen] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);
  const closeHistory = useCallback(() => setHistoryOpen(false), []);
  const closeProfile = useCallback(() => setProfileOpen(false), []);
  const toggleRail = useCallback(() => {
    setProfileOpen(false);
    setHistoryOpen((o) => !o);
  }, []);
  const toggleProfile = useCallback(() => {
    setHistoryOpen(false);
    setProfileOpen((o) => !o);
  }, []);
  const railVisible = historyOpen;

  const startResize = useCallback(
    (e: MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = railWidth;
      let latest = startW;
      setResizing(true);
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";
      const onMove = (ev: globalThis.MouseEvent) => {
        // Inverted against the left-hand rail this replaces: the panel is pinned
        // to the right edge, so dragging its left edge *leftwards* widens it.
        latest = clampRailWidth(startW - (ev.clientX - startX));
        railRef.current?.style.setProperty("--chat-rail-w", `${latest}px`);
      };
      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        setResizing(false);
        setRailWidth(latest);
        writeRailWidth(latest);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [railWidth],
  );

  const resetResize = useCallback(() => {
    const w = clampRailWidth(RAIL_W_DEFAULT);
    setRailWidth(w);
    writeRailWidth(w);
  }, []);

  // Keep the rail within bounds when the window shrinks (the conversation must
  // stay usable). Storage isn't touched, so the saved preference is preserved.
  useEffect(() => {
    const onResize = () => setRailWidth((w) => clampRailWidth(w));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // The active conversation for the current agent (its key drives the composer).
  const activeKey = chatStore.activeKeyOf(agent);
  const session = chatStore.activeSession(agent);
  const messages = session?.messages ?? EMPTY_MSGS;
  // Re-shows a browser dock the person hid once the agent browses again.
  // Counted each render, not memoized: the store grows a live turn's tool
  // list in place, so the array identity does not move with it.
  const browserCalls = countBrowsingCalls(messages);

  // Bring the newest unanswered approval card into view (the browser dock asks
  // for this when an approval arrives while the person is watching the
  // browser instead of the conversation).
  const showPendingApproval = useCallback(() => {
    const cards = document.querySelectorAll<HTMLElement>(".approval-card:not(.is-resolved)");
    const card = cards[cards.length - 1];
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.querySelector<HTMLButtonElement>(".btn.primary")?.focus({ preventScroll: true });
  }, []);

  // ── landing intent: ?thread= and the Cortex hand-off ──────────────────────
  // A copied /chat?thread=<id> link (or a plain refresh) reopens that thread;
  // previously the selection lived only in module state and a reload lost it.
  //
  // Cortex arrives the same way — it navigates to /chat?thread=pulse:<id> — but
  // stages a draft alongside it (see handoff.ts), because the thread it names
  // may not exist yet: a new discussion opens with the composer pre-filled
  // (never sent), an existing one simply reopens on its history.
  const [searchParams, setSearchParams] = useSearchParams();
  // The `?thread=` value most recently acted on. Landing is keyed on the URL
  // CHANGING rather than on first mount: the view can already be on screen
  // when a hand-off navigates here (a second hand-off, or a shell that keeps
  // the chat mounted), and a mount-time one-shot would then do nothing at all —
  // which is exactly how the Cortex buttons read as broken.
  const seenUrlThreadRef = useRef<string | null>(null);
  // `history=1` has done its job once the rail is open (see historyOpen) —
  // a copied link must not reopen the rail on every visit.
  useEffect(() => {
    if (!searchParams.has("history")) return;
    // The initializer above only sees the URL the view MOUNTED with; the
    // sidebar's "See all chats" / "Show all N chats" can navigate here while
    // the view is already on screen, and the drawer must open then too.
    setProfileOpen(false);
    setHistoryOpen(true);
    const next = new URLSearchParams(searchParams);
    next.delete("history");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  // ── OS share sheet → the composer ────────────────────────────────────────
  // An installed PWA can be a share target (manifest.webmanifest), so "share
  // to Olano" from another app navigates here with the payload in the query
  // string. It becomes a draft and waits — never sent on the user's behalf,
  // exactly like the Cortex hand-off below.
  //
  // Held until a session exists: a cold launch arrives here before the store
  // has opened a conversation, so there is no composer to fill yet. `session`
  // is a dependency, so the draft lands on the render that produces one, and
  // the params stay in the URL until then rather than being consumed by a pass
  // that could not act on them.
  const sharedRef = useRef(false);
  useEffect(() => {
    if (sharedRef.current) return;
    const draft = composeSharedDraft(readSharedPayload(searchParams));
    if (!draft || !session) return;
    sharedRef.current = true;
    // Only into an empty composer. `shouldPreloadDraft` is deliberately not
    // reused here — it also demands an empty transcript, and sharing a link
    // into a conversation already under way is a normal thing to do. The rule
    // that matters is never overwriting something half-typed.
    if (!session.input.trim()) {
      chatStore.setInput(agent, session.key, draft);
      focusComposerEnd(taRef.current);
    }
    // Strip the payload so a refresh or a back-button cannot re-draft it over
    // whatever the person has typed since.
    const stripped = stripShareParams(searchParams);
    if (stripped) setSearchParams(stripped, { replace: true });
  }, [agent, session, searchParams, setSearchParams]);
  // The staged hand-off, claimed from an effect rather than during render:
  // StrictMode double-invokes a lazy `useState` initializer and keeps only one
  // of the two results, which would drop the draft in development. Cleared
  // once acted on, so it is consumed exactly once whatever happens after.
  const handoffRef = useRef<ChatHandoff | null>(null);
  const landedRef = useRef(false);
  // The thread a landing is CURRENTLY opening, or null. The reflect effect
  // below must not write the URL while this is set: `landedRef` flips the
  // moment a landing starts, and the open is async, so on the very commit
  // that starts it there is still no session — reflecting "no thread" there
  // deletes the `?thread=` the landing is in the middle of honouring. That is
  // not hypothetical: it dropped the onboarding wizard's own thread on a slow
  // machine, landing the owner on a bare /chat after setup.
  const landingRef = useRef<string | null>(null);
  const reflectedRef = useRef<string | null>(null);
  // The onboarding hand-off: the runtime is writing this thread's first rows
  // itself (the greeting), and Agent Father may still be booting before the
  // first one lands — a minute on a cold box. Drawn as "waking up" instead of
  // the empty-thread prompt until a message arrives; the timer is a backstop
  // sized to the greeting's own wait budget (onboarding_greeting.py) plus a
  // turn, so a greeting that never comes still ends in the normal composer.
  const [waking, setWaking] = useState(false);
  useEffect(() => {
    if (messages.length > 0) setWaking(false);
  }, [messages.length]);
  useEffect(() => {
    if (!waking) return;
    const timer = setTimeout(() => setWaking(false), 6 * 60_000);
    return () => clearTimeout(timer);
  }, [waking]);

  // The last thread id this browser reflected INTO the URL (null once a New
  // chat cleared it). It separates "no conversation open yet" from
  // "the conversation was closed" — see the reflect effect below.
  useEffect(() => {
    const staged = takeChatHandoff();
    if (staged) handoffRef.current = staged;
    const id = searchParams.get("thread");
    if (!agent || !id) return;
    // The URL is the authority on WHICH thread; the hand-off only adds the
    // draft, and only for the thread it was staged for. A stale hand-off
    // (navigation never happened) can therefore never hijack a later plain
    // /chat visit.
    const handoff = handoffRef.current?.threadId === id ? handoffRef.current : null;
    // The reflect effect below writes the open thread into the URL, so a value
    // already acted on comes back here once per switch; that echo is not a
    // landing. Back/Forward to a thread this view showed before IS one, though
    // (the URL names it, the store has moved on), so "seen" only skips while
    // that thread is still the one open.
    if (!handoff && id === seenUrlThreadRef.current && chatStore.activeSession(agent)?.threadId === id) return;
    // A hand-off names its own agent (a Pulse card can belong to one the picker
    // isn't on). The caller switches the global selection before navigating, so
    // wait a render for that to arrive rather than opening the thread under the
    // previous agent — thread ids are per-agent and it would 404.
    if (handoff && agent !== handoff.agent) return;
    seenUrlThreadRef.current = id;
    landedRef.current = true;
    landingRef.current = id;
    handoffRef.current = null;
    if (handoff?.awaiting === "greeting") setWaking(true);
    void (async () => {
      try {
        if (chatStore.activeSession(agent)?.threadId !== id) {
          await chatStore.openThread(agent, id, { allowMissing: !!handoff });
        }
        // Pre-fill only a genuinely new thread with an empty composer: reopening
        // an existing Cortex discussion must continue it on its history, and a
        // message the person is mid-way through typing is never overwritten.
        // Never sent — the draft ends in "User: " and waits for their words.
        const opened = chatStore.activeSession(agent);
        if (handoff?.draft && opened && opened.threadId === id && shouldPreloadDraft(opened)) {
          chatStore.setInput(agent, opened.key, handoff.draft);
          focusComposerEnd(taRef.current);
        }
      } finally {
        // Released even if the open threw: a landing that failed still must not
        // leave the URL frozen for the rest of the visit.
        if (landingRef.current === id) landingRef.current = null;
      }
    })();
  }, [agent, searchParams]);
  // Reflect the open conversation back into the URL (replace, no history spam).
  // Held back until the landing intent above has been acted on: on the first
  // render no thread is open yet, so this would strip the `?thread=` it is
  // about to restore — a link that visibly loses its own parameter and back to
  // /chat for anyone watching the address bar.
  useEffect(() => {
    if (!landedRef.current && searchParams.get("thread")) return;
    if (landingRef.current) return; // a landing owns the URL until it settles
    const id = session?.threadId ?? null;
    if ((searchParams.get("thread") ?? null) === id) return;
    // Deleting is only ever right for a conversation this effect itself put in
    // the URL and that has since gone away (New chat). Before the first
    // reflection there is no such conversation — a null id there means the
    // open has not produced a session YET, and removing the parameter would
    // throw away the landing's own instruction. Reading "no thread" as "clear
    // the URL" is what dropped a hand-off's thread on a slow machine, and
    // whether it happened at all came down to which effect won the commit.
    if (id === null && reflectedRef.current === null) return;
    reflectedRef.current = id;
    const next = new URLSearchParams(searchParams);
    if (id) next.set("thread", id);
    else next.delete("thread");
    // Moving from one conversation to another (a row picked, New chat) is a
    // Back step. Filling in a URL that named no thread yet (the arrival pick,
    // a draft's first message getting its id) is not.
    const move = !!searchParams.get("thread");
    setSearchParams(next, { replace: !move });
  }, [session?.threadId, searchParams, setSearchParams]);
  // The user's own turns in this conversation, oldest→newest — the corpus the
  // ArrowUp/ArrowDown history recall cycles through (attachment-only turns with
  // no text are skipped).
  //
  // The store mutates `session.messages` IN PLACE (`.push()` in send()), so the
  // array reference is stable across a conversation — keying the memo on it alone
  // would never recompute after the first render, leaving a just-sent message
  // unreachable until a page reload reassigned the array. Key on the active
  // conversation + message count instead so a freshly sent turn is recalled
  // immediately.
  const userHistory = useMemo(
    () => messages.filter((m) => m.role === "user" && m.body).map((m) => m.body),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeKey, messages.length],
  );
  const streaming = session?.streaming ?? false;
  // A turn is live whether this tab is streaming it or rejoining one that
  // outlived its socket (olano/core/runs.py). The composer must treat the two
  // alike: while reconnecting, Stop has to stay reachable — that is the moment
  // someone most wants it — and Enter must queue or steer rather than start a
  // second turn the server would refuse. Only the composer needs this; the
  // transcript still distinguishes them (`attaching` draws "Reconnecting").
  const turnLive = streaming || (session?.attaching ?? false);
  const usage = session?.usage ?? null;
  const streamModel = session?.streamModel ?? "";
  const input = session?.input ?? "";
  const staged = session?.staged ?? EMPTY_ATT;
  // Messages the user typed while this turn was streaming (drained FIFO when it
  // finishes). Drives the queued-chips strip above the composer.
  const queued: QueuedMsg[] = session?.queue ?? EMPTY_QUEUE;
  // Shell "!" mode: composer flips when the draft starts with "!" AND the user
  // may use the terminal (admin / granted). The queue counter drives the
  // "results pending" chip.
  const canShell = useCapability("terminal_enabled") === true;
  const shellMode = canShell && input.trimStart().startsWith("!");
  const shellQueued = session?.shellBuffer.length ?? 0;

  // ── voice input (mic) ──────────────────────────────────────────────────────
  // The draft lives in the store, so dictation callbacks read it through a ref
  // rather than a closure that would go stale between utterances.
  const inputRef = useRef(input);
  inputRef.current = input;
  const voiceErrorMsg = useCallback(
    (code: VoiceErrorCode): string =>
      code === "denied"
        ? t("voice.denied")
        : code === "no-speech"
          ? t("voice.noSpeech")
          : code === "network"
            ? t("voice.network")
            : code === "stt-off"
              ? t("voice.sttOff")
              : t("voice.error"),
    [t],
  );
  const onVoiceCommit = useCallback(
    (text: string) => {
      const prev = inputRef.current;
      // Dictation always appends to the end of the draft; typing still edits
      // anywhere. Keeping one rule avoids fighting the caret mid-utterance.
      const next = prev ? `${prev.replace(/\s+$/, "")} ${text}` : text;
      const s = chatStore.ensureActive(agent);
      chatStore.setInput(agent, s.key, next);
    },
    [agent],
  );
  const onVoiceError = useCallback(
    (code: VoiceErrorCode) => {
      toast.push(voiceErrorMsg(code), code === "no-speech" ? "info" : "error");
    },
    [toast, voiceErrorMsg],
  );
  const [interim, setInterim] = useState("");
  const { lang } = useI18n();
  const sttEnabled = useCapability("stt_enabled");
  const voice = useVoiceInput({
    sttEnabled,
    lang,
    onInterim: setInterim,
    onCommit: onVoiceCommit,
    onError: onVoiceError,
  });
  const voiceRecording = voice.recording;
  // Press-and-hold vs tap-to-toggle is decided on release (see the mic button).
  const holdStartRef = useRef(0);
  // Whether the current press started a recording (vs. ended a toggled one),
  // so the matching release knows if it has anything to decide.
  const pressStartedRef = useRef(false);
  // Dictating into a shell command is a footgun, and the mic has no business
  // hijacking Cmd+D while the composer is in "!" mode.
  const voiceUsable = voice.support !== "none" && !shellMode;

  // The agent's thread list, from the store every list of its chats shares
  // (the sidebar's per-agent list, the simple rail): fetched once per agent,
  // kept live on the store's own SSE + turn-end triggers, refetched here only
  // after an action this view took. A failed refetch keeps the rows we have
  // (live sessions still render); blanking silently made a fetch error look
  // like "no conversations yet".
  const onThreadsFailed = useCallback(() => toast.push(t("toast.threadsFailed"), "error"), [toast, t]);
  const { threads, refresh: loadThreads } = useThreadList(agent, onThreadsFailed);

  // Per-agent composer prefs follow the agent.
  useEffect(() => {
    setPrefs(getPrefs(agent));
  }, [agent]);

  // A reopened thread continues on the model it last answered with (seeded by
  // chatStore.openThread). Once the catalog is known, a restored model this
  // box no longer offers is dropped back to the agent's default — sending it
  // would fail every turn — and the conversation says so, once.
  const sessionModel = session?.model ?? "";
  useEffect(() => {
    if (!agent || !session || !sessionModel) return;
    const opts = modelOptions(catalog.providers);
    if (opts.length === 0) return; // catalog not loaded yet — don't clear prematurely
    const usable = restoredModelUsable({
      model: sessionModel,
      agentModel: current?.model,
      catalogValues: opts.map((o) => o.value),
    });
    if (usable) return;
    chatStore.setModel(agent, session.key, "");
    chatStore.pushSystem(agent, session.key, t("model.unavailable", { model: sessionModel }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent, session?.key, sessionModel, catalog.providers, current?.model]);

  // ── arrival: which conversation to show ───────────────────────────────────
  // The first time this tab looks at an agent (and after every reload) there
  // is no active session for it. Rather than greet the reader with an empty
  // composer, open the conversation they most likely came back for — the
  // newest one a person is in, dashboard or messenger, else the newest of any
  // kind (threadPick.ts) — and only fall back to a draft when the agent has no
  // threads at all. A draft is local until its first message, so an agent with
  // no history can be opened any number of times without an empty thread ever
  // reaching the server. A `?thread=` landing (a copied link, a Cortex
  // hand-off) owns the choice instead and is left alone; so is an agent whose
  // session already exists — navigating away and back, or "New chat", keeps
  // the conversation exactly where the reader left it.
  const [arriving, setArriving] = useState(false);
  useEffect(() => {
    if (!agent) return;
    if (chatStore.activeSession(agent)) return;
    if (searchParams.get("thread")) return;
    let cancelled = false;
    setArriving(true);
    // From the newest page when it can answer, not the whole list: that read
    // costs every transcript the agent has, and on a long-lived agent it was
    // most of the wait before the first open showed anything.
    void threadListStore
      .arrivalThread(agent)
      .catch(() => null)
      .then((pick) => {
        if (cancelled) return;
        // Typing before the list arrived already made a draft: keep it.
        if (chatStore.activeSession(agent)) return;
        if (pick) void chatStore.openThread(agent, pick.id);
        else chatStore.newChat(agent);
      })
      .finally(() => {
        if (!cancelled) setArriving(false);
      });
    return () => {
      cancelled = true;
      setArriving(false);
    };
  }, [agent, searchParams]);

  // Local rail UI (multi-select) is per-agent — drop it on a real agent switch
  // so a selection made for agent A can't bulk-delete agent B's threads.
  useEffect(() => {
    if (prevAgentRef.current === agent) return;
    prevAgentRef.current = agent;
    setSelectMode(false);
    setSelected(new Set());
  }, [agent]);

  // Viewing lifecycle: clear the active conversation's unread badge while it's
  // on screen; on leaving (unmount or switching conversation) stop counting it
  // as read so replies that land while away are badged.
  useEffect(() => {
    if (agent && activeKey) chatStore.markRead(agent, activeKey);
    return () => chatStore.clearViewing();
  }, [agent, activeKey]);

  // Switching conversation (or agent) drops any in-progress history recall so
  // the new composer starts on its own live draft.
  useEffect(() => {
    setHistIdx(-1);
  }, [agent, activeKey]);

  // Surface background stream / approval / load errors as toasts.
  useEffect(
    () =>
      chatStore.subscribeError((code) => {
        const key = code === "approval" ? "approval.failed" : code === "open" ? "toast.openFailed" : "toast.chatFailed";
        toast.push(t(key), "error");
      }),
    [toast, t],
  );

  // ── pin-aware auto-scroll ────────────────────────────────────────────────
  // Follow new content only while the user sits at (or near) the bottom of the
  // log. Scrolling up to re-read releases the pin — streaming continues above
  // without yanking the viewport back — and a floating "jump to latest" pill
  // offers the way back. Scrolls are instant: re-starting a smooth animation on
  // every streamed frame is visible jank.
  const pinnedRef = useRef(true);
  const [pinned, setPinned] = useState(true);
  const updatePinned = useCallback(() => {
    const el = logRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < PIN_THRESHOLD_PX;
    pinnedRef.current = atBottom;
    setPinned((prev) => (prev === atBottom ? prev : atBottom));
  }, []);
  const scrollToLatest = useCallback((behavior: ScrollBehavior = "auto") => {
    const el = logRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
    pinnedRef.current = true;
    setPinned(true);
  }, []);

  // Switching conversation (or agent) always lands pinned at the newest message.
  useEffect(() => {
    scrollToLatest();
  }, [activeKey, scrollToLatest]);

  // New content in the active conversation: follow only while pinned. Keyed on
  // the per-session `rev` counter, which bumps on every store change.
  useEffect(() => {
    if (pinnedRef.current) scrollToLatest();
  }, [session?.rev, scrollToLatest]);

  // Grow the composer with its content (up to COMPOSER_MAX_ROWS rows, then it
  // scrolls). Runs on every draft change — typing, send/clear, thread switch,
  // history recall. The threads drawer overlays the conversation rather than
  // splitting it, so opening or resizing it never re-wraps this text.
  useLayoutEffect(() => {
    const ta = taRef.current;
    if (ta) autosizeComposer(ta);
    // The gray interim tail is part of the textarea's value while dictating, so
    // the box has to grow with it — and the mirror overlay has to scroll with
    // the textarea or the two drift apart past COMPOSER_MAX_ROWS.
    if (ta && mirrorRef.current) mirrorRef.current.scrollTop = ta.scrollTop;
  }, [input, interim]);

  // Grow the inline message editor with its content, same as the composer.
  useLayoutEffect(() => {
    const ta = msgEditRef.current;
    if (editingMsgId !== null && ta) autosizeComposer(ta);
  }, [editingMsgId, msgEditDraft]);

  // Switching thread/agent abandons any open message edit — the edited message
  // belongs to the thread you left, so carrying the editor over would be wrong.
  useEffect(() => {
    setEditingMsgId(null);
    setMsgEditDraft("");
  }, [agent, activeKey]);

  // Re-flow the composer height when the viewport width changes — wrapping, and
  // thus the row count, depends on the composer's width.
  useEffect(() => {
    const onResize = () => {
      const ta = taRef.current;
      if (ta) autosizeComposer(ta);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Cmd+D (⌘D) / Ctrl+D toggles dictation, the way it does in Claude Code.
  // preventDefault is what keeps the browser from opening its bookmark dialog.
  // The handler reads `voice` through a ref: this view re-renders on every
  // streamed token, and depending on the hook's object identity would tear the
  // window listener down and rebuild it that many times.
  const voiceToggleRef = useRef(voice.toggle);
  voiceToggleRef.current = voice.toggle;
  useEffect(() => {
    if (!voiceUsable) return;
    function onKey(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
      if (e.key.toLowerCase() !== "d" || e.repeat) return;
      // A modal owns the keyboard while it's up, and typing in some other
      // field shouldn't start dictating into the composer behind it.
      if (document.querySelector('[role="dialog"], dialog[open]')) return;
      const el = document.activeElement;
      if (el && el !== taRef.current && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      e.preventDefault();
      voiceToggleRef.current();
      taRef.current?.focus();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [voiceUsable]);

  // Leaving the conversation must release the mic — the draft it was dictating
  // into belongs to the thread being left.
  useEffect(() => {
    voice.cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent, activeKey]);

  // The mic and its "Listening…" bar are hidden in shell ("!") mode, so if the
  // draft turns into a command mid-utterance the engine would keep listening
  // with nothing on screen saying so. Never leave a hot mic unindicated.
  useEffect(() => {
    if (!voiceUsable && voice.recording) voice.cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voiceUsable, voice.recording]);

  // Skills for the current agent — fetched for the / menu and /skills. Scoped to
  // `agent` so it resolves the agent's own workspace (incl. per-agent learned
  // skills); without the param the backend falls back to the global default
  // workspace and the listing comes back empty.
  const loadSkills = useCallback(async () => {
    const path = agent ? `/api/catalog/skills?agent=${encodeURIComponent(agent)}` : "/api/catalog/skills";
    // cachedGet: this is reference data for the `/` menu, nothing the user can
    // see until they type a slash, but the server pays a full workspace scan
    // for it on every mount (and every agent switch). /reload-skills busts the
    // entry explicitly, so the manual refresh is still authoritative.
    const r = await api
      .cachedGet<{ skills?: { name: string; description?: string }[] }>(path, CATALOG_TTL_MS)
      .catch(() => ({ skills: [] }) as { skills?: { name: string; description?: string }[] });
    const list = (r.skills || []).map((s) => ({ name: s.name, description: s.description || "" }));
    setSkills(list);
    return list.length;
  }, [agent]);
  useEffect(() => {
    void loadSkills();
  }, [loadSkills]);

  // Global tool catalog → name/description map, used to enrich the agent's
  // configured tool names in the /tools listing. Fetched once.
  const loadToolCatalog = useCallback(async () => {
    // cachedGet: the first /api/catalog/tools of a runtime's life imports every
    // registered tool module (~2.5s, on the event loop), and chat asked for it
    // on every mount purely to label tools in the `/tools` listing.
    const r = await api
      .cachedGet<{ tools?: { name: string; description?: string }[] }>(
        "/api/catalog/tools",
        CATALOG_TTL_MS,
      )
      .catch(() => ({ tools: [] }) as { tools?: { name: string; description?: string }[] });
    toolCatalog.current = new Map((r.tools || []).map((tl) => [tl.name, tl.description || ""]));
  }, []);
  useEffect(() => {
    void loadToolCatalog();
  }, [loadToolCatalog]);

  // This agent's subagents, tools and MCP servers — from its config — for the
  // /subagents, /tools and /mcps listings (and context).
  const loadAgentMeta = useCallback(async () => {
    if (!agent) {
      setSubagents([]);
      setTools([]);
      setMcps([]);
      return { subagents: 0, tools: 0, mcps: 0 };
    }
    const cfg = await api
      .get<{
        subagents_resolved?: { name?: string; ref?: string; description?: string }[];
        subagents?: { name?: string; ref?: string; description?: string }[];
        tools?: string[];
        tools_resolved?: string[];
        mcp_servers?: { url?: string; transport?: string; enabled?: boolean }[];
        commands?: { enabled?: Record<string, boolean>; custom?: { name: string; prompt: string; description?: string }[] };
      }>(`/api/agents/${encodeURIComponent(agent)}`)
      .catch(() => ({}) as Record<string, never>);
    setCustomCmds((cfg.commands?.custom || []).filter((c) => c?.name && c?.prompt));
    setCmdEnabled(cfg.commands?.enabled || {});
    const subList = (cfg.subagents_resolved || cfg.subagents || [])
      .map((s) => ({ name: s.name || s.ref || "", description: s.description || "" }))
      .filter((s) => s.name);
    // Prefer the resolved toolset (universal + ops defaults + deepagents
    // built-ins) the agent actually runs with — `tools` only holds extras and is
    // empty for most agents, which made /tools wrongly report "No tools configured".
    const toolList = (cfg.tools_resolved || cfg.tools || []).filter(Boolean);
    const mcpList = (cfg.mcp_servers || [])
      .map((m) => ({ url: m.url || "", transport: m.transport || "sse", enabled: m.enabled !== false }))
      .filter((m) => m.url);
    setSubagents(subList);
    setTools(toolList);
    setMcps(mcpList);
    return { subagents: subList.length, tools: toolList.length, mcps: mcpList.length };
  }, [agent]);
  useEffect(() => {
    void loadAgentMeta();
  }, [loadAgentMeta]);

  // Built-in command catalog — fetched once; only used to resolve aliases so a
  // command the owner *explicitly* disabled (Commands tab) is hidden from the
  // chat slash menu. The dashboard chat is the owner console: built-ins are
  // available by default (incl. sensitive ones like /reload-vault) — only an
  // explicit disable hides them. (Messaging platforms keep sensitive off.)
  useEffect(() => {
    let alive = true;
    api
      .cachedGet<CommandCatalogResponse>("/api/catalog/commands", CATALOG_TTL_MS)
      .then((cat) => {
        if (!alive) return;
        const aliasToCanonical: Record<string, string> = {};
        for (const c of cat.commands) {
          aliasToCanonical[c.name] = c.name;
          for (const a of c.aliases) aliasToCanonical[a] = c.name;
        }
        cmdAlias.current = aliasToCanonical;
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  // Whether a dashboard slash command is shown. The dashboard chat exposes its
  // built-ins by default; only a command the owner explicitly turned off in the
  // Commands tab (`enabled[name] === false`) is hidden.
  const cmdExposed = useCallback(
    (name: string): boolean => {
      const canonical = cmdAlias.current[name.toLowerCase()] ?? name.toLowerCase();
      return cmdEnabled[canonical] ?? true;
    },
    [cmdEnabled],
  );

  // Display-only: slash-command titles, toasts, the header. Anything that
  // ADDRESSES the agent (API paths, thread keys) must use current?.name.
  const agentName = agentLabel(current) || t("agent.fallback");
  // Admin-only "Open in Langfuse" targets for this agent (null otherwise).
  const langfuseLinks = useLangfuseLinks(current?.name);
  // This conversation's model: its own pick, else the agent's configured one.
  const model = effectiveChatModel({ sessionModel, agentModel: current?.model, streamModel });

  // The open conversation's own surface. Drives the header badge, and is the
  // baseline a per-message badge is measured against: a turn is badged only
  // when it arrived somewhere OTHER than here, so a 200-message Telegram thread
  // stays clean and the two turns typed from the dashboard stand out.
  const threadChannel = useMemo(
    () => threads.find((th) => th.id === session?.threadId)?.channel || "",
    [threads, session?.threadId],
  );

  // Merge backend threads with live sessions: streaming dots, unread badges, and
  // sorted by most-recent activity (newest on top). Recomputed on store changes.
  const rows = useMemo<Row[]>(() => {
    const live = chatStore.sessionsFor(agent);
    const byThread = new Map<string, Session>();
    for (const s of live) if (s.threadId) byThread.set(s.threadId, s);
    const threadIds = new Set(threads.map((th) => th.id));
    const threadRows: Row[] = threads.map((th) => {
      const s = byThread.get(th.id);
      const rowKey = s?.key ?? th.id;
      return {
        key: rowKey,
        threadId: th.id,
        name: th.name,
        preview: th.preview,
        title: th.title || "",
        origin: th.origin || "",
        channel: th.channel || "",
        sender: th.sender || "",
        messageCount: th.message_count,
        usd: th.usd ?? null,
        credits: th.credits ?? null,
        updatedAt: th.updated_at ?? null,
        sortTs: Math.max(parseTs(th.updated_at), s?.lastActivity ?? 0),
        // Working, whichever surface the turn is running on.
        streaming: !!s?.streaming || !!s?.activity,
        // The conversation on screen is, by definition, read — don't flash a
        // stale badge for the one frame before the markRead effect runs.
        unread: rowKey === activeKey ? 0 : s?.unread ?? 0,
      };
    });
    // Live sessions not (yet) in the backend list: a brand-new chat streaming its
    // first turn before it has a thread id, or one whose freshly-created thread
    // hasn't surfaced in loadThreads yet. System-only drafts (e.g. /help output)
    // stay out of the rail.
    const extraRows: Row[] = live
      .filter(
        (s) =>
          (!s.threadId || !threadIds.has(s.threadId)) &&
          (s.streaming || s.messages.some((m) => m.role === "user" || m.role === "assistant")),
      )
      .map((s) => ({
        key: s.key,
        threadId: s.threadId,
        name: "",
        preview: firstUserText(s),
        // A live session has no server-derived label yet — it's a chat the user
        // is typing right now, so its own first turn is already the right title.
        title: "",
        origin: "",
        channel: "",
        sender: "",
        messageCount: s.messages.length,
        updatedAt: null,
        sortTs: s.lastActivity,
        streaming: s.streaming || !!s.activity,
        unread: s.key === activeKey ? 0 : s.unread,
      }));
    return [...extraRows, ...threadRows].sort((a, b) => b.sortTs - a.sortTs);
  }, [version, threads, agent, activeKey]);

  // Slash autocomplete state (derived from the composer text).
  const slashQuery = input.startsWith("/") && !input.includes(" ") && !input.includes("\n") ? input.slice(1).toLowerCase() : null;
  // Built-in commands (minus any the owner disabled) + custom commands + one
  // command per available skill (invoke it with a task).
  const allCommands: SlashCommand[] = [
    ...SLASH_COMMANDS.filter((c) => cmdExposed(c.name)),
    ...customCmds.map((c) => ({
      name: c.name,
      args: "[args]",
      desc: c.description || t("slash.customCommand"),
      group: "custom" as const,
    })),
    ...skills.map((s) => ({ name: s.name, args: "[task]", desc: s.description, group: "skill" as const })),
  ];
  const slashMatches = slashQuery !== null ? allCommands.filter((c) => c.name.toLowerCase().startsWith(slashQuery)) : [];
  const slashOpen = slashMatches.length > 0;

  // ── composer-bound store writes (always target the active session) ──────────
  function setInput(value: string) {
    const s = chatStore.ensureActive(agent);
    chatStore.setInput(agent, s.key, value);
  }
  function setStaged(next: Attachment[] | ((prev: Attachment[]) => Attachment[])) {
    const s = chatStore.ensureActive(agent);
    chatStore.setStaged(agent, s.key, next);
  }

  function updatePrefs(patch: Partial<ChatPrefs>) {
    setPrefs((p) => {
      const next = { ...p, ...patch };
      savePrefs(agent, next);
      return next;
    });
  }

  function pushSystem(body: string) {
    const s = chatStore.ensureActive(agent);
    chatStore.pushSystem(agent, s.key, body);
  }

  function newChat() {
    chatStore.newChat(agent);
    closeHistory(); // mobile: the drawer's job is done once a chat is chosen
  }

  function selectRow(row: Row) {
    // Don't navigate while an inline rename is open (a click elsewhere commits it).
    if (editingId) return;
    if (selectMode) {
      if (row.threadId) toggleSelected(row.threadId);
      return; // multi-select: the drawer stays open until the user is done
    }
    if (row.threadId) void chatStore.openThread(agent, row.threadId);
    else {
      chatStore.setActive(agent, row.key);
      chatStore.markRead(agent, row.key);
    }
    closeHistory();
  }

  async function deleteThread(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    const ok = await confirm({
      message: t("thread.delete.confirm"),
      confirmLabel: t("thread.deleteBtn"),
      cancelLabel: t("thread.cancel"),
      danger: true,
    });
    if (!ok) return;
    try {
      // The store drops the row (and its live session) the moment the server
      // agrees, ahead of the confirming refetch — a refetch that fails must
      // never leave a just-deleted thread as a ghost.
      await threadListStore.delete(agent, id);
      chatStore.ensureActive(agent);
    } catch {
      toast.push(t("toast.deleteFailed"), "error");
    }
  }

  // ── multi-select / bulk delete ─────────────────────────────────────────
  const allSelected = threads.length > 0 && selected.size === threads.length;

  function exitSelect() {
    setSelectMode(false);
    setSelected(new Set());
  }

  function toggleSelected(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(threads.map((th) => th.id)));
  }

  async function deleteSelected() {
    if (!selected.size) return;
    const ok = await confirm({
      message: t("thread.deleteSelected.confirm", { n: String(selected.size) }),
      confirmLabel: t("thread.deleteBtn"),
      cancelLabel: t("thread.cancel"),
      danger: true,
    });
    if (!ok) return;
    const ids = Array.from(selected);
    // Each DELETE that succeeds drops its own row at once (see deleteThread);
    // the ones that fail stay, and are reported together.
    const results = await Promise.allSettled(ids.map((id) => threadListStore.delete(agent, id)));
    if (results.some((r) => r.status === "rejected")) toast.push(t("toast.deleteFailed"), "error");
    chatStore.ensureActive(agent);
    exitSelect();
  }

  // ── attachments ────────────────────────────────────────────────────────
  const addFiles = useCallback(
    async (files: FileList | File[]) => {
      for (const file of Array.from(files)) {
        const res = await ingestFile(file);
        if (res.ok) setStaged((prev) => [...prev, res.attachment]);
        else
          toast.push(
            res.reason === "too_large" ? t("att.tooLarge", { name: file.name }) : t("att.unreadable", { name: file.name }),
            "error",
          );
      }
    },
    // setStaged is a stable-enough closure over `agent`; re-create with agent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [toast, t, agent],
  );

  function removeStaged(id: string) {
    setStaged((prev) => prev.filter((a) => a.id !== id));
  }

  function onPaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData?.items || [])
      .filter((it) => it.kind === "file")
      .map((it) => it.getAsFile())
      .filter((f): f is File => !!f);
    if (files.length) {
      e.preventDefault();
      void addFiles(files);
    }
  }

  function onDragEnter(e: React.DragEvent) {
    if (!Array.from(e.dataTransfer?.types || []).includes("Files")) return;
    e.preventDefault();
    dragDepth.current += 1;
    setDragging(true);
  }
  function onDragOver(e: React.DragEvent) {
    if (dragging) e.preventDefault();
  }
  function onDragLeave() {
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  }
  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    if (e.dataTransfer?.files?.length) void addFiles(e.dataTransfer.files);
  }

  // ── approvals ──────────────────────────────────────────────────────────
  function resolveApproval(msgId: number, idx: number, choice: ApprovalChoice) {
    if (session) chatStore.resolveApproval(agent, session.key, msgId, idx, choice);
  }

  function stop() {
    if (session) chatStore.stop(agent, session.key);
  }

  // Resend the last user message of an interrupted turn (connection dropped
  // mid-reply and the server had nothing more to recover).
  function retryTurn() {
    if (session) chatStore.retryLast(agent, session.key);
  }

  // ── slash commands ─────────────────────────────────────────────────────
  function helpText(): string {
    const rows = SLASH_COMMANDS.map(
      (c) => `- \`/${c.name}${c.args ? " " + c.args : ""}\` — ${c.descKey ? t(c.descKey) : c.desc || ""}`,
    ).join("\n");
    return `**${t("slash.heading")}**\n\n${rows}`;
  }

  function skillsListMd(): string {
    if (!skills.length) return `**${t("slash.skills.title", { agent: agentName })}**\n\n${t("slash.skills.none")}`;
    const rows = skills.map((s) => `- **${s.name}**${s.description ? ` — ${s.description}` : ""}`).join("\n");
    return `**${t("slash.skills.title", { agent: agentName })}**\n\n${rows}\n\n${t("slash.skills.hint")}`;
  }

  function subagentsListMd(): string {
    if (!subagents.length) return `**${t("slash.subagents.title", { agent: agentName })}**\n\n${t("slash.subagents.none")}`;
    const rows = subagents.map((s) => `- **${s.name}**${s.description ? ` — ${s.description}` : ""}`).join("\n");
    return `**${t("slash.subagents.title", { agent: agentName })}**\n\n${rows}`;
  }

  function toolsListMd(): string {
    if (!tools.length) return `**${t("slash.tools.title", { agent: agentName })}**\n\n${t("slash.tools.none")}`;
    const rows = tools
      .map((name) => {
        const desc = toolCatalog.current.get(name) || "";
        return `- \`${name}\`${desc ? ` — ${desc}` : ""}`;
      })
      .join("\n");
    return `**${t("slash.tools.title", { agent: agentName })}**\n\n${rows}`;
  }

  // Human label for a backend MCP status. The common states are localised; any
  // other status falls back to the raw value with underscores turned to spaces.
  function mcpStatusLabel(status: string): string {
    const known: Record<string, string> = {
      connected: t("slash.mcps.status.connected"),
      disabled: t("slash.mcps.disabled"),
      auth_required: t("slash.mcps.status.authRequired"),
      error: t("slash.mcps.status.error"),
    };
    return known[status] || status.replace(/_/g, " ");
  }

  function mcpsListMd(health?: { running: boolean; servers: McpHealthRow[] }): string {
    const title = `**${t("slash.mcps.title", { agent: agentName })}**`;
    // Prefer live health (connection status + tool counts, like the header MCP
    // indicator); fall back to the config-only list if the health fetch failed.
    const servers: McpHealthRow[] =
      health && health.servers.length
        ? health.servers
        : mcps.map((m) => ({
            url: m.url,
            transport: m.transport,
            status: m.enabled ? "unknown" : "disabled",
            ok: null,
            tool_count: 0,
            enabled: m.enabled,
          }));
    if (!servers.length) return `${title}\n\n${t("slash.mcps.none")}`;
    const rows = servers
      .map((s) => {
        const count =
          s.tool_count > 0
            ? ` · ${t(s.tool_count === 1 ? "slash.mcps.tool" : "slash.mcps.tools", {
                n: String(s.tool_count),
              })}`
            : "";
        return `- **${s.url}** — \`${s.transport}\` · ${mcpStatusLabel(s.status)}${count}`;
      })
      .join("\n");
    const note = health && !health.running ? `\n\n${t("slash.mcps.notRunning")}` : "";
    return `${title}\n\n${rows}${note}`;
  }

  // /mcps — fetch live MCP health so the listing shows real connection status
  // and tool counts (like the header indicator), not just the static config.
  async function showMcps() {
    let health: { running: boolean; servers: McpHealthRow[] } | undefined;
    if (agent) {
      try {
        const h = await api.get<{ running?: boolean; servers?: McpHealthRow[] }>(
          `/api/agents/${encodeURIComponent(agent)}/mcp/health`,
        );
        health = { running: h.running !== false, servers: h.servers || [] };
      } catch {
        /* fetch failed — fall back to the config-only listing */
      }
    }
    pushSystem(mcpsListMd(health));
  }

  // Reload handlers — refetch the live data behind the / listings. /reload-tools
  // also busts the server-side tool catalog cache so freshly added toolkits show.
  async function reloadSkills() {
    toast.push(t("reload.running"));
    try {
      api.invalidate("/api/catalog/skills"); // explicit refresh must not read the mount-time cache
      const n = await loadSkills();
      toast.push(t("reload.skills.done", { n: String(n) }));
    } catch {
      toast.push(t("reload.failed"), "error");
    }
  }

  async function reloadTools() {
    toast.push(t("reload.running"));
    try {
      await api.post("/api/catalog/tools/reload", {});
      api.invalidate("/api/catalog/tools"); // ditto — read past the mount-time cache
      await loadToolCatalog();
      const meta = await loadAgentMeta();
      toast.push(t("reload.tools.done", { n: String(meta.tools) }));
    } catch {
      toast.push(t("reload.failed"), "error");
    }
  }

  async function reloadMcps() {
    if (!agent) return;
    toast.push(t("reload.running"));
    try {
      // Rebuild + hot-swap the running agent's MCP tools (no restart) so newly
      // authorized / enabled servers become callable immediately.
      const r = await api.post<{ ok: boolean; running: boolean; servers?: { ok?: boolean }[] }>(
        `/api/agents/${encodeURIComponent(agent)}/mcp/reload`,
        {},
      );
      const meta = await loadAgentMeta();
      if (r.running === false) {
        toast.push(t("reload.mcps.done", { n: String(meta.mcps) }));
      } else {
        const live = (r.servers || []).filter((s) => s.ok).length;
        toast.push(t("reload.mcps.done", { n: String(live) }));
      }
    } catch (e) {
      toast.push(e instanceof ApiError ? e.detail : t("reload.failed"), "error");
    }
  }

  // Reload agent.json5 from disk and rebuild the live agent (restart re-reads
  // the config + re-wires tools/channels/scheduler). Thread state is persisted
  // on disk, so the conversation resumes after the rebuild.
  async function reloadAgent() {
    if (!agent) return;
    toast.push(t("reload.running"));
    try {
      await api.post(`/api/agents/${encodeURIComponent(agent)}/restart`, {});
      await loadAgentMeta();
      toast.push(t("reload.agent.done", { agent: agentName }));
    } catch (e) {
      toast.push(e instanceof ApiError ? e.detail : t("reload.failed"), "error");
    }
  }

  // Re-read vault secrets, re-hydrate env vars, and rebuild the agent so
  // rotated ${vault:...} references take effect.
  async function reloadVault() {
    if (!agent) return;
    toast.push(t("reload.running"));
    try {
      const res = await api.post<{ secrets?: number }>(
        `/api/agents/${encodeURIComponent(agent)}/reload-vault`,
        {},
      );
      await loadAgentMeta();
      toast.push(t("reload.vault.done", { n: String(res?.secrets ?? 0), agent: agentName }));
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        toast.push(t("reload.vault.locked"), "error");
      } else {
        toast.push(e instanceof ApiError ? e.detail : t("reload.failed"), "error");
      }
    }
  }

  // Plain-markdown fallback when the breakdown endpoint is unreachable — the
  // original /context output (model + window fill). Context-window fill is the
  // one token figure the dashboard still shows: it answers "how much room is
  // left", not "how much did this cost".
  function contextText(): string {
    const ctx = modelContextWindow(model);
    const fill = usage?.context ?? 0;
    const lines = [`**${t("context.heading")}**`, "", `- ${t("context.model")}: \`${model || "—"}\``];
    if (ctx && fill) {
      const pct = ((fill / ctx) * 100).toFixed(1);
      lines.push(`- ${t("context.window")}: ${(fill / 1000).toFixed(1)}k / ${(ctx / 1000).toFixed(0)}k (${pct}%)`);
    } else if (fill) {
      lines.push(`- ${t("context.window")}: ${fill.toLocaleString()} tokens`);
    }
    return lines.join("\n");
  }

  // Fetch the server-side static breakdown and render the rich /context card,
  // merging in this conversation's live token usage. Falls back to plain markdown
  // if the endpoint is unavailable (older runtime, network error).
  async function showContext() {
    if (!agent) {
      pushSystem(contextText());
      return;
    }
    try {
      const res = await api.get<{ model?: string; categories?: ContextCategory[] }>(
        `/api/agents/${encodeURIComponent(agent)}/context`,
      );
      const report: ContextReport = {
        model: res.model || model,
        categories: res.categories ?? [],
        usedContext: usage?.context ?? null,
      };
      const s = chatStore.ensureActive(agent);
      chatStore.pushContextCard(agent, s.key, report);
    } catch {
      pushSystem(contextText());
    }
  }

  async function compactThread() {
    const tid = session?.threadId;
    if (!tid) {
      toast.push(t("compact.noThread"), "error");
      return;
    }
    toast.push(t("compact.running"));
    try {
      const res = await api.post<{ ok: boolean; compacted: boolean; context_tokens?: number }>(
        `/api/agents/${encodeURIComponent(agent)}/threads/${encodeURIComponent(tid)}/compact`,
        {},
      );
      if (res.compacted) {
        await chatStore.refreshThread(agent, tid);
        // The header bar reads the last streamed turn's fill; a compaction
        // streams nothing, so seed it with the server's post-compaction
        // estimate or `/context` keeps printing the old number.
        if (typeof res.context_tokens === "number") {
          chatStore.setContextTokens(agent, tid, res.context_tokens);
        }
        loadThreads();
        toast.push(t("compact.done"));
      } else {
        toast.push(t("compact.empty"));
      }
    } catch {
      toast.push(t("compact.failed"), "error");
    }
  }

  // Persist a thread's display name (shared by the /rename command and the rail's
  // inline editor). Refreshes the rail so the new name shows immediately.
  async function persistThreadName(tid: string, name: string) {
    await threadListStore.rename(agent, tid, name);
  }

  async function renameThread(name: string) {
    const tid = session?.threadId;
    if (!tid) {
      toast.push(t("rename.noThread"), "error");
      return;
    }
    if (!name) {
      toast.push(t("rename.usage"));
      return;
    }
    try {
      await persistThreadName(tid, name);
      toast.push(t("rename.done", { name }));
    } catch {
      toast.push(t("rename.failed"), "error");
    }
  }

  // ── inline rename (pencil button on each rail row) ──────────────────────────
  // `editingRef` mirrors `editingId` so commit/cancel read the live value
  // synchronously — immune to the stale closure of an input's onBlur that fires
  // as the field unmounts (e.g. Escape, which must cancel, not save).
  function startRename(row: Row, e: React.MouseEvent) {
    e.stopPropagation();
    if (!row.threadId) return;
    editingRef.current = row.threadId;
    setEditingId(row.threadId);
    setEditDraft(row.name || "");
  }

  function cancelRename() {
    editingRef.current = null;
    setEditingId(null);
    setEditDraft("");
  }

  async function commitRename(tid: string) {
    if (editingRef.current !== tid) return; // already settled (e.g. Escape then blur)
    editingRef.current = null;
    const name = editDraft.trim();
    setEditingId(null);
    const current = threads.find((th) => th.id === tid)?.name || "";
    if (name === current) return; // unchanged — nothing to persist
    try {
      await persistThreadName(tid, name);
      toast.push(t("rename.done", { name }));
    } catch {
      toast.push(t("rename.failed"), "error");
    }
  }

  function setMode(mode: ChatMode) {
    updatePrefs({ mode });
    toast.push(t("mode.set", { mode: t(`mode.${mode}`) }));
  }

  function dispatchSlash(head: string, arg: string) {
    // A custom command (Commands tab) sends its prompt to the agent. `{args}`
    // is replaced with the user's text; otherwise the text is appended.
    const custom = customCmds.find((c) => c.name.toLowerCase() === head.toLowerCase());
    if (custom) {
      const msg = custom.prompt.includes("{args}")
        ? custom.prompt.replace("{args}", arg)
        : arg
          ? `${custom.prompt}\n\n${arg}`
          : custom.prompt;
      void runSend(msg, []);
      return;
    }
    // A per-skill command (e.g. /architecture-diagram) invokes that skill.
    const skill = skills.find((s) => s.name.toLowerCase() === head.toLowerCase());
    if (skill) {
      const msg = arg
        ? `Use your "${skill.name}" skill: ${arg}`
        : `Use your "${skill.name}" skill.`;
      void runSend(msg, []);
      return;
    }
    // A built-in the owner disabled for this agent is refused (not run).
    if (!cmdExposed(head)) {
      toast.push(t("slash.unknown", { name: head }), "error");
      return;
    }
    switch (head) {
      case "help":
        pushSystem(helpText());
        break;
      case "new":
        newChat();
        break;
      case "skills":
        pushSystem(skillsListMd());
        break;
      case "subagents":
        pushSystem(subagentsListMd());
        break;
      case "tools":
        pushSystem(toolsListMd());
        break;
      case "mcps":
        void showMcps();
        break;
      case "reload-skills":
        void reloadSkills();
        break;
      case "reload-tools":
        void reloadTools();
        break;
      case "reload-mcps":
        void reloadMcps();
        break;
      case "reload-agent":
        void reloadAgent();
        break;
      case "reload-vault":
        void reloadVault();
        break;
      case "context":
        void showContext();
        break;
      case "compact":
        void compactThread();
        break;
      case "quiet":
      case "normal":
      case "verbose": {
        // `/verbose off` keeps working (legacy) by falling back to normal.
        const detail: ChatDetail = head === "verbose" && arg === "off" ? "normal" : (head as ChatDetail);
        updatePrefs({ detail });
        toast.push(t("detail.set", { state: t(`detail.${detail}`) }));
        break;
      }
      case "auto":
      case "plan":
      case "manual":
      case "act":
        setMode(head);
        break;
      case "rename":
        void renameThread(arg);
        break;
      default:
        toast.push(t("slash.unknown", { name: head }), "error");
    }
  }

  // Select a command from the autocomplete menu: fill the composer when it
  // expects an argument, otherwise run it immediately.
  function chooseSlash(i: number) {
    const cmd = slashMatches[i];
    if (!cmd) return;
    if (cmd.args) {
      setInput(`/${cmd.name} `);
    } else {
      dispatchSlash(cmd.name, "");
      setInput("");
    }
  }

  // Run a one-shot shell command — shared by the composer "!" lane and the
  // inline "Run Now" button on a shell code block in the agent's reply. Captures
  // it server-side (full output saved to a per-run log), shows a head/tail
  // preview as a local block, and buffers the result for the next turn — never
  // auto-sent to the agent (it rides the next message or the "Send to Agent now"
  // chip). Stable identity (useCallback): it's a prop of every assistant
  // message's memoized <Markdown>, so a fresh closure per render would defeat
  // the memo and re-parse every message during streaming.
  const runShellCommand = useCallback(
    async (command: string) => {
      if (!agent || !command.trim() || shellBusy) return;
      const s = chatStore.ensureActive(agent);
      setShellBusy(true);
      try {
        const result = await runShell(command, agent);
        chatStore.pushShell(agent, s.key, result);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        toast.push(t("shell.error", { msg }), "error");
      } finally {
        setShellBusy(false);
      }
    },
    [agent, shellBusy, toast, t],
  );

  // Flush all buffered shell results to the agent now (no prose) — the "Send to
  // Agent now" chip action. send() drains the session buffer when shellResults
  // is omitted, so this rides the same path as a normal message.
  async function sendShellNow() {
    if (!agent) return;
    const s = chatStore.ensureActive(agent);
    if (s.streaming || s.shellBuffer.length === 0) return;
    await chatStore.send(agent, s.key, {
      text: "",
      attachments: [],
      mode: prefs.mode,
      model: s.model || undefined,
      reasoning_effort: s.reasoningEffort || undefined,
      errorLabel: t("error.label"),
    });
  }

  async function send() {
    setHistIdx(-1); // a send ends any history recall
    // Sending settles dictation: stop listening and keep what was said. Any
    // words the engine hasn't finalized yet land in the next draft rather than
    // holding up the send.
    if (voice.recording) voice.stop();
    const text = input.trim();
    // Shell mode — "!cmd" runs a one-shot command, captured locally; the agent
    // only sees it when the user sends a message (or hits "Send to Agent now").
    // Gated on terminal access; otherwise "!…" is just a normal message.
    if (canShell && text.startsWith("!")) {
      const command = text.slice(1).trim();
      setInput("");
      if (command && !shellBusy) void runShellCommand(command);
      return;
    }
    // Slash command — dispatch locally, never sent to the agent.
    if (text.startsWith("/")) {
      const parsed = parseSlash(text);
      setInput("");
      if (parsed) dispatchSlash(parsed.head, parsed.arg);
      return;
    }
    if (!text && staged.length === 0) return;
    // The agent is still working this conversation — queue the draft instead of
    // dropping it. It auto-sends when the current turn finishes (type-ahead);
    // the chip's Steer button (or the ▸▸ / Cmd+Enter steer) hands it to the
    // running turn instead, without stopping it.
    if (session?.streaming) {
      enqueueDraft();
      return;
    }
    const attachments = staged;
    setInput("");
    setStaged([]);
    await runSend(text, attachments);
  }

  // SendOpts carried into the store's queue drain (model + error label); the
  // queued message's own text/attachments/mode come from the QueuedMsg. The
  // model is the conversation's own (Session.model), never a per-agent pref.
  function drainOpts(): SendOpts {
    const s = chatStore.ensureActive(agent);
    return {
      text: "",
      attachments: [],
      mode: prefs.mode,
      model: s.model || undefined,
      reasoning_effort: s.reasoningEffort || undefined,
      errorLabel: t("error.label"),
    };
  }

  // Queue the current draft behind the running turn (type-ahead).
  function enqueueDraft() {
    setHistIdx(-1);
    const text = input.trim();
    if ((!text && staged.length === 0) || !agent) return;
    const s = chatStore.ensureActive(agent);
    chatStore.enqueue(agent, s.key, { text, attachments: staged, mode: prefs.mode }, drainOpts());
    setInput("");
    setStaged([]);
    scrollToLatest();
  }

  // Steer = hand this draft to the reply in progress: the agent reads it at its
  // next step (after the tool calls in flight) without stopping. If the reply
  // finishes first, the store sends it as the next message.
  function steer() {
    setHistIdx(-1);
    const text = input.trim();
    if ((!text && staged.length === 0) || !agent) return;
    const s = chatStore.ensureActive(agent);
    chatStore.steer(agent, s.key, { text, attachments: staged, mode: prefs.mode }, drainOpts());
    setInput("");
    setStaged([]);
    scrollToLatest();
  }

  // The agent asked a person to take its browser (to sign in, say) and they
  // have handed it back: tell the agent, in the conversation where everyone can
  // see it, so it carries on. Mid-turn the note reaches the running reply.
  function browserHandedBack() {
    if (!agent) return;
    const text = t("browser.handedBack");
    if (session?.streaming) {
      const s = chatStore.ensureActive(agent);
      chatStore.steer(agent, s.key, { text, attachments: [], mode: prefs.mode }, drainOpts());
      return;
    }
    void runSend(text, []);
  }

  // Stream one user turn (text + attachments) to the agent through the store.
  // The store runs the stream independently of this component, so it survives
  // navigation and runs alongside other agents'/threads' streams.
  async function runSend(text: string, attachments: Attachment[], modeOverride?: ChatMode) {
    if ((!text && attachments.length === 0) || !agent) return;
    // Sending re-pins the log: even if the user had scrolled up, their own new
    // message (and the reply) is what they want to see next. Re-pinning is
    // enough — the rev effect follows the content as it lands.
    scrollToLatest();
    const s = chatStore.ensureActive(agent);
    await chatStore.send(agent, s.key, {
      text,
      attachments,
      mode: modeOverride ?? prefs.mode,
      model: s.model || undefined,
      reasoning_effort: s.reasoningEffort || undefined,
      errorLabel: t("error.label"),
    });
  }

  // ── inline edit of a past user message ─────────────────────────────────────
  // Open the message for editing: seed the draft with its current body and focus
  // the textarea (caret parked at the end so the user can keep typing).
  function beginEditMessage(m: Msg) {
    setEditingMsgId(m.id);
    setMsgEditDraft(m.body);
    requestAnimationFrame(() => {
      const ta = msgEditRef.current;
      if (ta) {
        ta.focus();
        ta.selectionStart = ta.selectionEnd = ta.value.length;
      }
    });
  }

  function cancelEditMessage() {
    setEditingMsgId(null);
    setMsgEditDraft("");
  }

  // Resend an edited user message. Like retryLast, this fires a NEW turn rather
  // than rewriting history — the model still sees prior context through the
  // thread checkpoint. The original attachments ride along with the edited text.
  // While a turn is streaming we queue it (type-ahead), matching send().
  function resendEditedMessage(m: Msg) {
    const text = msgEditDraft.trim();
    const attachments = m.attachments ?? [];
    if (!text && attachments.length === 0) return;
    setEditingMsgId(null);
    setMsgEditDraft("");
    if (!agent) return;
    if (session?.streaming) {
      const s = chatStore.ensureActive(agent);
      chatStore.enqueue(agent, s.key, { text, attachments, mode: prefs.mode }, drainOpts());
      return;
    }
    void runSend(text, attachments);
  }

  // Plan-mode action shown under a drafted plan: approve it, switch to Act mode,
  // and tell the agent to execute. To refine instead, the user just types a
  // message (still in Plan mode) — no dedicated button needed.
  function approvePlan() {
    updatePrefs({ mode: "act" });
    void runSend(t("plan.approveMsg"), [], "act");
  }

  // Drop the recalled text into the composer and park the caret at the end so
  // the next ArrowUp/ArrowDown (on a single-line entry) keeps cycling and the
  // message is ready to edit/send.
  function applyRecall(text: string) {
    setInput(text);
    requestAnimationFrame(() => {
      const ta = taRef.current;
      if (ta) ta.selectionStart = ta.selectionEnd = ta.value.length;
    });
  }

  // Shell-style history recall: with the slash menu closed, ArrowUp walks back
  // through previously sent messages and ArrowDown walks forward, restoring the
  // live draft once you pass the newest. We only hijack the key when the caret
  // sits on the composer's first (ArrowUp) / last (ArrowDown) line so multi-line
  // editing still moves the caret normally. Returns true if it handled the key.
  function recallHistory(e: React.KeyboardEvent): boolean {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return false;
    if (e.shiftKey || e.metaKey || e.ctrlKey || e.altKey) return false;
    if (userHistory.length === 0) return false;
    const ta = taRef.current;
    const caret = ta ? ta.selectionStart : input.length;
    const caretEnd = ta ? ta.selectionEnd : input.length;
    if (caret !== caretEnd) return false; // a selection — leave it alone
    const onFirstLine = input.slice(0, caret).indexOf("\n") === -1;
    const onLastLine = input.slice(caret).indexOf("\n") === -1;
    // Guard a stale index if the thread reloaded mid-recall.
    const idx = histIdx >= userHistory.length ? -1 : histIdx;

    if (e.key === "ArrowUp") {
      if (!onFirstLine) return false;
      if (idx === -1) {
        histDraftRef.current = input; // stash the in-progress draft
        const next = userHistory.length - 1;
        setHistIdx(next);
        applyRecall(userHistory[next]);
      } else if (idx > 0) {
        const next = idx - 1;
        setHistIdx(next);
        applyRecall(userHistory[next]);
      }
      // idx === 0 → already at the oldest; swallow the key to hold position.
      e.preventDefault();
      return true;
    }

    // ArrowDown — only meaningful while navigating history.
    if (idx === -1 || !onLastLine) return false;
    if (idx < userHistory.length - 1) {
      const next = idx + 1;
      setHistIdx(next);
      applyRecall(userHistory[next]);
    } else {
      // Past the newest — back to the live draft we stashed on entry.
      setHistIdx(-1);
      applyRecall(histDraftRef.current);
    }
    e.preventDefault();
    return true;
  }

  function onComposerKey(e: React.KeyboardEvent) {
    if (slashOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIndex((i) => Math.min(i + 1, slashMatches.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        chooseSlash(Math.min(slashIndex, slashMatches.length - 1));
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setInput("");
        return;
      }
    }
    // Slash menu closed → ArrowUp/ArrowDown recall previously sent messages.
    if (recallHistory(e)) return;
    // Cmd/Ctrl+Enter while a turn streams = steer (the agent reads it at its
    // next step, without stopping).
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && streaming) {
      e.preventDefault();
      steer();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      if (coarsePointer) return; // Return = newline on touch; the send button submits
      e.preventDefault();
      void send();
    }
  }

  return (
    <>
      <div className="topbar">
        <div className="row" style={{ gap: 10 }}>
          <strong>{t("nav.chat")}</strong>
          <span className="muted" style={{ fontSize: 13 }}>{agentName}</span>
        </div>
      </div>

      <div
        className="chat-split"
        style={{
          display: "grid",
          // The drawer is out of flow at every viewport, so the conversation
          // always owns the single column whether or not the rail is mounted.
          gridTemplateColumns: "1fr",
          height: "calc(100vh - var(--topbar-h))",
        }}
      >
        {/* Backdrop: click anywhere off the list to dismiss (plus Escape / focus
            trap via the shared dialog hook below). */}
        {historyOpen && (
          <>
            <div className="chat-rail-backdrop" onClick={closeHistory} />
            <ChatRailA11y panelRef={railRef} onClose={closeHistory} />
          </>
        )}

        {/* Threads rail — an off-canvas drawer on the right at every viewport.
            Its layout lives in .chat-rail / .chat-rail.is-drawer rather than
            inline, because an inline `position` would outrank the drawer's
            `position: fixed` and leave it in flow. The width rides a custom
            property for the same reason: an inline width would outrank the
            mobile cap. */}
        {railVisible && (
        <aside
          ref={railRef}
          className="chat-rail is-drawer"
          aria-label={t("history.title")}
          style={{ "--chat-rail-w": `${railWidth}px` } as React.CSSProperties}
        >
          <div className="chat-rail-head">
            {/* The same tinted clock that opened it, so the drawer is visibly
                the thing behind that button rather than an unrelated panel. */}
            <strong className="chat-rail-title">
              <History size={14} strokeWidth={2} aria-hidden />
              {t("history.title")}
            </strong>
            <button
              className="btn ghost xs"
              onClick={closeHistory}
              aria-label={t("history.close")}
              title={t("history.close")}
            >
              <X size={15} strokeWidth={2} />
            </button>
          </div>
          <div
            style={{
              flex: 1,
              minHeight: 0,
              overflow: "auto",
              padding: 10,
              display: "flex",
              flexDirection: "column",
              gap: 3,
            }}
          >
          <button className="btn primary" style={{ marginBottom: 6, justifyContent: "center" }} onClick={newChat}>
            <PlusIcon size={15} /> {t("chat.new")}
          </button>

          {threads.length > 0 && (
            <div className="thread-toolbar">
              {selectMode ? (
                <>
                  <button className="btn ghost xs" onClick={toggleAll}>
                    {allSelected ? t("thread.deselectAll") : t("thread.selectAll")}
                  </button>
                  <span style={{ flex: 1 }} />
                  <button
                    className="btn danger xs"
                    disabled={selected.size === 0}
                    onClick={() => void deleteSelected()}
                    title={t("thread.delete.aria")}
                  >
                    <Trash2 size={13} strokeWidth={1.75} /> {selected.size > 0 ? selected.size : ""}
                  </button>
                  <button className="btn ghost xs" onClick={exitSelect} aria-label={t("thread.done")} title={t("thread.done")}>
                    <X size={14} strokeWidth={2} />
                  </button>
                </>
              ) : (
                <>
                  <span className="muted" style={{ fontSize: 11, flex: 1 }}>
                    {t(threads.length === 1 ? "thread.count.one" : "thread.count.many", { n: String(threads.length) })}
                  </span>
                  <button className="btn ghost xs" onClick={() => setSelectMode(true)} title={t("thread.edit")}>
                    <Pencil size={12} strokeWidth={1.75} /> {t("thread.edit")}
                  </button>
                </>
              )}
            </div>
          )}

          {rows.length === 0 ? (
            <div className="muted" style={{ fontSize: 12, padding: "10px 6px", textAlign: "center" }}>
              {t("thread.empty")}
            </div>
          ) : (
            rows.map((row) => {
              const isSel = row.threadId ? selected.has(row.threadId) : false;
              const selectable = selectMode && !!row.threadId;
              // Guard against null===null matching a draft row that has no thread id.
              const isEditing = !!row.threadId && editingId === row.threadId;
              const timeLabel = relTime(row.updatedAt, t) || t("time.now");
              return (
                <div
                  key={row.key}
                  className={`thread-item ${!selectMode && activeKey === row.key ? "active" : ""} ${selectable && isSel ? "selected" : ""}`}
                  data-testid="thread-item"
                  onClick={() => selectRow(row)}
                >
                  {selectable && (
                    <span className={`thread-check ${isSel ? "is-on" : ""}`} aria-hidden>
                      {isSel && <CheckIcon size={12} />}
                    </span>
                  )}
                  <div style={{ overflow: "hidden", flex: 1, minWidth: 0 }}>
                    {isEditing ? (
                      <input
                        className="thread-rename-input"
                        autoFocus
                        value={editDraft}
                        placeholder={t("thread.rename.placeholder")}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => setEditDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void commitRename(row.threadId as string);
                          } else if (e.key === "Escape") {
                            e.preventDefault();
                            cancelRename();
                          }
                        }}
                        onBlur={() => void commitRename(row.threadId as string)}
                      />
                    ) : (
                      <div className="thread-title">
                        <OriginPill origin={row.origin} t={t} />
                        {/* A platform row earns its badge here, beside the origin
                            pill, because that is where a reader looks to tell
                            what KIND of row this is. Only messaging platforms:
                            a dashboard chat is the baseline the rest are read
                            against, exactly why `interactive` has no origin pill.
                            When a row somehow carries both (a scheduled fan-out
                            really does run on Telegram), the channel goes
                            icon-only so two badges can't crowd out the title. */}
                        {isPlatformChannel(row.channel) && (
                          <ChannelPill
                            channel={row.channel}
                            t={t}
                            // …except beside EXTERNAL: there the channel IS the
                            // protocol the outside system used (MCP / A2A), which
                            // is the one thing the row must say, and both words
                            // are short enough to share the line.
                            compact={!!ORIGIN_ICONS[row.origin] && row.origin !== "external"}
                          />
                        )}
                        <span className="thread-title-text">
                          {row.name || row.title || row.preview || t("thread.fallback")}
                        </span>
                      </div>
                    )}
                    <div className="muted thread-meta">
                      {sourceLabel(row) && (
                        <span className="thread-src" title={sourceLabel(row)}>
                          {sourceLabel(row)} ·
                        </span>
                      )}
                      {/* Both books, each in its own unit, never summed: the
                          credits the Olano gateway charged for turns on Olano
                          models, and the dollars the customer's provider bills
                          for turns on their own keys. A thread carries both
                          when the model was switched mid-conversation. The
                          title repeats the line because a narrow rail can
                          ellipsis it — and half a price is not a price. */}
                      <span
                        className={
                          (row.credits ?? 0) > 0 && (row.usd ?? 0) > 0
                            ? "thread-counts is-two-books"
                            : "thread-counts"
                        }
                        title={countsLabel(row, timeLabel, t)}
                      >
                        {countsLabel(row, timeLabel, t)}
                      </span>
                    </div>
                  </div>
                  {!selectMode && !isEditing && row.streaming && (
                    <span className="thread-dot" title={t("thread.streaming")} aria-label={t("thread.streaming")} />
                  )}
                  {!selectMode && !isEditing && row.unread > 0 && (
                    <span className="thread-badge" title={t("thread.unread", { n: String(row.unread) })}>
                      {row.unread}
                    </span>
                  )}
                  {!selectMode && !isEditing && row.threadId && (
                    <>
                      <button
                        className="thread-rename"
                        aria-label={t("thread.rename.aria")}
                        title={t("thread.rename.aria")}
                        onClick={(e) => startRename(row, e)}
                      >
                        <Pencil size={13} strokeWidth={1.75} />
                      </button>
                      <button
                        className="thread-del"
                        aria-label={t("thread.delete.aria")}
                        onClick={(e) => void deleteThread(row.threadId as string, e)}
                      >
                        <Trash2 size={13} strokeWidth={1.75} />
                      </button>
                    </>
                  )}
                </div>
              );
            })
          )}
          </div>

          <ResizeHandle
            active={resizing}
            title={t("resize.title")}
            onMouseDown={startResize}
            onDoubleClick={resetResize}
          />
        </aside>
        )}

        {/* The agent's profile card, from the header's info button — the same
            drawer treatment as the list (right edge, backdrop, Escape, focus
            trap), with the card the simple shell docks beside its chat inside.
            The card is the landmark; this wrapper only places it. */}
        {profileOpen && !!agent && (
          <>
            <div className="chat-rail-backdrop" onClick={closeProfile} />
            <ChatRailA11y panelRef={profileRef} onClose={closeProfile} />
            <div ref={profileRef} className="chat-profile-drawer" data-testid="chat-profile-drawer">
              <BotProfilePanel
                agent={agent}
                bot={current}
                asOverlay={false}
                onClose={closeProfile}
                onChanged={reload}
              />
            </div>
          </>
        )}

        {/* conversation */}
        <div
          className="chat"
          style={{ height: "100%", position: "relative" }}
          onDragEnter={onDragEnter}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={onDrop}
        >
          {/* The agent's live browser, while it has one open (views/browser). */}
          {agent && (
            <ChatBrowserDock
              agentName={agent}
              browserCalls={browserCalls}
              approvalPending={messages.some((m) =>
                (m.approvals || []).some((ap) => !ap.resolved && needsPersonEveryTime(ap.actions)),
              )}
              onShowApproval={showPendingApproval}
              onHandBack={browserHandedBack}
            />
          )}
          <ChatHeader
            mode={prefs.mode}
            detail={prefs.detail}
            model={model}
            defaultModel={current?.model || ""}
            reasoningEffort={session?.reasoningEffort || ""}
            usage={usage}
            agentName={agent || ""}
            channel={threadChannel}
            historyOpen={historyOpen}
            profileOpen={profileOpen}
            onToggleRail={toggleRail}
            onToggleProfile={toggleProfile}
            onNewChat={newChat}
            onCycleMode={() => updatePrefs({ mode: nextMode(prefs.mode) })}
            onCycleDetail={() => updatePrefs({ detail: nextDetail(prefs.detail) })}
            onSelectModel={(m) => {
              const s = chatStore.ensureActive(agent);
              chatStore.setModel(agent, s.key, m);
            }}
            onSelectEffort={(v) => {
              const s = chatStore.ensureActive(agent);
              chatStore.setReasoningEffort(agent, s.key, v);
            }}
            onAddBot={groupsEnabled && agents.length > 1 ? () => setAddBotOpen(true) : undefined}
          />

          {dragging && (
            <div className="chat-drop">
              <Paperclip size={20} /> {t("att.drop")}
            </div>
          )}

          <div className="chat-log" ref={logRef} onScroll={updatePinned}>
            {/* While the arrival above is still choosing a conversation there
                is nothing to say yet — a "say something" prompt that then
                turns into last week's Telegram thread reads as a glitch. */}
            {messages.length === 0 && arriving && !session && (
              <div className="spinner" style={{ margin: "auto" }} aria-label={t("thread.loading")} />
            )}
            {messages.length === 0 && !(arriving && !session) && waking && (
              <div
                role="status"
                aria-busy="true"
                style={{
                  margin: "auto",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 10,
                  textAlign: "center",
                  color: "var(--text-3)",
                  maxWidth: 360,
                }}
              >
                <span className="spinner" style={{ width: 18, height: 18 }} />
                <div style={{ color: "var(--text-2)" }}>{t("chat.waking", { agent: agentName })}</div>
                <div style={{ fontSize: 12.5 }}>{t("chat.wakingHint")}</div>
              </div>
            )}
            {messages.length === 0 && !(arriving && !session) && !waking && (
              <div style={{ margin: "auto", textAlign: "center", color: "var(--text-3)" }}>
                {t("chat.empty", { agent: agentName })}
              </div>
            )}
            {messages.map((m, i) => {
              if (m.role === "system") {
                return (
                  <div className="chat-system" key={m.id}>
                    {/* The connect card carries its own ✅/⚠️ status icon, so
                        the row's generic info glyph would just double it up. */}
                    {!m.connectResult && <Info size={13} />}
                    <div className="md">
                      {m.connectResult ? (
                        <ConnectResultCard result={m.connectResult} />
                      ) : m.context ? (
                        <ContextCard report={m.context} onRunCommand={(cmd) => dispatchSlash(cmd, "")} />
                      ) : (
                        <Markdown text={m.body} />
                      )}
                    </div>
                  </div>
                );
              }
              const streamingThis = streaming && i === messages.length - 1 && m.role === "assistant";
              const hasBlocks = !!(m.todos?.length || m.approvals?.length);
              // Detail gates: Quiet renders no tool calls at all; subagent
              // transcripts only ever show in Verbose.
              const showTools = prefs.detail !== "quiet";
              const showSubs = prefs.detail === "verbose";
              const verboseTools = prefs.detail === "verbose";
              // Sign-in links mined from tool results render as Connect cards
              // at every detail level (like media) — they are the action the
              // turn asks of the user, not tool noise.
              const connects = m.role === "assistant" ? extractConnectLinks(m.tools) : [];
              // The reply in the order it happened — tool cards and text
              // spans interleaved (turnBlocks.ts) — and, once the backend has
              // named the files the reply mentions, the index that links
              // them (memoized per media array, so the Markdown memo holds).
              const blocks = m.role === "assistant" ? buildTurnBlocks(m.tools, m.segments, m.body) : [];
              const fileIndex = m.role === "assistant" ? fileIndexFor(m.media) : undefined;
              // Skip stray empty assistant turns (e.g. a tool-only turn with no
              // text) so we never show a blank consecutive bubble — accounting
              // for which blocks are actually visible at this detail level.
              const hasVisible =
                !!m.body ||
                (showTools && m.tools.some((c) => c.name)) ||
                (showSubs && m.subs.length > 0) ||
                !!m.media?.length ||
                connects.length > 0 ||
                hasBlocks;
              if (m.role === "assistant" && !streamingThis && !hasVisible) {
                return null;
              }
              return (
                <div className="msg" key={m.id}>
                  {m.role === "user" ? (
                    <span className="avatar-sm">{initials(user?.name || user?.email || "You")}</span>
                  ) : current?.avatar_url ? (
                    <img className="avatar-sm" src={current.avatar_url} alt="" style={{ objectFit: "cover" }} />
                  ) : (
                    <span className="avatar-sm" style={{ background: "var(--accent-soft)", color: "var(--accent)" }}>
                      {initials(agentName)}
                    </span>
                  )}
                  <div className="msg-body">
                    <div className="msg-name">
                      {/* Every thread of an agent is shared by everyone who may
                          chat with it, so a user row names its author. "You" is
                          reserved for rows with no stamp (single-user installs,
                          older transcripts) and for the signed-in human's own. */}
                      {m.role === "user"
                        ? m.sender && m.sender !== user?.name
                          ? m.sender
                          : t("common.you")
                        : agentName}
                      {!!m.channel && m.channel !== threadChannel && (
                        <ChannelPill channel={m.channel} t={t} />
                      )}
                      {/* A message steered into the reply mid-turn, not one
                          that started a turn — the agent read it at its next
                          step, between two of its tool calls. */}
                      {m.role === "user" && m.steer && (
                        <span className="steer-pill" title={t("msg.steeredTitle")}>
                          <FastForward size={10} />
                          {t("msg.steered")}
                        </span>
                      )}
                    </div>

                    {/* The turn as it ran: a "tools" block per run of tool
                        calls, a text bubble per span of the reply between them
                        (turnBlocks.ts). Tool cards — Quiet → none at all.
                        Otherwise only named calls render (empty-name partials
                        are noise): Verbose → full input (+ output) cards;
                        Normal → a single compact peek line per call. */}
                    {blocks.map((block, bi) =>
                      block.kind === "text" ? (
                        <div className="bubble assistant" key={`text-${bi}`}>
                          <Markdown
                            text={block.text}
                            files={fileIndex}
                            onNavigate={navigate}
                            onRunCommand={canShell ? runShellCommand : undefined}
                          />
                        </div>
                      ) : (
                        showTools && (
                          <Fragment key={`tools-${bi}`}>
                            {block.tools
                              .filter((c) => c.name)
                              .map((c) =>
                                verboseTools ? (
                                  <Fragment key={c.id}>
                                    <div className="tool-card" style={{ display: "flex", alignItems: "flex-start", gap: 6 }}>
                                      <ToolsIcon size={12} /> {c.name}({(c.args || "").slice(0, 400)})
                                    </div>
                                    {c.hasResult && (
                                      <div className="tool-card" style={{ display: "flex", alignItems: "flex-start", gap: 6 }}>
                                        <CheckIcon size={12} /> {(c.result || "").slice(0, 600) || c.name}
                                      </div>
                                    )}
                                  </Fragment>
                                ) : (
                                  (() => {
                                    const isTask = c.name === "task";
                                    const info = isTask ? parseTaskArgs(c.args) : null;
                                    const label = isTask
                                      ? t("activity.subagent", { name: info?.subagent || t("subagent.fallback") })
                                      : t("activity.tool", { name: c.name });
                                    const pk = peekText(isTask ? info?.description || "" : c.args || "");
                                    return (
                                      <div className="tool-activity" key={c.id}>
                                        {isTask ? <SubIcon size={12} /> : <ToolsIcon size={12} />}
                                        <span className="tool-activity-label">{label}</span>
                                        {pk && <span className="tool-activity-peek">{pk}</span>}
                                      </div>
                                    );
                                  })()
                                ),
                              )}
                          </Fragment>
                        )
                      ),
                    )}

                    {showSubs &&
                      m.subs.map((s) => (
                        <div key={s.path}>
                          <div
                            className="muted"
                            style={{ fontSize: 11, margin: "6px 0 2px", display: "flex", alignItems: "center", gap: 4 }}
                          >
                            <SubIcon size={12} /> {s.path}
                          </div>
                          <div className="bubble assistant">
                            <Markdown text={s.text} />
                          </div>
                        </div>
                      ))}

                    {m.todos && m.todos.length > 0 && <TodoList items={m.todos} live={streamingThis} />}

                    {m.approvals?.map((ap, idx) => (
                      <ApprovalCard key={idx} state={ap} onDecide={(c) => resolveApproval(m.id, idx, c)} />
                    ))}

                    {m.role === "user" ? (
                      <>
                        {m.attachments && <MessageAttachments items={m.attachments} />}
                        {/* Reopened threads: uploaded files come back as server
                            media refs (uploads/ + /api/files-raw) instead of the
                            live turn's local base64 previews. */}
                        {m.media && <AgentMedia items={m.media} />}
                        {editingMsgId === m.id ? (
                          // Inline editor — swap the bubble for a textarea plus
                          // Send/Cancel. Enter sends, Shift+Enter adds a newline,
                          // Escape cancels. On touch devices Return adds a
                          // newline instead — the Send button submits.
                          <div className="msg-edit">
                            <textarea
                              ref={msgEditRef}
                              className="msg-edit-input"
                              value={msgEditDraft}
                              onChange={(e) => setMsgEditDraft(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter" && !e.shiftKey && !coarsePointer) {
                                  e.preventDefault();
                                  resendEditedMessage(m);
                                } else if (e.key === "Escape") {
                                  e.preventDefault();
                                  cancelEditMessage();
                                }
                              }}
                              rows={1}
                            />
                            <div className="msg-edit-actions">
                              <button className="btn ghost sm" type="button" onClick={cancelEditMessage}>
                                {t("msg.editCancel")}
                              </button>
                              <button
                                className="btn primary sm"
                                type="button"
                                disabled={!msgEditDraft.trim() && !(m.attachments && m.attachments.length > 0)}
                                onClick={() => resendEditedMessage(m)}
                              >
                                {t("msg.editSend")}
                              </button>
                            </div>
                          </div>
                        ) : (
                          <>
                            {m.body && <div className="bubble user">{m.body}</div>}
                            {/* Copy / Edit the message. Edit swaps in the inline
                                editor above; saving resends as a new turn. */}
                            {m.body && (
                              <div className="msg-actions">
                                <CopyButton
                                  text={m.body}
                                  label={t("msg.copy")}
                                  copiedLabel={t("msg.copied")}
                                  onFail={() => toast.push(t("msg.copyFailed"), "error")}
                                />
                                <button
                                  className="msg-edit-btn"
                                  type="button"
                                  title={t("msg.edit")}
                                  aria-label={t("msg.edit")}
                                  onClick={() => beginEditMessage(m)}
                                >
                                  <Pencil size={13} /> {t("msg.edit")}
                                </button>
                                {m.ts && <MsgTime ts={m.ts} now={nowTick} t={t} />}
                              </div>
                            )}
                          </>
                        )}
                      </>
                    ) : (
                      <>
                        {/* The reply text is drawn above, among the tool
                            cards, in the order the turn produced it. */}
                        {connects.map((link) => (
                          <ConnectCard key={link.href} link={link} />
                        ))}
                        {m.media && <AgentMedia items={m.media} />}
                        {/* Copy the reply — shown once the turn has finished
                            streaming (a partial reply isn't worth copying). */}
                        {m.body && !streamingThis && (
                          <div className="msg-actions">
                            <CopyButton
                              text={m.body}
                              label={t("msg.copy")}
                              copiedLabel={t("msg.copied")}
                              onFail={() => toast.push(t("msg.copyFailed"), "error")}
                            />
                            <ReplyFeedback
                              agent={agent}
                              threadId={session?.threadId}
                              msgId={m.id}
                              turnId={m.turnId}
                              vote={m.feedback}
                            />
                            <LangfuseLink href={traceUrl(langfuseLinks, m.traceId)} kind="trace" />
                            {m.ts && <MsgTime ts={m.ts} now={nowTick} t={t} />}
                          </div>
                        )}
                        {streamingThis && (
                          <div className="typing" aria-label={t("chat.thinking")}>
                            <span />
                            <span />
                            <span />
                          </div>
                        )}
                        {/* The socket died mid-turn but the RUN did not: the
                            agent kept working server-side (olano/core/runs.py)
                            and this tab is replaying what it missed. Nothing
                            was lost, so this deliberately does NOT say
                            "Interrupted" and offers no Retry — the reply is
                            still arriving. */}
                        {i === messages.length - 1 && !streaming && session?.attaching && (
                          <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
                            <span
                              className="badge"
                              style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "1px 8px" }}
                            >
                              <span className="spinner" style={{ width: 12, height: 12 }} />
                              {t("reattach.chip")}
                            </span>
                            <span className="muted" style={{ fontSize: 12 }}>
                              {t("reattach.line", { agent: agentName })}
                            </span>
                          </div>
                        )}
                        {/* The connection died mid-turn AND the run is gone (it
                            finished long enough ago to be swept, or the runtime
                            restarted). The partial stays visible above; this
                            block narrates the reconcile and offers Retry when
                            the server had nothing more to recover. */}
                        {i === messages.length - 1 && !streaming && !session?.attaching && session?.interrupted && (
                          <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
                            <span
                              className="badge"
                              style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "1px 8px", color: "var(--warn)", background: "var(--warn-soft)" }}
                            >
                              <WifiOff size={12} /> {t("interrupted.chip")}
                            </span>
                            {session.reconciling ? (
                              <span className="muted" style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 6 }}>
                                <span className="spinner" style={{ width: 12, height: 12 }} />
                                {t("interrupted.checking")}
                              </span>
                            ) : (
                              <>
                                <span className="muted" style={{ fontSize: 12 }}>
                                  {t("interrupted.line", { agent: agentName })}
                                </span>
                                <button
                                  className="btn"
                                  style={{ padding: "3px 10px", fontSize: 12 }}
                                  title={t("interrupted.retryTitle")}
                                  onClick={retryTurn}
                                >
                                  <RotateCcw size={12} /> {t("interrupted.retry")}
                                </button>
                              </>
                            )}
                          </div>
                        )}
                        {i === messages.length - 1 && !streaming && !session?.interrupted && !!session?.recoveredAt && (
                          <div
                            style={{ display: "inline-flex", alignItems: "center", gap: 5, marginTop: 8, fontSize: 12, color: "var(--success)" }}
                          >
                            <CheckIcon size={12} /> {t("interrupted.recovered")}
                          </div>
                        )}
                        {/* Hard failure before the stream opened (HTTP error,
                            server unreachable): the bubble above carries the
                            error text — offer a one-click resend instead of
                            making the user retype. */}
                        {i === messages.length - 1 && !streaming && !session?.interrupted && !!session?.error && (
                          <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
                            <button
                              className="btn"
                              style={{ padding: "3px 10px", fontSize: 12 }}
                              title={t("interrupted.retryTitle")}
                              onClick={retryTurn}
                            >
                              <RotateCcw size={12} /> {t("interrupted.retry")}
                            </button>
                          </div>
                        )}
                        {m.planProposal && i === messages.length - 1 && !streaming && (
                          <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                            <button
                              className="btn primary"
                              style={{ padding: "4px 11px", fontSize: 12 }}
                              onClick={approvePlan}
                            >
                              <Play size={13} /> {t("plan.approveRun")}
                            </button>
                          </div>
                        )}
                        {m.remediation && (
                          <ErrorRemediation remediation={m.remediation} onNewThread={newChat} />
                        )}
                      </>
                    )}
                  </div>
                </div>
              );
            })}
            {/* A turn running on this thread from ANOTHER surface — Telegram,
                a scheduled job, the OpenAI connector, a second device. The
                runtime streams the reply-so-far in throttled previews; the
                persisted rows (tool calls, the final reply) land above through
                the same fold a reload uses, and the `idle` frame clears this. */}
            {!streaming && session?.activity && (
              <div className="msg" key="remote-activity">
                {current?.avatar_url ? (
                  <img className="avatar-sm" src={current.avatar_url} alt="" style={{ objectFit: "cover" }} />
                ) : (
                  <span className="avatar-sm" style={{ background: "var(--accent-soft)", color: "var(--accent)" }}>
                    {initials(agentName)}
                  </span>
                )}
                <div className="msg-body">
                  <div className="msg-name">
                    {agentName}
                    {!!session.activity.channel && session.activity.channel !== threadChannel && (
                      <ChannelPill channel={session.activity.channel} t={t} />
                    )}
                  </div>
                  {session.activity.text && (
                    <div className="md">
                      <Markdown text={session.activity.text} />
                    </div>
                  )}
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <div
                      className="typing"
                      aria-label={t("chat.thinking")}
                    >
                      <span />
                      <span />
                      <span />
                    </div>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {session.activity.channel
                        ? t("chat.remote.working", { channel: channelLabel(session.activity.channel, t) })
                        : t("chat.remote.workingUnknown")}
                    </span>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Escape hatch back to the live end of the log once the auto-follow
              pin is released (the user scrolled up). Zero-height wrapper so the
              pill floats over the log without shifting the composer. */}
          {!pinned && (
            <div className="chat-jump-wrap">
              <button
                className="chat-jump"
                type="button"
                onClick={() => scrollToLatest("smooth")}
              >
                <ArrowDown size={13} /> {t("chat.jumpLatest")}
              </button>
            </div>
          )}

          <div className="composer">
            {slashOpen && (
              <div className="slash-menu">
                {slashMatches.map((c, i) => {
                  const active = i === Math.min(slashIndex, slashMatches.length - 1);
                  const group = c.group || "command";
                  const prevGroup = i > 0 ? slashMatches[i - 1].group || "command" : null;
                  const desc = c.descKey ? t(c.descKey) : c.desc || "";
                  return (
                    <Fragment key={`${group}:${c.name}`}>
                      {group !== prevGroup && (
                        <div className="slash-group">
                          {group === "skill" ? t("slash.group.skills") : t("slash.group.commands")}
                        </div>
                      )}
                      <div
                        className={`slash-item ${active ? "active" : ""}`}
                        onMouseEnter={() => setSlashIndex(i)}
                        onMouseDown={(e) => {
                          e.preventDefault();
                          chooseSlash(i);
                        }}
                      >
                        <span className={`slash-icon ${group}`}>
                          {group === "skill" ? <Sparkles size={13} /> : <Slash size={13} />}
                        </span>
                        <span className="slash-name">
                          /{c.name}
                          {c.args && <span className="slash-args"> {c.args}</span>}
                        </span>
                        {desc && <span className="slash-desc">{desc}</span>}
                      </div>
                    </Fragment>
                  );
                })}
              </div>
            )}
            <StagedAttachments items={staged} onRemove={removeStaged} />
            {shellMode && (
              <div className="shell-mode-bar">
                <TerminalIcon size={13} />
                <span>{t("shell.mode", { agent: agentName })}</span>
              </div>
            )}
            {shellQueued > 0 && activeKey && (
              <div className="shell-chip">
                <TerminalIcon size={13} />
                <span>{t("shell.queued", { n: String(shellQueued) })}</span>
                <button className="btn ghost sm" disabled={streaming} onClick={() => void sendShellNow()}>
                  {t("shell.sendNow")}
                </button>
                <button className="btn ghost sm" onClick={() => chatStore.clearShell(agent, activeKey)}>
                  {t("shell.clear")}
                </button>
              </div>
            )}
            {queued.length > 0 && activeKey && (
              <div className="queue-chip">
                <Clock size={13} />
                <span>{t("queue.label", { n: String(queued.length) })}</span>
                <div className="queue-items">
                  {queued.map((q) => (
                    <QueuedChip
                      key={q.id}
                      q={q}
                      streaming={streaming}
                      t={t}
                      onSteer={(id) => chatStore.steerQueued(agent, activeKey, id)}
                      onEdit={(id) => {
                        void chatStore.editQueued(agent, activeKey, id).then(() => focusComposerEnd(taRef.current));
                      }}
                      onCancel={(id) => chatStore.cancelQueued(agent, activeKey, id)}
                    />
                  ))}
                </div>
                <button className="btn ghost sm" onClick={() => chatStore.clearQueue(agent, activeKey)}>
                  {t("queue.clear")}
                </button>
              </div>
            )}
            {voiceUsable && (voiceRecording || voice.state === "transcribing") && (
              <div className="dictation-bar">
                <span className="dot" />
                <span>{voiceRecording ? t("voice.listening") : t("voice.transcribing")}</span>
                {voiceRecording && <span className="hint">{t("voice.listeningHint")}</span>}
              </div>
            )}
            <div className={`composer-box${shellMode ? " shell" : ""}${voiceRecording ? " recording" : ""}`}>
              <input
                ref={fileRef}
                type="file"
                multiple
                hidden
                onChange={(e) => {
                  if (e.target.files?.length) void addFiles(e.target.files);
                  e.target.value = "";
                }}
              />
              <button
                className="composer-attach"
                onClick={() => fileRef.current?.click()}
                aria-label={t("att.attach")}
                title={t("att.attach")}
              >
                <Paperclip size={17} />
              </button>
              <div className="composer-ta-wrap">
                {interim && (
                  // Draws what the textarea is holding, with the not-yet-final
                  // words dimmed. The textarea's own text goes transparent (see
                  // .dictating) so this is what the user reads; both share the
                  // exact same typography, so the wrap is identical.
                  <div className="dictation-mirror" aria-hidden="true" ref={mirrorRef}>
                    <span>{input}</span>
                    <span className="interim">{input ? " " : ""}{interim}</span>
                  </div>
                )}
                <textarea
                  ref={taRef}
                  className={interim ? "dictating" : undefined}
                  rows={1}
                  placeholder={shellMode ? t("shell.placeholder") : t("chat.placeholder", { agent: agentName })}
                  value={interim ? `${input}${input ? " " : ""}${interim}` : input}
                  onChange={(e) => {
                    const v = e.target.value;
                    // While dictating, the interim tail is display-only — strip
                    // it back off so typed edits land in the draft alone.
                    const tail = interim ? `${input ? " " : ""}${interim}` : "";
                    setInput(tail && v.endsWith(tail) ? v.slice(0, v.length - tail.length) : v);
                    setSlashIndex(0);
                    // A real edit leaves history-recall mode; the next ArrowUp
                    // starts a fresh walk from the newest message.
                    setHistIdx(-1);
                  }}
                  onPaste={onPaste}
                  onKeyDown={onComposerKey}
                />
              </div>
              {voiceUsable && (
                <button
                  className={`composer-mic${voiceRecording ? " rec" : ""}`}
                  disabled={voice.state === "transcribing"}
                  aria-label={voiceRecording ? t("voice.stop") : t("voice.hold")}
                  aria-pressed={voiceRecording}
                  title={voiceRecording ? t("voice.stop") : t("voice.hold")}
                  onContextMenu={(e) => e.preventDefault()}
                  onPointerDown={(e) => {
                    // Keeps focus (and the caret) in the composer, and stops
                    // iOS turning the hold into a selection/callout.
                    e.preventDefault();
                    if (voice.state === "transcribing") return;
                    try {
                      // Keeps events coming to the button if the finger slides
                      // off mid-hold. A nicety — never let it take the gesture
                      // down with it (it throws on a pointer the browser no
                      // longer considers active).
                      e.currentTarget.setPointerCapture(e.pointerId);
                    } catch {
                      /* capture unavailable; the hold still works */
                    }
                    if (voiceRecording) {
                      // Pressing the mic while it's already listening (a
                      // toggled session) ends it; the release does nothing.
                      pressStartedRef.current = false;
                      voice.stop();
                      return;
                    }
                    pressStartedRef.current = true;
                    holdStartRef.current = Date.now();
                    voice.start("hold");
                  }}
                  onPointerUp={() => {
                    if (!pressStartedRef.current) return;
                    pressStartedRef.current = false;
                    // A quick tap means "toggle" — keep listening until the
                    // next press. An actual hold is walkie-talkie: ends here.
                    if (Date.now() - holdStartRef.current < TAP_THRESHOLD_MS) voice.setMode("toggle");
                    else voice.stop();
                  }}
                  onPointerCancel={() => {
                    pressStartedRef.current = false;
                    voice.cancel();
                  }}
                >
                  <MicIcon size={17} />
                </button>
              )}
              {turnLive ? (
                <>
                  {/* Queue + Steer are the two things a draft can do while a
                      reply streams. Enter queues on a keyboard, but a touch
                      Return is a newline and the Send button is hidden here,
                      so the pair is explicit; the tooltips say what each
                      means and the fallback. */}
                  {(input.trim() || staged.length > 0) && (
                    <>
                      <button
                        className="icon-btn"
                        onClick={() => void send()}
                        aria-label={t("compose.queue")}
                        title={t("compose.queueTitle")}
                      >
                        <SendIcon size={17} />
                      </button>
                      <button
                        className="icon-btn"
                        onClick={() => steer()}
                        aria-label={t("compose.steer")}
                        title={t("compose.steerTitle")}
                      >
                        <FastForward size={17} />
                      </button>
                    </>
                  )}
                  <button className="icon-btn danger" onClick={stop} aria-label={t("compose.stop")} title={t("compose.stop")}>
                    <Square size={15} fill="currentColor" />
                  </button>
                </>
              ) : (
                <button
                  className="icon-btn"
                  disabled={!input.trim() && staged.length === 0}
                  onClick={() => void send()}
                  aria-label={t("compose.send")}
                >
                  <SendIcon size={17} />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>

      {addBotOpen && !!agent && (
        <AddBotToChatDialog
          agent={agent}
          agents={agents}
          threadId={session?.threadId ?? ""}
          onClose={() => setAddBotOpen(false)}
          onCreated={(group) => {
            setAddBotOpen(false);
            navigate(`/groups/${group.id}`);
          }}
        />
      )}
    </>
  );
}

// Hook host for the threads drawer: mounted only while the drawer is open, so
// the shared dialog behavior (Escape, focus trap, focus restore) applies exactly
// for its lifetime. Mirrors Layout's nav drawer — hooks can't be conditional
// inside ChatView itself.
function ChatRailA11y({
  panelRef,
  onClose,
}: {
  panelRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  useDialogA11y(panelRef, onClose);
  return null;
}
