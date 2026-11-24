// The conversation, consumer-shaped.
//
// Same engine as the dashboard's chat (`chatStore`), a deliberately smaller
// surface: no model picker, no reasoning-effort picker, no token or context
// counters, no cost chips, no MCP indicator, no channel pills and no "!"
// shell lane. Tool activity follows the same three detail levels as the
// dashboard — the default here is `quiet` (a single friendly "working on it"
// line and nothing else), with `normal` and `verbose` one header-button cycle
// (or a /normal, /verbose command) away for anyone who wants to watch the
// work happen.
//
// What survives is everything a person actually uses: streaming with a typing
// indicator, stop, type-ahead queueing and steering, attachments (paperclip,
// paste, drag-and-drop), dictation, HITL approvals, the agent's checklist,
// retry after a dropped connection, and copy.

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, FastForward, Info, Menu, MessageSquarePlus, Play, RotateCcw, UserPlus, WifiOff } from "lucide-react";
import { api } from "../../core/api";
import { useI18n, useT } from "../../core/i18n";
import type { Agent } from "../../core/types";
import { CheckIcon, CopyIcon, PlusIcon } from "../../ui/icons";
import { Markdown } from "../../ui/Markdown";
import { channelLabel } from "../chat/channels";
import { focusComposerEnd } from "../chat/handoff";
import { useToast } from "../../ui/toast";
import { ApprovalCard, type ApprovalChoice } from "../chat/ApprovalCard";
import { AgentMedia, MessageAttachments } from "../chat/AttachmentViews";
import { TurnBlocks } from "../chat/TurnBlocksView";
import { ConnectCard } from "../chat/ConnectCard";
import { extractConnectLinks } from "../chat/connectLinks";
import { ConnectResultCard } from "../chat/ConnectResultCard";
import { ContextCard } from "../chat/ContextCard";
import { ErrorRemediation } from "../chat/ErrorRemediation";
import { TodoList } from "../chat/TodoList";
import { ingestFile, type Attachment } from "../chat/attachments";
import { modelContextWindow } from "../chat/models";
import { type ChatDetail, type ChatMode } from "../chat/prefs";
import {
  chatStore,
  type ContextCategory,
  type ContextReport,
  type Msg,
  type QueuedMsg,
  type SendOpts,
  type Session,
  type Sub,
  type ToolCall,
} from "../chat/sessionStore";
import { SLASH_COMMANDS, parseSlash } from "../chat/slash";
import { STR as CHAT_STR } from "../chat/strings";
import { BotMark } from "./BotMark";
import {
  CHAT_DETAIL_KEY,
  DetailButton,
  PeerThinking,
  pendingPeers,
  ToolTrail,
  useDetail,
} from "./detail";
import { absoluteTime, daySeparator, relativeTime } from "./format";
import { SimpleComposer } from "./SimpleComposer";
import { STR } from "./strings";
import { LangfuseLink } from "../observability/LangfuseLink";
import { ReplyFeedback } from "../chat/ReplyFeedback";
import { traceUrl, useLangfuseLinks } from "../observability/links";

// The user-facing slice of the dashboard's command registry. Everything else
// there is an operator tool (`/tools`, `/mcps`, the `/reload-*` family) or a
// setting this shell deliberately owns (`/quiet`, `/verbose`), so it is
// filtered out rather than shown and refused. Filtering the shared list
// — instead of retyping it — is what keeps a renamed command from silently
// disappearing here.
const SIMPLE_COMMANDS = new Set([
  "help",
  "new",
  "compact",
  "rename",
  "context",
  "auto",
  "plan",
  "manual",
  "act",
  "quiet",
  "normal",
  "verbose",
]);

// Auto-scroll counts the reader as "at the bottom" within this distance, so
// momentum overshoot and sub-pixel rounding don't release the follow.
const PIN_THRESHOLD_PX = 80;

// Stable empty fallbacks: reads against a not-yet-created session must not
// churn object identity on every render.
const EMPTY_MSGS: Msg[] = [];
const EMPTY_ATT: Attachment[] = [];
const EMPTY_QUEUE: QueuedMsg[] = [];
const EMPTY_TOOLS: ToolCall[] = [];
const EMPTY_SUBS: Sub[] = [];

// A gap this long between two messages gets a centred stamp between them. Short
// enough that a conversation picked up after lunch is visibly a new block, long
// enough that a back-and-forth is not chopped into slices.
const TIME_BLOCK_MS = 30 * 60 * 1000;

