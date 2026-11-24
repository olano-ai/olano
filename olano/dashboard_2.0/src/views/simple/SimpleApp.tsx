// Simple mode — the consumer chat shell.
//
// For a human whose `ui_mode` is "simple", app.tsx mounts this INSTEAD of the
// dashboard: no Layout, no 25-route table, no operator nav. What is left is the
// shape of a messaging app — a rail of bots (contacts, not threads), the
// conversation, and the bot's profile — so someone who has never seen an agent
// runtime can still talk to one.
//
// This shell is a DISPLAY choice and nothing else. Every endpoint it calls is
// the one the dashboard calls, behind the same server-side guards; hiding the
// model picker does not grant or remove a permission. The chat engine is the
// same module-scope store the dashboard's ChatView drives
// (`../chat/sessionStore`), so a turn started here keeps streaming while the
// user switches bots, closes the drawer, or navigates away — and the same
// conversation is waiting in advanced mode.
//
// Layout ownership lives here: the shell owns the bot roster, the thread list
// for the selected bot, the mobile drawer/overlay state and the ?thread= deep
// link; the children are presentation plus their own local concerns.

import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { PanelLeftOpen } from "lucide-react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { simpleShellFallback } from "../../core/routeUrls";
import { RouteCanonical } from "../../core/RouteCanonical";
import { useAuth } from "../../core/auth";
import { useCapabilities, useCapability } from "../../core/capabilities";
import { useCurrentAgent } from "../../core/currentAgent";
import { useI18n, useT } from "../../core/i18n";
import { RouteErrorBoundary } from "../../ui/RouteErrorBoundary";
import { PwaBanners } from "../pwa/PwaBanners";
import { useMediaQuery } from "../../core/useMediaQuery";
import { threadListStore, useThreadList } from "../chat/threadListStore";
import { useConfirm } from "../../ui/confirm";
import { useDialogA11y } from "../../ui/dialogA11y";
import { useToast } from "../../ui/toast";
import { shouldPreloadDraft, takeChatHandoff, type ChatHandoff } from "../chat/handoff";
import { chatStore, type Session } from "../chat/sessionStore";
import { BotProfilePanel } from "./BotProfilePanel";
import {
  BotRail,
  readRailCollapsed,
  writeRailCollapsed,
  type BotBadge,
  type ChatRow,
  type RailPreview,
} from "./BotRail";
import { ChatsPanel } from "./ChatsPanel";
import { previewText } from "./format";
import {
  readThreadFilter,
  writeThreadFilter,
  type ThreadFilterId,
} from "../chat/threadFilters";
import { GroupInfoPanel } from "./GroupInfoPanel";
import { GroupThreadView } from "./GroupThreadView";
import {
  groupErrorText,
  renameGroup,
  setOpenGroup,
  useGroups,
  useGroupsEnabled,
  type GroupMessage,
  type PendingMessage,
} from "./groupsStore";
import { AddBotToChatDialog } from "./AddBotToChatDialog";
import { NewBotDialog } from "./NewBotDialog";
import { NewGroupDialog } from "./NewGroupDialog";
import { SIMPLE_PANELS, panelForPath, type SimplePanel } from "./panels";
import { SimpleChatView } from "./SimpleChatView";
import { STR } from "./strings";
import "../../styles/simple.css";

// Simple mode stacks one breakpoint below the dashboard's 900px: there is no
// nav column to hide here, only a rail and a profile panel, and both still fit
// beside a conversation on a tablet. Exported so every file in this shell asks
// the same question (and so a future test can import it rather than retype it).
export const SIMPLE_MOBILE_QUERY = "(max-width: 768px)";

// Whether the profile panel is open on a desktop-width viewport. Persisted
// per browser (like the dashboard's sidebar-collapsed flag), because it is a
// layout preference, not something that should follow the human across devices.
const PROFILE_KEY = "lc.simple.profileOpen";

function readProfileOpen(): boolean {
  try {
    return localStorage.getItem(PROFILE_KEY) !== "0";
  } catch {
    // storage disabled — default to showing the profile.
    return true;
  }
}