// Copy a reply to the clipboard, confirming in place for a beat. `onFail`
// surfaces the reason (insecure origin, denied permission) as a toast.
function CopyButton({
  text,
  label,
  copiedLabel,
  onFail,
}: {
  text: string;
  label: string;
  copiedLabel: string;
  onFail: () => void;
}) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      onFail();
    }
  }
  return (
    <button
      type="button"
      className={`msg-copy${copied ? " copied" : ""}`}
      onClick={() => void copy()}
      title={label}
      aria-label={label}
    >
      {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
      {copied ? copiedLabel : label}
    </button>
  );
}

export function SimpleChatView({
  agent,
  bot,
  agentsByName,
  session,
  composerFocusKey,
  arriving = false,
  waking = false,
  isMobile,
  profileOpen,
  hasBots,
  loadingBots,
  canCreateBots,
  onOpenRail,
  onToggleProfile,
  onNewChat,
  onNewBot,
  onAddBot,
  onThreadsChanged,
}: {
  agent: string;
  bot: Agent | undefined;
  /** The whole roster, for naming and drawing a peer this bot is waiting on. */
  agentsByName: Map<string, Agent>;
  session: Session | undefined;
  /**
   * Bumped by SimpleApp after it preloads a hand-off draft into the composer;
   * the textarea is here, so this is how the caret gets parked after "User: ".
   */
  composerFocusKey?: number;
  /**
   * SimpleApp is still choosing which conversation to open for this bot (its
   * newest, see threadPick.ts). Draws a spinner where the "say something"
   * hint would otherwise flash for a moment before last week's chat appears.
   */
  arriving?: boolean;
  /** The runtime is writing this thread's first rows (the onboarding greeting). */
  waking?: boolean;
  isMobile: boolean;
  profileOpen: boolean;
  hasBots: boolean;
  loadingBots: boolean;
  /** Whether this human may create a bot — drives the zero-bots call to action. */
  canCreateBots: boolean;
  onOpenRail: () => void;
  onToggleProfile: () => void;
  onNewChat: () => void;
  /** Opens the New-bot dialog SimpleApp owns. */
  onNewBot: () => void;
  /**
   * Opens the "add a bot to this chat" dialog SimpleApp owns. Undefined when
   * the action cannot apply — group chats off, or no second bot to add — and
   * the header then draws no button rather than one that opens a dialog whose
   * only outcome is a refusal.
   */
  onAddBot?: () => void;
  onThreadsChanged: () => void;
}) {
  const t = useT(STR);
  // Admin-only "Open in Langfuse" targets for this agent (null otherwise).
  const langfuseLinks = useLangfuseLinks(agent);
  // The /context fallback line reuses the dashboard chat's own wording, so the
  // two shells describe the same numbers with the same words.
  const tc = useT(CHAT_STR);
  const toast = useToast();
  // The reader's language drives every clock and date in the transcript.
  const { lang } = useI18n();

  const botName = bot?.name || agent || t("chat.botFallback");
  const messages = session?.messages ?? EMPTY_MSGS;
  const streaming = session?.streaming ?? false;
  const input = session?.input ?? "";
  const staged = session?.staged ?? EMPTY_ATT;
  const queued = session?.queue ?? EMPTY_QUEUE;
  const activeKey = session?.key;

  // How the agent treats tool calls this turn: auto (just do it), plan (draft
  // first), manual/act (ask about everything / approve everything). There is no
  // picker in this shell — the four slash commands are the only way in, and the
  // choice is per bot and per visit rather than persisted, so a mode set for a
  // one-off question can never quietly govern next week's conversation.
  const [mode, setMode] = useState<ChatMode>("auto");
  useEffect(() => setMode("auto"), [agent]);

  // The header button cycles it; /quiet /normal /verbose set it directly.
  // Quiet by default here: you asked one bot a question and you want its
  // answer. The group thread defaults to `normal` for the opposite reason —
  // see views/simple/detail.tsx.
  const [detail, changeDetail, cycleDetail] = useDetail(CHAT_DETAIL_KEY, "quiet");

  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const logRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (composerFocusKey) focusComposerEnd(taRef.current);
  }, [composerFocusKey]);

  // A coarse clock so every "time ago" label ages in place. Minute granularity
  // is all the labels resolve.
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  // ── pin-aware auto-scroll ────────────────────────────────────────────────
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

  // Switching bot or conversation always lands at the newest message.
  useEffect(() => {
    scrollToLatest();
  }, [agent, activeKey, scrollToLatest]);

  // New content follows only while the reader is pinned to the bottom. Keyed on
  // the per-session revision the store bumps on every change.
  useEffect(() => {
    if (pinnedRef.current) scrollToLatest();
  }, [session?.rev, scrollToLatest]);

  // The user's own turns, oldest → newest: the corpus ArrowUp recalls in the
  // composer. `messages` is mutated in place by the store, so the memo is keyed
  // on the conversation plus its length — an array-identity key would never
  // recompute and a just-sent message would be unreachable.
  const history = useMemo(
    () => messages.filter((m) => m.role === "user" && m.body).map((m) => m.body),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeKey, messages.length],
  );

  const commands = useMemo(() => SLASH_COMMANDS.filter((c) => SIMPLE_COMMANDS.has(c.name)), []);

  // ── store-bound writes (always target the active conversation) ────────────
  const setInput = useCallback(
    (value: string) => {
      if (!agent) return;
      const s = chatStore.ensureActive(agent);
      chatStore.setInput(agent, s.key, value);
    },
    [agent],
  );

  const setStaged = useCallback(
    (next: Attachment[] | ((prev: Attachment[]) => Attachment[])) => {
      if (!agent) return;
      const s = chatStore.ensureActive(agent);
      chatStore.setStaged(agent, s.key, next);
    },
    [agent],
  );

  const addFiles = useCallback(
    async (files: FileList | File[]) => {
      for (const file of Array.from(files)) {
        const res = await ingestFile(file);
        if (res.ok) setStaged((prev) => [...prev, res.attachment]);
        else
          toast.push(
            res.reason === "too_large"
              ? t("att.tooLarge", { name: file.name })
              : t("att.unreadable", { name: file.name }),
            "error",
          );
      }
    },
    [setStaged, toast, t],
  );

  function removeStaged(id: string) {
    setStaged((prev) => prev.filter((a) => a.id !== id));
  }

  // ── drag & drop onto the conversation ────────────────────────────────────
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

  // ── sending ──────────────────────────────────────────────────────────────
  // The opts a queued message inherits when the store drains it (the queued
  // message carries its own text / attachments / mode).
  function drainOpts(): SendOpts {
    return { text: "", attachments: [], mode, errorLabel: t("error.label") };
  }

  async function runSend(text: string, attachments: Attachment[], modeOverride?: ChatMode) {
    if ((!text && attachments.length === 0) || !agent) return;
    // Sending re-pins the log: even after scrolling back, what the reader wants
    // next is their own message and the reply.
    scrollToLatest();
    const s = chatStore.ensureActive(agent);
    await chatStore.send(agent, s.key, {
      text,
      attachments,
      mode: modeOverride ?? mode,
      errorLabel: t("error.label"),
    });
  }

  function submit() {
    if (!agent) return;
    const text = input.trim();
    // A slash command is handled locally and never reaches the agent.
    if (text.startsWith("/")) {
      const parsed = parseSlash(text);
      setInput("");
      if (parsed) dispatchSlash(parsed.head, parsed.arg);
      return;
    }
    if (!text && staged.length === 0) return;
    // The bot is still working: hold the message instead of dropping it. It
    // sends itself when the current reply finishes — or the chip's Steer hands
    // it to the reply in progress.
    if (session?.streaming) {
      const s = chatStore.ensureActive(agent);
      chatStore.enqueue(agent, s.key, { text, attachments: staged, mode }, drainOpts());
      setInput("");
      setStaged([]);
      scrollToLatest();
      return;
    }
    const attachments = staged;
    setInput("");
    setStaged([]);
    void runSend(text, attachments);
  }

  // Steer = hand the draft to the reply in progress: the bot reads it at its
  // next step without stopping. If the reply finishes first it is sent as the
  // next message (the store falls back to queueing).
  function steer() {
    if (!agent) return;
    const text = input.trim();
    if (!text && staged.length === 0) return;
    const s = chatStore.ensureActive(agent);
    chatStore.steer(agent, s.key, { text, attachments: staged, mode }, drainOpts());
    setInput("");
    setStaged([]);
    scrollToLatest();
  }

  function stop() {
    if (session) chatStore.stop(agent, session.key);
  }

  function retryTurn() {
    if (session) chatStore.retryLast(agent, session.key);
  }

  function resolveApproval(msgId: number, idx: number, choice: ApprovalChoice) {
    if (session) chatStore.resolveApproval(agent, session.key, msgId, idx, choice);
  }

  // A plan the agent drafted is waiting on the human: approve it, switch to
  // Act, and tell it to go. Refining instead is just typing another message.
  function approvePlan() {
    setMode("act");
    void runSend(t("plan.approveMsg"), [], "act");
  }

  // ── slash commands ───────────────────────────────────────────────────────
  function pushSystem(body: string) {
    if (!agent) return;
    const s = chatStore.ensureActive(agent);
    chatStore.pushSystem(agent, s.key, body);
  }

  function helpText(): string {
    const rows = commands
      .map((c) => `- \`/${c.name}${c.args ? " " + c.args : ""}\` — ${c.descKey ? t(c.descKey) : c.desc || ""}`)
      .join("\n");
    return `**${t("slash.heading")}**\n\n${rows}`;
  }

  async function compactChat() {
    const tid = session?.threadId;
    if (!tid) {
      toast.push(t("compact.noThread"), "error");
      return;
    }
    toast.push(t("compact.running"));
    try {
      const res = await api.post<{ ok: boolean; compacted: boolean }>(
        `/api/agents/${encodeURIComponent(agent)}/threads/${encodeURIComponent(tid)}/compact`,
        {},
      );
      if (res.compacted) {
        await chatStore.refreshThread(agent, tid);
        onThreadsChanged();
        toast.push(t("compact.done"));
      } else {
        toast.push(t("compact.empty"));
      }
    } catch {
      toast.push(t("compact.failed"), "error");
    }
  }

  // /context — the same usage card the dashboard chat shows: the agent's
  // static context breakdown from the server, merged with this conversation's
  // live window fill. Falls back to a plain-markdown line when the endpoint is
  // unavailable (older runtime, network error).
  async function showContext() {
    if (!agent) return;
    try {
      const res = await api.get<{ model?: string; categories?: ContextCategory[] }>(
        `/api/agents/${encodeURIComponent(agent)}/context`,
      );
      const report: ContextReport = {
        model: res.model || session?.streamModel || "",
        categories: res.categories ?? [],
        usedContext: session?.usage?.context ?? null,
      };
      const s = chatStore.ensureActive(agent);
      chatStore.pushContextCard(agent, s.key, report);
    } catch {
      pushSystem(contextFallbackText());
    }
  }

  function contextFallbackText(): string {
    const model = session?.streamModel || "";
    const ctx = modelContextWindow(model);
    const fill = session?.usage?.context ?? 0;
    const lines = [`**${tc("context.heading")}**`, "", `- ${tc("context.model")}: \`${model || "—"}\``];
    if (ctx && fill) {
      const pct = ((fill / ctx) * 100).toFixed(1);
      lines.push(`- ${tc("context.window")}: ${(fill / 1000).toFixed(1)}k / ${(ctx / 1000).toFixed(0)}k (${pct}%)`);
    } else if (fill) {
      lines.push(`- ${tc("context.window")}: ${fill.toLocaleString()} tokens`);
    }
    return lines.join("\n");
  }

  async function renameChat(name: string) {
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
      await api.patch(`/api/agents/${encodeURIComponent(agent)}/threads/${encodeURIComponent(tid)}`, { name });
      onThreadsChanged();
      toast.push(t("rename.done", { name }));
    } catch {
      toast.push(t("rename.failed"), "error");
    }
  }

  function dispatchSlash(head: string, arg: string) {
    switch (head) {
      case "help":
        pushSystem(helpText());
        break;
      case "new":
        onNewChat();
        break;
      case "compact":
        void compactChat();
        break;
      case "rename":
        void renameChat(arg);
        break;
      case "context":
        void showContext();
        break;
      case "auto":
      case "plan":
      case "manual":
      case "act":
        setMode(head);
        toast.push(t("mode.set", { mode: t(`mode.${head}`) }));
        break;
      case "quiet":
      case "normal":
      case "verbose":
        changeDetail(head);
        break;
      default:
        toast.push(t("slash.unknown", { name: head }), "error");
    }
  }

  // ── empty shells ─────────────────────────────────────────────────────────
  if (!agent) {
    return (
      <main className="simple-chat" aria-label={t("chat.region")} data-testid="simple-chat">
        <SimpleHeader
          botName={loadingBots ? t("chat.loading") : t("chat.noBotTitle")}
          bot={undefined}
          isMobile={isMobile}
          profileOpen={profileOpen}
          canAct={false}
          onOpenRail={onOpenRail}
          onToggleProfile={onToggleProfile}
          onNewChat={onNewChat}
          t={t}
        />
        <div className="simple-blank">
          {loadingBots ? (
            <div className="spinner" />
          ) : (
            <>
              <p className="simple-blank-title">{hasBots ? t("chat.pickBot") : t("chat.noBots")}</p>
              <p className="simple-blank-sub muted">
                {hasBots
                  ? t("chat.pickBotHint")
                  : canCreateBots
                    ? t("chat.noBotsHintCreate")
                    : t("chat.noBotsHint")}
              </p>
              {/* The blank slate is where "why is there no create button?"
                  gets asked, so when this human CAN create, the answer stands
                  right here — not only in a rail that may be a closed drawer
                  on a phone. */}
              {!hasBots && canCreateBots && (
                <button type="button" className="btn primary" onClick={onNewBot}>
                  <PlusIcon size={15} /> {t("chat.noBotsCta")}
                </button>
              )}
            </>
          )}
        </div>
      </main>
    );
  }

  return (
    <main
      className="simple-chat"
      aria-label={t("chat.region")}
      data-testid="simple-chat"
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <SimpleHeader
        botName={botName}
        bot={bot}
        isMobile={isMobile}
        profileOpen={profileOpen}
        canAct
        detail={detail}
        onCycleDetail={cycleDetail}
        onOpenRail={onOpenRail}
        onToggleProfile={onToggleProfile}
        onNewChat={onNewChat}
        onAddBot={onAddBot}
        t={t}
      />

      {dragging && (
        <div className="chat-drop">
          <Info size={18} /> {t("att.drop")}
        </div>
      )}

      <div className="chat-log simple-log" ref={logRef} onScroll={updatePinned}>
        {messages.length === 0 && arriving && !session && (
          <div className="simple-blank" aria-busy="true">
            <div className="spinner" />
          </div>
        )}
        {messages.length === 0 && !(arriving && !session) && waking && (
          <div className="simple-blank" role="status" aria-busy="true">
            {/* A block element: text-align on the parent does not centre it. */}
            <div className="spinner" style={{ margin: "0 auto 10px" }} />
            <p className="simple-blank-title">{t("chat.waking", { agent: botName })}</p>
            <p className="simple-blank-sub muted">{t("chat.wakingHint")}</p>
          </div>
        )}
        {messages.length === 0 && !(arriving && !session) && !waking && (
          <div className="simple-blank">
            <p className="simple-blank-title">{t("chat.empty", { agent: botName })}</p>
            <p className="simple-blank-sub muted">{t("chat.emptyHint")}</p>
          </div>
        )}

        {messages.map((m, i) => {
          // The same three detail gates as the dashboard (ChatView): Quiet
          // renders no tool activity at all — this shell's default — Normal a
          // compact line per call, Verbose full cards plus subagent
          // transcripts.
          const showTools = detail !== "quiet";
          const showSubs = detail === "verbose";
          if (m.role === "system") {
            // An event, not a turn: centred and muted, the way a messenger
            // narrates "Renamed to Sales Outbound" between two bubbles. A
            // /context report renders as the same rich card the dashboard
            // shows (no command links here — /tools and friends are operator
            // commands this shell does not expose).
            return (
              <div className="chat-system simple-system" key={m.id}>
                <div className="md">
                  {m.connectResult ? (
                    <ConnectResultCard result={m.connectResult} />
                  ) : m.context ? (
                    <ContextCard report={m.context} />
                  ) : (
                    <Markdown text={m.body} />
                  )}
                </div>
              </div>
            );
          }
          const isLast = i === messages.length - 1;
          // Time blocks and speaker grouping both look backwards at the last
          // message that actually rendered — a skipped tool-only turn must not
          // break a run of replies into two avatars. `messages[i - 1]` was
          // subtly wrong here: it could BE the skipped tool-only turn, so the
          // reply after it grouped against something invisible and rendered
          // with no avatar after a user message. Walk back to the previous
          // message with something to show instead.
          let prev = messages[i - 1];
          for (let back = i - 1; back >= 0 && prev && prev.role === "assistant"
               && !prev.body && !prev.media?.length && !prev.todos?.length
               && !prev.approvals?.length
               && !(showTools && prev.tools.some((c) => c.name))
               && !(showSubs && prev.subs.length > 0); back -= 1) {
            prev = messages[back - 1];
          }
          const gap = !prev || !m.ts || !prev.ts || m.ts - prev.ts > TIME_BLOCK_MS;
          const separator = m.ts && (i === 0 || gap) ? daySeparator(m.ts, nowTick, lang, t) : "";
          // (a system row can never match here — the branch above returned.)
          const grouped = !!prev && prev.role === m.role && !gap;
          const streamingThis = streaming && isLast && m.role === "assistant";
          // Sign-in links from tool results (MCP OAuth, magic links) render as
          // Connect cards at every detail level — on Quiet they are the one
          // thing a connect turn must still show.
          const connects = m.role === "assistant" ? extractConnectLinks(m.tools) : [];
          // An assistant turn with nothing to show AT THIS DETAIL LEVEL is
          // skipped rather than left as a blank bubble — on Quiet that
          // includes every tool-only turn, which is why the gates above feed
          // this check.
          const hasVisible =
            !!m.body ||
            !!m.media?.length ||
            !!m.todos?.length ||
            !!m.approvals?.length ||
            connects.length > 0 ||
            (showTools && m.tools.some((c) => c.name)) ||
            (showSubs && m.subs.length > 0);
          if (m.role === "assistant" && !streamingThis && !hasVisible) return null;
          // While a reply is still forming with no words yet, the agent is
          // running tools. On Quiet the reader gets one friendly line and
          // nothing more; on Normal/Verbose it sits under the live tool rows.
          const working = streamingThis && !m.body;
          // Who this bot has handed the question to and is now blocked on. Only
          // while the turn is live: a settled turn whose stream was cut still
          // carries a resultless call, and "still thinking" about a bot that
          // stopped thinking minutes ago is a lie the reader cannot check.
          const peers = streamingThis ? pendingPeers(m.tools) : [];

          const mine = m.role === "user";
          return (
            <Fragment key={m.id}>
              {!!separator && (
                <div className="simple-daysep">
                  <span>{separator}</span>
                </div>
              )}
              <div
                className={`msg simple-msg ${mine ? "is-me" : "is-bot"}${grouped ? " is-grouped" : ""}`}
              >
                {/* The bot's face anchors the left column; a run of replies
                    shows it once, so the column stays a column. The human needs
                    none — their side of the conversation is the identity. */}
                {!mine && (
                  <span className="simple-msg-face">
                    {!grouped && (
                      <BotMark name={botName} avatarUrl={bot?.avatar_url} emoji={bot?.emoji} size={30} />
                    )}
                  </span>
                )}

                <div className="msg-body simple-msg-body">
                  {/* Who is speaking is carried by the side and the face, so
                      the name is for screen readers only — a visible "You"
                      over every one of your own bubbles is noise. */}
                  <div className="msg-name simple-sr">{mine ? t("chat.you") : botName}</div>
                  {/* A message the assistant read mid-reply (steered in), not
                      one that started a turn — the only tag the shell shows
                      on a person's own bubble. */}
                  {mine && m.steer && (
                    <span className="steer-pill simple-steer-pill" title={t("msg.steeredTitle")}>
                      <FastForward size={10} />
                      {t("msg.steered")}
                    </span>
                  )}

                  {/* The turn in the order it happened: the words written
                      before the first tool ran, the tools, then the words that
                      followed. Same block builder and file index the dashboard
                      chat uses — only the tool markup is this shell's own,
                      which is why `renderTools` is a prop. Tool activity at
                      Normal is one compact labelled line per named call, at
                      Verbose the full call and its result as cards; Quiet draws
                      none of it and keeps the friendly "working on it" line
                      below as the only trace. */}
                  {!mine && (
                    <TurnBlocks
                      tools={m.tools}
                      segments={m.segments}
                      body={m.body}
                      media={m.media}
                      renderTools={(tools) => (
                        <ToolTrail tools={tools} subs={EMPTY_SUBS} detail={detail} />
                      )}
                    />
                  )}

                  {/* Subagent transcripts belong to the reply as a whole, so
                      they are drawn once, after it, rather than inside one of
                      the tool runs above (the dashboard chat places them the
                      same way). Verbose only. */}
                  {!mine && <ToolTrail tools={EMPTY_TOOLS} subs={m.subs} detail={detail} />}

                  {m.todos && m.todos.length > 0 && (
                    // The plan card: what the bot said it would do, with how
                    // far along it is on the right. On Quiet this is the only
                    // "computer result" card the shell shows.
                    <div className="simple-card">
                      <div className="simple-card-head">
                        <span className="simple-card-title">{t("card.plan")}</span>
                        <span
                          className={`simple-status${
                            m.todos.every((it) => it.status === "completed") ? " is-done" : ""
                          }`}
                        >
                          {m.todos.every((it) => it.status === "completed")
                            ? t("card.planDone")
                            : t("card.planWorking")}
                        </span>
                      </div>
                      <TodoList items={m.todos} live={streamingThis} />
                    </div>
                  )}

                  {m.approvals?.map((ap, idx) => (
                    <ApprovalCard key={idx} state={ap} onDecide={(c) => resolveApproval(m.id, idx, c)} />
                  ))}

                  {mine ? (
                    <>
                      {m.attachments && <MessageAttachments items={m.attachments} />}
                      {/* A reopened conversation brings uploads back as server
                          media refs rather than the live turn's local previews. */}
                      {m.media && <AgentMedia items={m.media} />}
                      {m.body && <div className="bubble user">{m.body}</div>}
                      {m.body && (
                        <div className="msg-actions">
                          <CopyButton
                            text={m.body}
                            label={t("msg.copy")}
                            copiedLabel={t("msg.copied")}
                            onFail={() => toast.push(t("msg.copyFailed"), "error")}
                          />
                          {!!m.ts && (
                            <time className="msg-time" dateTime={new Date(m.ts).toISOString()} title={absoluteTime(m.ts, lang)}>
                              {relativeTime(m.ts, nowTick, lang, t)}
                            </time>
                          )}
                        </div>
                      )}
                    </>
                  ) : (
                    <>
                      {connects.map((link) => (
                        <ConnectCard key={link.href} link={link} />
                      ))}
                      {m.media && <AgentMedia items={m.media} />}

                      {/* Naming the bot being waited on beats "working on
                          it": it is the same statement with the one fact the
                          reader wants in it, so it REPLACES the generic line
                          rather than stacking a second pill under it. It also
                          shows once words have started arriving, because a
                          peer call suspends this bot mid-reply. */}
                      {peers.length > 0 && (
                        <PeerThinking tools={m.tools} asker={botName} agentsByName={agentsByName} />
                      )}
                      {working && peers.length === 0 && (
                        <div className="simple-working" role="status">
                          <span className="typing" aria-hidden>
                            <span />
                            <span />
                            <span />
                          </span>
                          <span>{m.todos?.length || m.tools.length ? t("chat.working") : t("chat.thinking")}</span>
                        </div>
                      )}
                      {streamingThis && !working && peers.length === 0 && (
                        <div className="typing" aria-label={t("chat.thinking")}>
                          <span />
                          <span />
                          <span />
                        </div>
                      )}

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
                          {!!m.ts && (
                            <time className="msg-time" dateTime={new Date(m.ts).toISOString()} title={absoluteTime(m.ts, lang)}>
                              {relativeTime(m.ts, nowTick, lang, t)}
                            </time>
                          )}
                        </div>
                      )}

                      {/* The connection dropped mid-reply (a locked phone, a
                          network blip). The partial stays; this narrates the
                          recovery and offers to send the message again when the
                          server had nothing more to hand back. */}
                      {/* The connection dropped but the agent kept working —
                          this tab is catching up on what it missed. Not a
                          failure, so no warning badge and no resend. */}
                      {isLast && !streaming && session?.attaching && (
                        <div className="simple-notice">
                          <span className="badge">
                            <span className="spinner" style={{ width: 12, height: 12 }} />
                            {t("reattach.chip")}
                          </span>
                          <span className="muted simple-notice-text">
                            {t("reattach.line", { agent: botName })}
                          </span>
                        </div>
                      )}
                      {isLast && !streaming && !session?.attaching && session?.interrupted && (
                        <div className="simple-notice">
                          <span className="badge simple-warn">
                            <WifiOff size={12} /> {t("interrupted.chip")}
                          </span>
                          {session.reconciling ? (
                            <span className="muted simple-notice-text">
                              <span className="spinner" style={{ width: 12, height: 12 }} />
                              {t("interrupted.checking")}
                            </span>
                          ) : (
                            <>
                              <span className="muted simple-notice-text">
                                {t("interrupted.line", { agent: botName })}
                              </span>
                              <button className="btn simple-notice-btn" type="button" onClick={retryTurn}>
                                <RotateCcw size={12} /> {t("interrupted.retry")}
                              </button>
                            </>
                          )}
                        </div>
                      )}
                      {isLast && !streaming && !session?.interrupted && !!session?.recoveredAt && (
                        <div className="simple-recovered">
                          <CheckIcon size={12} /> {t("interrupted.recovered")}
                        </div>
                      )}
                      {/* The request failed before the stream opened — the error
                          text is in the bubble above; offer the resend rather
                          than making the reader retype. */}
                      {isLast && !streaming && !session?.interrupted && !!session?.error && (
                        <div className="simple-notice">
                          <button className="btn simple-notice-btn" type="button" onClick={retryTurn}>
                            <RotateCcw size={12} /> {t("interrupted.retry")}
                          </button>
                        </div>
                      )}
                      {m.planProposal && isLast && !streaming && (
                        <div className="simple-notice">
                          <button className="btn primary simple-notice-btn" type="button" onClick={approvePlan}>
                            <Play size={13} /> {t("plan.approveRun")}
                          </button>
                        </div>
                      )}
                      {m.remediation && (
                        <ErrorRemediation remediation={m.remediation} onNewThread={onNewChat} />
                      )}
                    </>
                  )}
                </div>
              </div>
            </Fragment>
          );
        })}
        {/* A turn running on this thread from ANOTHER surface — Telegram, a
            scheduled job, the connector, a second device. The runtime streams
            the reply-so-far in throttled previews; the persisted rows land
            above through the same fold a reload uses, and `idle` clears this. */}
        {!streaming && session?.activity && (
          <div className="msg simple-msg is-bot" key="remote-activity">
            <span className="simple-msg-face">
              <BotMark name={botName} avatarUrl={bot?.avatar_url} emoji={bot?.emoji} size={30} />
            </span>
            <div className="msg-body simple-msg-body">
              <div className="msg-name simple-sr">{botName}</div>
              {session.activity.text && (
                <div className="bubble assistant">
                  <Markdown text={session.activity.text} />
                </div>
              )}
              <div className="simple-working" role="status">
                <span className="typing" aria-hidden>
                  <span />
                  <span />
                  <span />
                </span>
                <span>
                  {session.activity.channel
                    ? t("chat.remote.working", { channel: channelLabel(session.activity.channel, t) })
                    : t("chat.remote.workingUnknown")}
                </span>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Back to the live end of the conversation once the follow is released. */}
      {!pinned && (
        <div className="chat-jump-wrap">
          <button className="chat-jump" type="button" onClick={() => scrollToLatest("smooth")}>
            <ArrowDown size={13} /> {t("chat.jumpLatest")}
          </button>
        </div>
      )}

      <SimpleComposer
        botName={botName}
        input={input}
        staged={staged}
        queued={queued}
        streaming={streaming}
        commands={commands}
        history={history}
        textareaRef={taRef}
        onInput={setInput}
        onAddFiles={(files) => void addFiles(files)}
        onRemoveStaged={removeStaged}
        onSteerQueued={(id) => activeKey && chatStore.steerQueued(agent, activeKey, id)}
        onEditQueued={(id) => {
          if (!activeKey) return;
          void chatStore.editQueued(agent, activeKey, id).then(() => focusComposerEnd(taRef.current));
        }}
        onCancelQueued={(id) => activeKey && chatStore.cancelQueued(agent, activeKey, id)}
        onClearQueue={() => activeKey && chatStore.clearQueue(agent, activeKey)}
        onSubmit={submit}
        onSteer={steer}
        onStop={stop}
        onSlash={dispatchSlash}
      />
    </main>
  );
}

// The conversation header: who you are talking to, plus the two things this
// shell can do to the layout (open the bot drawer on a phone, show or hide the
// profile) and a new conversation with the same bot.
function SimpleHeader({
  botName,
  bot,
  isMobile,
  profileOpen,
  canAct,
  detail,
  onCycleDetail,
  onOpenRail,
  onToggleProfile,
  onNewChat,
  onAddBot,
  t,
}: {
  botName: string;
  bot: Agent | undefined;
  isMobile: boolean;
  profileOpen: boolean;
  canAct: boolean;
  /** Current tool-detail level; the header button cycles it. Omitted on the
   * no-bot empty shell, where there is nothing the level could change. */
  detail?: ChatDetail;
  onCycleDetail?: () => void;
  onOpenRail: () => void;
  onToggleProfile: () => void;
  onNewChat: () => void;
  /** Turns this conversation into a group. Absent when that cannot apply. */
  onAddBot?: () => void;
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  const online = bot?.running === true && bot?.paused !== true;
  return (
    <header className="simple-head">
      {isMobile && (
        <button
          type="button"
          className="simple-head-btn"
          onClick={onOpenRail}
          aria-label={t("chat.openBots")}
          title={t("chat.openBots")}
        >
          <Menu size={18} strokeWidth={1.75} />
        </button>
      )}
      <BotMark
        name={botName}
        avatarUrl={bot?.avatar_url}
        emoji={bot?.emoji}
        size={36}
        online={bot ? online : undefined}
      />
      <div className="simple-head-text">
        <strong className="simple-head-name">{botName}</strong>
        {!!bot && (
          <span className="simple-head-sub muted">
            {[(bot.title || "").trim(), online ? t("rail.online") : t("rail.offline")]
              .filter(Boolean)
              .join(" · ")}
          </span>
        )}
      </div>
      {canAct && (
        <button
          type="button"
          className="simple-head-btn"
          onClick={onNewChat}
          aria-label={t("chat.newChat")}
          title={t("chat.newChat")}
        >
          <MessageSquarePlus size={16} strokeWidth={1.75} />
          {/* The label is dropped on a phone, where the bot's name has first
              claim on the header width; the icon and aria-label carry it. */}
          <span className="simple-head-new-label">{t("chat.newChat")}</span>
        </button>
      )}
      {canAct && !!onAddBot && (
        // Turns this 1:1 into a group carrying its history. Sits next to "New
        // chat" because it is the same kind of move — where the conversation
        // goes next — and it is the answer to "get someone else in on this",
        // which people otherwise solve by re-explaining to a second bot.
        <button
          type="button"
          className="simple-head-btn"
          onClick={onAddBot}
          aria-label={t("chat.addBot")}
          title={t("chat.addBotHelp")}
        >
          <UserPlus size={16} strokeWidth={1.75} />
          <span className="simple-head-new-label">{t("chat.addBot")}</span>
        </button>
      )}
      {!!detail && !!onCycleDetail && <DetailButton detail={detail} onCycle={onCycleDetail} />}
      <button
        type="button"
        className="simple-head-btn"
        onClick={onToggleProfile}
        aria-expanded={profileOpen}
        aria-label={profileOpen ? t("chat.hideProfile") : t("chat.showProfile")}
        title={profileOpen ? t("chat.hideProfile") : t("chat.showProfile")}
      >
        <Info size={17} strokeWidth={1.75} />
      </button>
    </header>
  );
}