function writeProfileOpen(open: boolean): void {
  try {
    localStorage.setItem(PROFILE_KEY, open ? "1" : "0");
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

// ── rail width (px) ──────────────────────────────────────────────────────────
// The bot list is user-resizable on a wide viewport (drag its edge, double-
// click to reset), persisted per browser — mirrors the dashboard chat rail's
// lc.chat.railWidth. The clamp keeps both ends honest: a rail below MIN stops
// being a readable contact list, and one past MAX (or crowding the
// conversation under CHAT_W_MIN) stops being a rail.
const RAIL_W_KEY = "lc.simple.railWidth";
const RAIL_W_MIN = 230;
const RAIL_W_MAX = 480;
const RAIL_W_DEFAULT = 300;
const CHAT_W_MIN = 400;

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

function parseTs(iso: string | null | undefined): number {
  if (!iso) return 0;
  const n = Date.parse(iso);
  return Number.isNaN(n) ? 0 : n;
}

// Stable empty fallbacks: a group whose transcript has not been fetched yet
// must not hand a fresh array to the view on every render.
const EMPTY_MESSAGES: GroupMessage[] = [];
const EMPTY_PENDING: PendingMessage[] = [];

// First human turn of a live session — the rail label for a conversation that
// has not been titled by the backend yet.
function firstUserText(s: Session): string {
  return s.messages.find((m) => m.role === "user")?.body || "";
}

export function SimpleApp() {
  const t = useT(STR);
  const { t: tCore } = useI18n();
  const toast = useToast();
  const confirm = useConfirm();
  const { agents, agent, setAgent, current, loading, settled, reload } = useCurrentAgent();
  const { user } = useAuth();
  const isMobile = useMediaQuery(SIMPLE_MOBILE_QUERY);
  // Tri-state: `undefined` means /api/status hasn't answered yet, and MUST NOT
  // read as "no" — but here the safe direction is also the honest one, so for a
  // member the button appears only on an explicit `true`. An ADMIN gets the
  // button regardless of the capability: `POST /api/agents` has never gated
  // admins on `self_serve_agents` (that switch exists to govern members), so
  // hiding it from them advertised a restriction the server does not have —
  // which is exactly how an admin ended up staring at "You don't have any bots
  // yet" with no way to make one.
  const selfServe = useCapability("self_serve_agents") === true;
  const canCreateBots = user?.is_admin === true || selfServe;
  // Group chats are a deployment feature that ships OFF. Same tri-state rule as
  // above: `undefined` is "still loading", never "no", and only an explicit
  // `true` draws a single pixel of group UI — so a default install has no group
  // section, no group dialog, and issues no /api/groups request at all.
  const groupsEnabled = useGroupsEnabled() === true;
  const groups = useGroups(groupsEnabled);

  // ── the More panels ────────────────────────────────────────────────────────
  // Which of the dashboard views this shell offers, and which one (if any) the
  // URL is currently asking for. Selection is the pathname rather than state:
  // Back works, a reload keeps the panel, `/files?path=…` opens where it points,
  // and the notification bell's plain <Link to="/overview"> lands somewhere real.
  // Anything outside the table — including "/" — is the conversation.
  const caps = useCapabilities();
  const panels = useMemo(
    // Same rule the dashboard's TabGuard applies: default-on, and only an
    // explicit `false` hides a tab, so a still-loading capability map shows
    // everything rather than flashing the section empty.
    () => SIMPLE_PANELS.filter((p) => !p.capability || caps?.[p.capability] !== false),
    [caps],
  );
  const location = useLocation();
  const navigate = useNavigate();
  const panel = useMemo(() => {
    const found = panelForPath(location.pathname);
    return found && panels.includes(found) ? found : null;
  }, [location.pathname, panels]);

  // A dashboard route this shell does not offer — Overview's own "Manage
  // agents" button is one — would otherwise leave the address bar pointing at
  // /agents while the conversation is on screen, and a reload would land there
  // again. Send it back to "/", which IS the conversation, so the URL never
  // says something the shell is not showing. `replace` keeps Back working.
  //
  // `?thread=` rides along. The Cortex panel hands a conversation over by
  // navigating to /chat?thread=<id> (the dashboard's chat route), and this
  // redirect used to drop the query on the way to "/" — the button then did
  // nothing visible at all in this shell.
  //
  // The dashboard's chat URL names its agent in the path (/chat/<agent>), and
  // that has to survive the trip too: it becomes /?agent=<agent>&thread=<id>.
  useEffect(() => {
    if (panel || location.pathname === "/") return;
    // Wait for the capability map: until it answers, `panels` is everything, so
    // this can only fire for a path that is genuinely not a panel.
    navigate(simpleShellFallback(location.pathname, location.search), { replace: true });
  }, [panel, location.pathname, location.search, navigate]);

  // One subscription to the app-wide chat store re-renders this shell whenever
  // any conversation moves (a streamed token, an unread badge, a new thread).
  // The store notifies at most once per animation frame, so this is the same
  // cost the dashboard's chat pays.
  const version = useSyncExternalStore(chatStore.subscribe, chatStore.getSnapshot);

  const [railOpen, setRailOpen] = useState(false);
  const [mobileProfileOpen, setMobileProfileOpen] = useState(false);
  const [desktopProfileOpen, setDesktopProfileOpen] = useState(readProfileOpen);
  // The all-chats browser in the right panel. While open it takes the third
  // column over from the profile (or covers the phone as an overlay) — one
  // panel at a time, so the two can never fight over the same pixels.
  const [chatsOpen, setChatsOpen] = useState(false);
  const [newBotOpen, setNewBotOpen] = useState(false);
  const [newGroupOpen, setNewGroupOpen] = useState(false);
  const [addBotOpen, setAddBotOpen] = useState(false);
  // User-adjustable rail width (see clampRailWidth above). Applied as the
  // shell's --simple-rail-w, so the grid is the only consumer.
  const [railWidth, setRailWidth] = useState(readRailWidth);
  // Collapsed to a strip of faces. Persisted per browser like the width and
  // the profile flag; the rail owns the storage key, this owns the layout,
  // because the collapsed width is a grid track and nothing below the shell
  // can set one.
  const [railCollapsed, setRailCollapsed] = useState(readRailCollapsed);
  const toggleRailCollapsed = useCallback(() => {
    setRailCollapsed((prev) => {
      writeRailCollapsed(!prev);
      return !prev;
    });
  }, []);
  const changeRailWidth = useCallback((px: number | null) => {
    const next = px === null ? RAIL_W_DEFAULT : clampRailWidth(px);
    setRailWidth(next);
    writeRailWidth(next);
  }, []);
  // The selected bot's conversation-source filter (All / Chats / Telegram /
  // Scheduled / …), shared by the rail and the all-chats panel and persisted
  // per bot — the bot with the chatty scheduler is the one you quiet down.
  const [threadFilter, setThreadFilter] = useState<ThreadFilterId>(() => readThreadFilter(agent));
  useEffect(() => setThreadFilter(readThreadFilter(agent)), [agent]);
  const changeThreadFilter = useCallback(
    (id: ThreadFilterId) => {
      setThreadFilter(id);
      writeThreadFilter(agent, id);
    },
    [agent],
  );
  // Which group is on screen, or null when a 1:1 conversation is. The bot
  // selection is deliberately left alone underneath: leaving a group puts the
  // reader back where they were rather than on an arbitrary bot.
  const [activeGroupId, setActiveGroupId] = useState<number | null>(null);
  const railRef = useRef<HTMLElement>(null);

  const session = chatStore.activeSession(agent);
  const activeKey = chatStore.activeKeyOf(agent);
  const profileOpen = isMobile ? mobileProfileOpen : desktopProfileOpen;

  const closeRail = useCallback(() => setRailOpen(false), []);
  const closeProfile = useCallback(() => {
    if (isMobile) setMobileProfileOpen(false);
    else {
      setDesktopProfileOpen(false);
      writeProfileOpen(false);
    }
  }, [isMobile]);
  const toggleProfile = useCallback(() => {
    // While the all-chats panel holds the column, the header's info button
    // means "show me the profile instead" — swap panels rather than toggling
    // a profile the reader cannot see.
    if (chatsOpen) {
      setChatsOpen(false);
      if (isMobile) setMobileProfileOpen(true);
      else {
        setDesktopProfileOpen(true);
        writeProfileOpen(true);
      }
      return;
    }
    if (isMobile) {
      setMobileProfileOpen((o) => !o);
      return;
    }
    setDesktopProfileOpen((o) => {
      writeProfileOpen(!o);
      return !o;
    });
  }, [isMobile, chatsOpen]);

  // Crossing the breakpoint (a rotation, a resized window) must never strand an
  // off-canvas panel over a layout that has no way to dismiss it.
  useEffect(() => {
    if (!isMobile) {
      setRailOpen(false);
      setMobileProfileOpen(false);
    }
  }, [isMobile]);

  // ── the selected bot's conversations ─────────────────────────────────────
  // From the store every list of an agent's chats shares (the dashboard's
  // sidebar and chat view read the same rows): fetched once per bot, kept
  // live on the store's own SSE + turn-end triggers. A failed refetch keeps
  // the rows we have (live sessions still render) — blanking the list would
  // make a fetch error look like "no conversations yet".
  const onThreadsFailed = useCallback(() => toast.push(t("toast.threadsFailed"), "error"), [toast, t]);
  const { threads, refresh: loadThreads } = useThreadList(agent, onThreadsFailed);

  // Viewing lifecycle: the open conversation is read while it is on screen;
  // leaving makes later replies count as unread (the rail badges them).
  useEffect(() => {
    if (agent && activeKey) chatStore.markRead(agent, activeKey);
    return () => chatStore.clearViewing();
  }, [agent, activeKey]);

  // Background stream / approval / open failures surface as one calm toast.
  useEffect(
    () =>
      chatStore.subscribeError((code) => {
        const key =
          code === "approval" ? "toast.approvalFailed" : code === "open" ? "toast.openFailed" : "toast.chatFailed";
        toast.push(t(key), "error");
      }),
    [toast, t],
  );

  // ── ?thread= deep linking and the staged hand-offs ───────────────────────
  // A copied link (or a plain refresh) reopens that conversation; the open one
  // is reflected back into the URL with `replace`, so it never spams history.
  //
  // The Cortex panel arrives the same way, and this shell stays mounted while
  // it is on screen — so landing is keyed on the URL's thread CHANGING, not on
  // first mount: a mount-time one-shot had already fired long before the
  // button was pressed, and the hand-off went nowhere. Same contract as the
  // dashboard's ChatView: the URL says which thread; a staged hand-off adds a
  // draft for a brand-new one (pre-filled, never sent) and names the agent the
  // thread belongs to, which the caller switches to before navigating. Two
  // callers stage one: Cortex, and the onboarding wizard landing here after
  // setup.
  const [searchParams, setSearchParams] = useSearchParams();
  const seenUrlThreadRef = useRef<string | null>(null);
  const handoffRef = useRef<ChatHandoff | null>(null);
  const landedRef = useRef(false);
  // The thread a landing is currently opening — the reflect effect below must
  // not write the URL until it settles. See the same guard in ChatView: the
  // open is async, so the commit that starts a landing still has no session,
  // and reflecting "no thread" there deletes the `?thread=` being honoured.
  const landingRef = useRef<string | null>(null);
  const reflectedRef = useRef<string | null>(null);
  // The last thread id this browser reflected INTO the URL (null once a New
  // chat cleared it). Same role as in ChatView: it separates "not
  // open yet" from "closed", so only the second clears the URL.
  // Bumped after a draft is preloaded so the chat view parks the caret after
  // "User: " — the textarea lives in SimpleChatView, not here.
  const [composerFocusKey, setComposerFocusKey] = useState(0);
  // The onboarding hand-off: the runtime is writing this thread's first rows
  // itself (the greeting), and Agent Father may still be booting before the
  // first one lands. Shown as "waking up" instead of an empty composer until
  // a message arrives — or, as a backstop, until the greeting's own wait
  // budget is spent (runtime/onboarding_greeting.RUNNER_WAIT_SECONDS + a turn).
  const [waking, setWaking] = useState(false);
  useEffect(() => {
    if ((session?.messages.length ?? 0) > 0) setWaking(false);
  }, [session?.messages.length]);
  useEffect(() => {
    if (!waking) return;
    const timer = setTimeout(() => setWaking(false), 6 * 60_000);
    return () => clearTimeout(timer);
  }, [waking]);
  useEffect(() => {
    const staged = takeChatHandoff();
    if (staged) handoffRef.current = staged;
    const id = searchParams.get("thread");
    if (!agent || !id) return;
    const handoff = handoffRef.current?.threadId === id ? handoffRef.current : null;
    // Only skip the reflect effect's own echo: Back/Forward to a thread shown
    // earlier names it again while another one is open, and must reopen it.
    if (!handoff && id === seenUrlThreadRef.current && chatStore.activeSession(agent)?.threadId === id) return;
    // Thread ids are per-agent: opening under the previous agent would 404.
    if (handoff && agent !== handoff.agent) return;
    seenUrlThreadRef.current = id;
    landedRef.current = true;
    landingRef.current = id;
    handoffRef.current = null;
    if (handoff?.awaiting === "greeting") setWaking(true);
    void (async () => {
      try {
        // `allowMissing` covers both callers: a Cortex thread this browser is
        // about to create, and the onboarding wizard's, whose first rows the
        // runtime may still be writing (they arrive live over thread_updated).
        // An empty thread is the expected answer there, not a "Couldn't open".
        if (chatStore.activeSession(agent)?.threadId !== id) {
          await chatStore.openThread(agent, id, { allowMissing: !!handoff });
        }
        const opened = chatStore.activeSession(agent);
        if (handoff?.draft && opened && opened.threadId === id && shouldPreloadDraft(opened)) {
          chatStore.setInput(agent, opened.key, handoff.draft);
          setComposerFocusKey((k) => k + 1);
        }
      } finally {
        if (landingRef.current === id) landingRef.current = null;
      }
    })();
  }, [agent, searchParams]);
  // The conversation's address is /?agent=<name>&thread=<id>, and this effect
  // is its only writer, so the two parameters can never race each other.
  //
  // The thread half is held back until a landing URL has been acted on: before
  // that no thread is open, and reflecting "nothing" would strip the very
  // `?thread=` the effect above is waiting to honour (it waits a render for the
  // agent switch). The agent half is written regardless: it never touches the
  // thread, so a reload always reopens a thread under the agent it belongs to.
  useEffect(() => {
    if (location.pathname !== "/") return;
    const urlAgentName = searchParams.get("agent");
    // The URL named an agent the roster does not have (deleted, or not this
    // member's): the selection fell back, and the thread belonged to that
    // other agent, so it goes too.
    if (settled && urlAgentName && urlAgentName !== agent) {
      if (!agents.some((a) => a.name === urlAgentName)) {
        toast.push(tCore("toast.unknownAgent", { agent: urlAgentName }), "error");
      }
      setSearchParams(agent ? { agent } : {}, { replace: true });
      return;
    }
    const thread = searchParams.get("thread");
    let nextThread = thread;
    let move = false;
    const threadHeld = (!landedRef.current && !!thread) || !!landingRef.current;
    const id = session?.threadId ?? null;
    // Deleting is only ever right for a conversation this effect itself put in
    // the URL and that has since gone away (New chat). Before the first
    // reflection there is no such conversation — a null id there means the
    // open has not produced a session YET, and removing the parameter would
    // throw away the landing's own instruction. Reading "no thread" as "clear
    // the URL" is what dropped a hand-off's thread on a slow machine, and
    // whether it happened at all came down to which effect won the commit.
    if (!threadHeld && thread !== id && !(id === null && reflectedRef.current === null)) {
      reflectedRef.current = id;
      nextThread = id;
      // One conversation to another is a Back step; filling in a URL that
      // named no thread yet (the arrival pick, a draft's first id) is not.
      move = !!thread;
    }
    const nextAgent = urlAgentName || agent || null;
    if (nextAgent === urlAgentName && nextThread === thread) return;
    // Agent first, then thread, then whatever else the URL carried.
    const next = new URLSearchParams();
    if (nextAgent) next.set("agent", nextAgent);
    if (nextThread) next.set("thread", nextThread);
    searchParams.forEach((v, k) => {
      if (k !== "agent" && k !== "thread") next.append(k, v);
    });
    setSearchParams(next, { replace: !move });
  }, [location.pathname, session?.threadId, searchParams, setSearchParams, agent, agents, settled, toast, tCore]);

  // ── arrival: which conversation a selected bot shows ─────────────────────
  // The first time this tab looks at a bot (and after every reload) it has no
  // active session. Rather than greet the reader with an empty composer, open
  // the conversation they most likely came back for — the newest one a person
  // is in, here or on a messenger, else the newest of any kind (threadPick.ts)
  // — and only fall back to a draft when the bot has no chats at all. A draft
  // is local until its first message, so a bot with no history can be opened
  // any number of times without an empty thread ever reaching the server. A
  // `?thread=` landing above owns the choice instead; so does a bot whose
  // session already exists (switching bots and back, or New chat, keeps the
  // conversation where the reader left it).
  const [arriving, setArriving] = useState(false);
  useEffect(() => {
    if (!agent) return;
    if (chatStore.activeSession(agent)) return;
    if (searchParams.get("thread")) return;
    let cancelled = false;
    setArriving(true);
    // From the newest page when it can answer, not the whole list
    // (threadListStore.arrivalThread): that read costs every transcript the
    // bot has, and was most of the wait before a first open showed anything.
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

  // ── ⌘N / Ctrl+N — new bot ────────────────────────────────────────────────
  // Only bound when the deployment allows self-serve creation: a shortcut that
  // opens a dialog whose only outcome is a 403 is worse than no shortcut.
  useEffect(() => {
    if (!canCreateBots) return;
    function onKey(e: KeyboardEvent) {
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
      if (e.key.toLowerCase() !== "n" || e.repeat) return;
      // A dialog owns the keyboard while it is up.
      if (document.querySelector('[role="dialog"], dialog[open]')) return;
      e.preventDefault();
      setNewBotOpen(true);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canCreateBots]);

  // ── rail data ────────────────────────────────────────────────────────────
  // The selected bot's conversations: backend threads merged with live
  // in-memory sessions (streaming dot, unread badge), newest first.
  const rows = useMemo<ChatRow[]>(() => {
    const live = chatStore.sessionsFor(agent);
    const byThread = new Map<string, Session>();
    for (const s of live) if (s.threadId) byThread.set(s.threadId, s);
    const known = new Set(threads.map((th) => th.id));
    const persisted: ChatRow[] = threads.map((th) => {
      const s = byThread.get(th.id);
      const key = s?.key ?? th.id;
      return {
        key,
        threadId: th.id,
        title: th.name || th.title || th.preview || "",
        // Server-derived provenance (core/thread_labels.py) — the rail's
        // pills and the source filter both read these.
        origin: th.origin || "",
        channel: th.channel || "",
        sender: th.sender || "",
        // Working, whichever surface the turn is running on.
        streaming: !!s?.streaming || !!s?.activity,
        // The conversation on screen is read by definition — never flash a
        // stale badge for the frame before markRead runs.
        unread: key === activeKey ? 0 : (s?.unread ?? 0),
        sortTs: Math.max(parseTs(th.updated_at), s?.lastActivity ?? 0),
      };
    });
    // A brand-new conversation streaming its first turn has no backend thread
    // yet. Drafts that hold nothing but local system notes stay out. A draft
    // is by definition a chat typed HERE, so it carries no provenance and
    // files under the filter's plain-chats bucket.
    const drafts: ChatRow[] = live
      .filter(
        (s) =>
          (!s.threadId || !known.has(s.threadId)) &&
          (s.streaming || s.messages.some((m) => m.role === "user" || m.role === "assistant")),
      )
      .map((s) => ({
        key: s.key,
        threadId: s.threadId,
        title: firstUserText(s),
        origin: "",
        channel: "",
        sender: "",
        streaming: s.streaming || !!s.activity,
        unread: s.key === activeKey ? 0 : s.unread,
        sortTs: s.lastActivity,
      }));
    return [...drafts, ...persisted].sort((a, b) => b.sortTs - a.sortTs);
  }, [version, threads, agent, activeKey]);

  // Per-bot "something happened here" marks, so the rail reads like a contacts
  // list with unread counts rather than a static roster.
  const badges = useMemo<Map<string, BotBadge>>(() => {
    const map = new Map<string, BotBadge>();
    for (const bot of agents) {
      let unread = 0;
      let busy = false;
      for (const s of chatStore.sessionsFor(bot.name)) {
        if (!(bot.name === agent && s.key === activeKey)) unread += s.unread;
        if (s.streaming) busy = true;
      }
      if (unread || busy) map.set(bot.name, { unread, busy });
    }
    return map;
  }, [version, agents, agent, activeKey]);

  // The muted line under each bot's name: the last thing actually said, from
  // whichever source knows it. Live sessions know it for any bot talked to this
  // session; the selected bot's fetched thread list knows it for everything
  // older. A bot neither knows about keeps its description, which is why the
  // rail never has a blank second line.
  const previews = useMemo<Map<string, RailPreview>>(() => {
    const map = new Map<string, RailPreview>();
    for (const bot of agents) {
      let best: RailPreview | null = null;
      for (const s of chatStore.sessionsFor(bot.name)) {
        for (let i = s.messages.length - 1; i >= 0; i -= 1) {
          const m = s.messages[i];
          if (m.role === "system" || !m.body) continue;
          const ts = m.ts ?? s.lastActivity;
          if (!best || ts > best.ts) best = { text: previewText(m.body), ts };
          break;
        }
      }
      if (best) map.set(bot.name, best);
    }
    const newest = rows[0];
    const known = map.get(agent);
    if (agent && newest && (!known || known.ts < newest.sortTs)) {
      map.set(agent, { text: previewText(newest.title), ts: newest.sortTs });
    }
    return map;
  }, [version, agents, rows, agent]);

  // Group rows read the same way, from the store's per-group preview fetch.
  const groupPreviews = useMemo<Map<number, RailPreview>>(() => {
    const map = new Map<number, RailPreview>();
    for (const g of groups.groups) {
      const last = groups.previews[g.id];
      if (!last) continue;
      const who = last.author_kind === "human" ? t("groups.you") : last.author;
      map.set(g.id, {
        text: t("groups.previewLine", { who, text: previewText(last.body, 90) }),
        ts: parseTs(last.ts),
      });
    }
    return map;
  }, [groups.groups, groups.previews, t]);

  const activeGroup = useMemo(
    () => groups.groups.find((g) => g.id === activeGroupId),
    [groups.groups, activeGroupId],
  );
  const agentsByName = useMemo(() => new Map(agents.map((a) => [a.name, a])), [agents]);

  // Tell the store which transcript is on screen: it pulls that one in full on
  // every live frame and settles for a preview refresh on the others.
  useEffect(() => {
    setOpenGroup(activeGroupId);
    return () => setOpenGroup(null);
  }, [activeGroupId]);

  // A group deleted here, in another tab, or by another admin must not leave
  // the shell staring at a thread that no longer exists.
  useEffect(() => {
    if (activeGroupId === null) return;
    if (groups.status === "ready" && !groups.groups.some((g) => g.id === activeGroupId)) {
      setActiveGroupId(null);
    }
  }, [groups.status, groups.groups, activeGroupId]);

  // Turning the feature off (or losing access to it) closes the group surface
  // rather than leaving an unreachable view mounted.
  useEffect(() => {
    if (!groupsEnabled) setActiveGroupId(null);
  }, [groupsEnabled]);

  // A group's own info panel owns the right column while a group is on
  // screen — the 1:1 chats browser steps aside rather than shadowing it.
  useEffect(() => {
    if (activeGroupId !== null) setChatsOpen(false);
  }, [activeGroupId]);

  // ── actions ──────────────────────────────────────────────────────────────
  // A More panel is the pathname (see `panel` above), and it holds the whole
  // centre for as long as the URL names it. Every action below that picks a
  // conversation is a request to SEE that conversation, so it first sends the
  // URL back to "/" — which is the conversation. Without this, tapping an
  // agent after visiting Issues or Insights changed the selection underneath
  // the page and left the page on screen, so the tap looked like it did
  // nothing (the dashboard shell never had the problem: its agent rows
  // navigate to /chat). A push, not a replace: Back returns to the panel, the
  // way it returns to any page you navigated away from.
  function leavePanel() {
    if (panel) navigate("/");
  }

  // Show `name`'s conversation: ONE navigation to its address, whether that
  // also leaves a panel or not, so Back returns to exactly where the tap was
  // made (the panel, or the previous agent's conversation).
  function showBot(name: string) {
    setAgent(name, { navigate: false });
    if (panel || name !== agent) navigate(`/?agent=${encodeURIComponent(name)}`);
  }

  function selectBot(name: string) {
    showBot(name);
    setActiveGroupId(null);
    closeRail();
  }

  // The rail's chevron: select that bot and unfold its chats, leaving the
  // drawer where it is. On a phone this is how a person browses one agent's
  // conversations without the tap that would take them to the chat and shut
  // the drawer on the list they wanted.
  function expandBot(name: string) {
    showBot(name);
    setActiveGroupId(null);
  }

  function selectGroup(id: number) {
    leavePanel();
    setActiveGroupId(id);
    closeRail();
  }

  function openThread(threadId: string) {
    leavePanel();
    void chatStore.openThread(agent, threadId);
    setActiveGroupId(null);
    // On a phone the all-chats overlay covers the conversation it just
    // opened — get out of the way. The docked panel stays for browsing.
    if (isMobile) setChatsOpen(false);
    closeRail();
  }

  function openDraft(key: string) {
    leavePanel();
    chatStore.setActive(agent, key);
    chatStore.markRead(agent, key);
    setActiveGroupId(null);
    if (isMobile) setChatsOpen(false);
    closeRail();
  }

  function newChat() {
    if (!agent) return;
    leavePanel();
    chatStore.newChat(agent);
    setActiveGroupId(null);
    closeRail();
  }

  // Context-menu actions work on ANY bot row, so they name their bot instead
  // of assuming the selected one.
  function newChatFor(name: string) {
    if (!name) return;
    showBot(name);
    chatStore.newChat(name);
    setActiveGroupId(null);
    if (isMobile) setChatsOpen(false);
    closeRail();
  }

  function showProfileFor(name: string) {
    showBot(name);
    setActiveGroupId(null);
    setChatsOpen(false);
    if (isMobile) setMobileProfileOpen(true);
    else {
      setDesktopProfileOpen(true);
      writeProfileOpen(true);
    }
    closeRail();
  }

  function showAllThreadsFor(name: string) {
    showBot(name);
    setActiveGroupId(null);
    setChatsOpen(true);
    if (isMobile) {
      setMobileProfileOpen(false);
      closeRail();
    }
  }

  function markBotRead(name: string) {
    chatStore.clearUnread(name);
  }

  async function deleteThread(threadId: string) {
    const ok = await confirm({
      message: t("rail.deleteConfirm"),
      confirmLabel: t("rail.delete"),
      cancelLabel: t("rail.cancel"),
      danger: true,
    });
    if (!ok) return;
    try {
      // The store drops the row (and its live session) the moment the server
      // agrees, ahead of the confirming refetch — a refetch that fails must
      // never leave a just-deleted conversation as a ghost.
      await threadListStore.delete(agent, threadId);
      chatStore.ensureActive(agent);
    } catch {
      toast.push(t("toast.deleteFailed"), "error");
    }
  }

  /**
   * Rename a rail row — a conversation (`t:<thread id>`) or a group (`g:<id>`).
   *
   * One entry point for both because the rail draws one inline editor for
   * both, and because this is the layer that owns each side's aftermath: a
   * renamed thread needs the thread list refetched, a renamed group is applied
   * from the server's copy by its own store.
   */
  async function renameRow(key: string, name: string) {
    const [kind, id] = [key.slice(0, 2), key.slice(2)];
    try {
      if (kind === "g:") {
        await renameGroup(Number(id), name);
      } else {
        await threadListStore.rename(agent, id, name);
      }
      toast.push(t("rename.done", { name }));
    } catch (e) {
      toast.push(groupErrorText(e, t("rename.failed")), "error");
    }
  }

  function onBotCreated(name: string) {
    reload();
    if (name) showBot(name);
    setNewBotOpen(false);
  }

  // Collapsing is a wide-viewport idea only: below the breakpoint the rail is
  // already an off-canvas drawer, and a strip of faces parked over the
  // conversation would be a worse version of it.
  const railIsStrip = railCollapsed && !isMobile;

  const shellClass = [
    "simple-shell",
    (profileOpen || chatsOpen) && !panel && !isMobile ? "with-profile" : "",
    isMobile && railOpen ? "rail-open" : "",
    railIsStrip ? "rail-collapsed" : "",
  ]
    .filter(Boolean)
    .join(" ");

  // The user's chosen rail width drives the grid via its CSS variable. Only on
  // a wide viewport — the drawer sizes itself to the phone.
  // A collapsed rail is a fixed strip, so it ignores the dragged width — the
  // stylesheet's --simple-rail-collapsed-w takes over via .rail-collapsed, and
  // the reader's own width is remembered for when they open it again.
  const shellStyle =
    isMobile || railIsStrip
      ? undefined
      : ({ "--simple-rail-w": `${railWidth}px` } as React.CSSProperties);

  // `data-simple-shell` / `data-simple-bot-rail` / `data-simple-composer` are
  // the e2e contract hooks for this shell (tests/e2e/route_checks.py). They are
  // their own attributes on purpose: reusing a class or aria-label the advanced
  // shell already publishes (aside.sidebar[aria-label="Primary"], the mobile
  // topbar hamburger) would make the dashboard's own checks resolve against
  // this tree instead.
  return (
    <div className={shellClass} style={shellStyle} data-simple-shell data-testid="simple-shell">
      {isMobile && railOpen && (
        <>
          <div className="simple-scrim" onClick={closeRail} />
          {/* Escape, focus trap and focus restore for the open drawer — the
              same shared dialog behavior every modal uses. Mounted only while
              the drawer is open (hooks can't be conditional inside SimpleApp). */}
          <PanelA11y panelRef={railRef} onClose={closeRail} />
        </>
      )}

      <BotRail
        panelRef={railRef}
        agents={agents}
        agent={agent}
        badges={badges}
        previews={previews}
        rows={rows}
        activeKey={activeKey}
        threadFilter={threadFilter}
        loading={loading}
        asDrawer={isMobile}
        drawerOpen={railOpen}
        panelOpen={!!panel}
        canCreateBots={canCreateBots}
        groupsEnabled={groupsEnabled}
        groups={groups.groups}
        groupPreviews={groupPreviews}
        activeGroupId={activeGroupId}
        onSelectBot={selectBot}
        onExpandBot={expandBot}
        onOpenThread={openThread}
        onOpenDraft={openDraft}
        onNewChat={newChat}
        onNewChatFor={newChatFor}
        onDeleteThread={(id) => void deleteThread(id)}
        onRename={renameRow}
        onNewBot={() => setNewBotOpen(true)}
        onSelectGroup={selectGroup}
        onNewGroup={() => setNewGroupOpen(true)}
        onShowProfile={showProfileFor}
        onShowAllThreads={showAllThreadsFor}
        onMarkRead={markBotRead}
        onThreadFilter={changeThreadFilter}
        onRailWidth={changeRailWidth}
        panels={panels}
        onNavigatePanel={closeRail}
        collapsed={railIsStrip}
        onToggleCollapsed={toggleRailCollapsed}
        onClose={closeRail}
      />

      {/* The centre is a dashboard panel or one conversation or the other —
          never two of them mounted, so a group's transcript can't scroll behind
          a 1:1 chat and the two composers can't both claim
          `data-simple-composer`. */}
      {panel ? (
        <SimplePanelPane panel={panel} isMobile={isMobile} onOpenRail={() => setRailOpen(true)} />
      ) : activeGroup ? (
        <GroupThreadView
          group={activeGroup}
          messages={groups.messages[activeGroup.id] ?? EMPTY_MESSAGES}
          pending={groups.pending[activeGroup.id] ?? EMPTY_PENDING}
          awaiting={groups.awaiting[activeGroup.id]}
          streaming={groups.streaming[activeGroup.id]}
          activity={groups.activity[activeGroup.id]}
          agentsByName={agentsByName}
          isMobile={isMobile}
          infoOpen={profileOpen}
          onOpenRail={() => setRailOpen(true)}
          onToggleInfo={toggleProfile}
        />
      ) : (
        <SimpleChatView
          agent={agent}
          bot={current}
          // For the peer-thinking line: a bot this one reaches with
          // talk_to_agent is somebody else on the roster, so its own face and
          // canonical name are here rather than in the chat's single `bot`.
          agentsByName={agentsByName}
          session={session}
          composerFocusKey={composerFocusKey}
          arriving={arriving}
          waking={waking}
          isMobile={isMobile}
          profileOpen={profileOpen}
          hasBots={agents.length > 0}
          loadingBots={loading}
          canCreateBots={canCreateBots}
          onOpenRail={() => setRailOpen(true)}
          onToggleProfile={toggleProfile}
          onNewChat={newChat}
          onNewBot={() => setNewBotOpen(true)}
          // Only offered when it can actually work: groups on, and a second
          // bot to add. Undefined draws no button rather than one whose only
          // outcome is a refusal.
          onAddBot={
            groupsEnabled && agents.length > 1 ? () => setAddBotOpen(true) : undefined
          }
          onThreadsChanged={loadThreads}
        />
      )}

      {/* A panel takes the whole centre AND the right column: it is a page, not
          a conversation, so there is no conversation for a profile to be about. */}
      {profileOpen && !panel && activeGroup && (
        <GroupInfoPanel
          group={activeGroup}
          agents={agents}
          asOverlay={isMobile}
          onClose={closeProfile}
          onDeleted={() => setActiveGroupId(null)}
        />
      )}
      {/* The right column holds ONE panel at a time: the all-chats browser
          when it is open, the bot's profile otherwise. */}
      {chatsOpen && !panel && !activeGroup && !!agent && (
        <ChatsPanel
          agent={agent}
          rows={rows}
          activeKey={activeKey}
          threadFilter={threadFilter}
          asOverlay={isMobile}
          onThreadFilter={changeThreadFilter}
          onOpenThread={openThread}
          onOpenDraft={openDraft}
          onDeleteThread={(id) => void deleteThread(id)}
          onClose={() => setChatsOpen(false)}
        />
      )}
      {profileOpen && !panel && !chatsOpen && !activeGroup && !!agent && (
        <BotProfilePanel
          agent={agent}
          bot={current}
          asOverlay={isMobile}
          onClose={closeProfile}
          onChanged={reload}
        />
      )}

      {newBotOpen && <NewBotDialog onClose={() => setNewBotOpen(false)} onCreated={onBotCreated} />}
      {newGroupOpen && groupsEnabled && (
        <NewGroupDialog
          agents={agents}
          onClose={() => setNewGroupOpen(false)}
          onCreated={(group) => {
            setNewGroupOpen(false);
            setActiveGroupId(group.id);
          }}
        />
      )}
      {addBotOpen && groupsEnabled && !!agent && (
        <AddBotToChatDialog
          agent={agent}
          agents={agents}
          // A chat with nothing sent yet has no persisted thread, so there is
          // no transcript to carry — the dialog drops the question rather than
          // offering a switch that would do nothing.
          threadId={session?.threadId ?? ""}
          onClose={() => setAddBotOpen(false)}
          onCreated={(group) => {
            setAddBotOpen(false);
            setActiveGroupId(group.id);
          }}
        />
      )}
      {/* Mounted here as well as in the advanced Layout: this shell never
          renders <Layout>, so simple-mode users previously saw neither the
          install prompt nor — the one that matters — the "a new version is
          ready" prompt, which is a primary way out of a stale build. Both
          banners are position:fixed, so the placement inside the shell is
          about lifetime, not layout. */}
      <PwaBanners />
    </div>
  );
}

/**
 * One of the dashboard views, mounted in the shell's centre column.
 *
 * The view itself is untouched — it renders its own `.page` exactly as it does
 * under the dashboard's `<main class="main">`, which is why this wrapper carries
 * that same class. What it adds is the two things the shell owns and the view
 * cannot know about: the phone's way back to the rail (there is no topbar
 * hamburger here) and the Suspense/error boundary for its lazily-loaded chunk,
 * keyed by path so navigating away clears a failed import and the next panel
 * retries rather than inheriting the error card.
 */
function SimplePanelPane({
  panel,
  isMobile,
  onOpenRail,
}: {
  panel: SimplePanel;
  isMobile: boolean;
  onOpenRail: () => void;
}) {
  const t = useT(STR);
  return (
    <main className="main simple-panel" data-simple-panel={panel.to}>
      {isMobile && (
        <div className="simple-panel-bar">
          <button
            type="button"
            className="btn ghost xs"
            onClick={onOpenRail}
            aria-label={t("chat.openBots")}
            title={t("chat.openBots")}
          >
            <PanelLeftOpen size={16} strokeWidth={1.9} />
          </button>
          <strong className="simple-panel-title">{t(panel.key)}</strong>
        </div>
      )}
      <RouteErrorBoundary key={panel.to}>
        <Suspense
          fallback={
            <div className="page">
              <div className="spinner" style={{ marginTop: 40 }} />
            </div>
          }
        >
          {/* Same URL rules as the dashboard's routes: a legacy ?agent= spelling
              or an agent not in the roster is rewritten before the panel runs. */}
          <RouteCanonical>
            <panel.View />
          </RouteCanonical>
        </Suspense>
      </RouteErrorBoundary>
    </main>
  );
}

// Hook host for an off-canvas panel: mounted only while the panel is open, so
// the shared dialog behavior applies for exactly its lifetime.
function PanelA11y({
  panelRef,
  onClose,
}: {
  panelRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  useDialogA11y(panelRef, onClose);
  return null;
}
