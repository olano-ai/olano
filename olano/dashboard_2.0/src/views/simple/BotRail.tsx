// The left rail: a list of BOTS, the way a messenger lists contacts.
//
// Deliberately not a list of threads. A person thinks "I want to ask Merli
// something", picks the bot, and only then cares which conversation — so the
// bots are the top level, pinnable like favourite contacts, and the selected
// bot's conversations unfold underneath it. The rail also carries the three
// things that have nowhere else to live in a shell with no nav: the "New bot"
// button, the group-chat list, and the account menu.
//
// A row is the messenger row everyone already knows: face, bold name, the
// time of the last thing that happened on the right, and one muted line of
// what it was. That line is the row's whole job — a rail of eight identical
// names is a directory, not a conversation list.
//
// The selected bot's conversations fold and unfold on a second click of its
// row, carry the same provenance pills the dashboard's rail uses (SCHEDULED,
// TELEGRAM, AGENT, …), can be filtered by where they came from, and cap at a
// handful — "Show all" hands the full, paginated list to the right-hand
// panel, so a bot with 100 conversations costs the rail seven rows, not 100.
//
// Below the breakpoint the whole thing becomes an off-canvas drawer (SimpleApp
// owns the open state and the focus trap); the layout for that lives in
// simple.css, because an inline `position` would outrank the drawer's `fixed`.

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ArrowDown,
  ArrowUp,
  BellOff,
  ChevronDown,
  History,
  Info,
  MessageSquarePlus,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Pin,
  PinOff,
  Search,
  Trash2,
  UsersRound,
  X,
} from "lucide-react";
import { useAuth } from "../../core/auth";
import { NavLink } from "react-router-dom";
import { useI18n, useT } from "../../core/i18n";
import type { Agent } from "../../core/types";
import { PlusIcon, SignOutIcon } from "../../ui/icons";
import { ThemeSelect, LanguageSelect } from "../../ui/PreferenceSelects";
import { useAnchor } from "../../ui/anchoredMenu";
import { NotificationBell } from "../notifications/NotificationBell";
import { placeItem, type NavSection, type NavSplit } from "../../core/nav";
import type { SimplePanel } from "./panels";
import { UiModeSwitch } from "../../ui/UiModeSwitch";
import { ChannelPill, isPlatformChannel, ORIGIN_ICONS, OriginPill } from "../chat/channels";
import { STR as CHAT_STR } from "../chat/strings";
import { BotMark, GroupMark } from "./BotMark";
import {
  asleepShown,
  asleepToggleShown,
  bandOf,
  isAsleep,
  moveWithinBands,
  pipTone,
  presenceOf,
  splitAsleep,
  type Presence,
} from "../agents/presence";
import { PRESENCE_STR, presenceText } from "../agents/presenceStrings";
import { initials, railTime } from "./format";
import type { GroupSummary } from "./groupsStore";
import { STR } from "./strings";
import { ThreadFilterChips } from "../chat/ThreadFilterChips";
import { buildFilterOptions, filterThreadRows, type ThreadFilterId } from "../chat/threadFilters";

/** One conversation of the selected bot, as the rail renders it. */
export interface ChatRow {
  /** Stable local handle — the thread id, or `draft:<n>` for a new chat. */
  key: string;
  threadId: string | null;
  title: string;
  /** Server-derived provenance (core/thread_labels.py). Empty for drafts. */
  origin: string;
  channel: string;
  sender: string;
  streaming: boolean;
  unread: number;
  /** Epoch ms of the last activity; the rail is sorted on it by SimpleApp. */
  sortTs: number;
}

/** What the rail shows beside a bot that has something going on. */
export interface BotBadge {
  unread: number;
  busy: boolean;
}

/** The muted line + right-aligned stamp on a row. `ts` 0 = no stamp. */
export interface RailPreview {
  text: string;
  ts: number;
}

// How many conversations the rail shows before handing over to the all-chats
// panel. Seven keeps the selected bot's block within one glance while still
// covering "the chats I actually flip between".
const RAIL_THREAD_LIMIT = 7;

// Pinned bots, persisted per browser. Pinning is a per-device convenience (the
// order you like your contacts in), not account state, so it lives in
// localStorage exactly like the theme and the sidebar width do.
const PINS_KEY = "lc.simple.pins";

// The order the UNPINNED bots sit in, once a drag has given them one. Empty is
// the default and means alphabetical — a rail nobody has touched should be in
// the order everybody already expects. The first drop materialises the whole
// current order, so every later drop has something stable to move within, and
// a bot missing from the list (one created since) sorts alphabetically *after*
// the ones that are in it rather than silently jumping to the top.
const ORDER_KEY = "lc.simple.botOrder";

// Which of the rail's sections are folded shut, by id. Same reasoning as the
// pins: a fold is a per-device convenience, not account state.
const FOLDED_KEY = "lc.simple.railFolded";

// Whether the rail is collapsed to a strip of faces. Also per device — it is a
// layout preference about this window, exactly like the rail's width.
const COLLAPSED_KEY = "lc.simple.railCollapsed";

// Whether the **More** section (the dashboard panels below the groups) is open.
// Its own key rather than a fourth id in FOLDED_KEY, because its default is the
// opposite: More starts CLOSED, the way the dashboard's own More group does
// (`nav_more_collapsed ?? true`) — the rail is a list of agents first, and five
// operator panels unfurled above the account button would say otherwise.
const MORE_OPEN_KEY = "lc.simple.moreOpen";

// Whether All agents shows its "N offline" tail (views/agents/presence.ts).
// Same shape and same default as More: CLOSED until this browser opens it, so
// a rail full of stopped agents leads with the ones that can answer.
const OFFLINE_OPEN_KEY = "lc.simple.offlineOpen";

function readOfflineOpen(): boolean {
  try {
    return localStorage.getItem(OFFLINE_OPEN_KEY) === "1";
  } catch {
    // storage disabled: closed, the default.
    return false;
  }
}

function writeOfflineOpen(open: boolean): void {
  try {
    localStorage.setItem(OFFLINE_OPEN_KEY, open ? "1" : "0");
  } catch {
    // storage disabled / quota: keep working in-memory only.
  }
}

function readMoreOpen(): boolean {
  try {
    return localStorage.getItem(MORE_OPEN_KEY) === "1";
  } catch {
    // storage disabled — closed, the default.
    return false;
  }
}

function writeMoreOpen(open: boolean): void {
  try {
    localStorage.setItem(MORE_OPEN_KEY, open ? "1" : "0");
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

// How the panels are split between the always-visible group above More and More
// itself — the same gesture and the same shape as the dashboard's own nav, over
// this shell's five entries.
//
// Deliberately localStorage and NOT the server-side `nav_primary`/`nav_more`
// prefs the console uses, for two reasons. It is the same kind of thing as the
// rail's pins, hand order and folds, which are all per-device here. And a
// write-back from this shell would carry only the five paths it knows about,
// silently dropping the other eighteen from the console's saved split.
const PANEL_SPLIT_KEY = "lc.simple.panelSplit";

/** The saved split, or null when this browser has never arranged one. */
function readPanelSplit(): NavSplit | null {
  try {
    const raw = localStorage.getItem(PANEL_SPLIT_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    if (!parsed || typeof parsed !== "object") return null;
    const rec = parsed as Record<string, unknown>;
    const list = (v: unknown) =>
      Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
    return { primary: list(rec.primary), more: list(rec.more) };
  } catch {
    // storage disabled or a corrupt value — everything under More, the default.
    return null;
  }
}

function writePanelSplit(split: NavSplit): void {
  try {
    localStorage.setItem(PANEL_SPLIT_KEY, JSON.stringify(split));
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

/**
 * Resolve `panels` into the two groups using a saved split.
 *
 * Everything starts under More: this shell is a chat app, and five dashboard
 * pages unfurled above the account button on a first visit would say otherwise.
 * A panel absent from the saved split (newly shipped, or newly un-gated) lands
 * under More too, so it is discoverable without rearranging what the human put
 * where.
 */
function splitPanels(panels: SimplePanel[], saved: NavSplit | null): {
  primary: SimplePanel[];
  more: SimplePanel[];
} {
  const byTo = new Map(panels.map((p) => [p.to, p] as const));
  const seen = new Set<string>();
  const pick = (order: string[]) => {
    const out: SimplePanel[] = [];
    for (const to of order) {
      const p = byTo.get(to);
      if (p && !seen.has(to)) {
        out.push(p);
        seen.add(to);
      }
    }
    return out;
  };
  const primary = pick(saved?.primary ?? []);
  const more = pick(saved?.more ?? []);
  // Anything the saved split never named — a panel shipped since, or one a
  // capability has just un-gated — joins the end of More.
  more.push(...panels.filter((p) => !seen.has(p.to)));
  return { primary, more };
}

/** Read a persisted list of names; anything unreadable reads as "not set". */
function readNames(key: string): string[] {
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : [];
  } catch {
    // storage disabled or a corrupt value — start empty rather than throw.
    return [];
  }
}

function writeNames(key: string, names: string[]): void {
  try {
    localStorage.setItem(key, JSON.stringify(names));
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

function readPins(): string[] {
  return readNames(PINS_KEY);
}

function writePins(pins: string[]): void {
  writeNames(PINS_KEY, pins);
}

/** The rail's three foldable sections. A group row is not a bot, so a bot can
 *  only ever be dropped into the first two — see `BotSection`. */
type SectionId = "pinned" | "all" | "groups";
/** The two a bot row can live in. */
type BotSection = "pinned" | "all";

const SECTION_IDS: readonly SectionId[] = ["pinned", "all", "groups"];

function readFolded(): Set<SectionId> {
  return new Set(readNames(FOLDED_KEY).filter((id): id is SectionId =>
    (SECTION_IDS as readonly string[]).includes(id),
  ));
}

export function readRailCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    // storage disabled — a rail nobody asked to collapse stays open.
    return false;
  }
}

export function writeRailCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

/**
 * Move `name` to `index` in `list`, inserting it if it isn't there yet.
 *
 * `index` is an insertion point measured in the list the reader is LOOKING at
 * — the one that still contains the dragged row. So when the row is moving
 * down within its own section, pulling it out first shifts every later slot up
 * by one and the raw index would land it one place too far; that is the `from <
 * index` correction, and dropping it is the classic "drag down by one does
 * nothing" bug.
 */
function moveInto(list: string[], name: string, index: number): string[] {
  const from = list.indexOf(name);
  const without = list.filter((n) => n !== name);
  const raw = from !== -1 && from < index ? index - 1 : index;
  const at = Math.max(0, Math.min(without.length, raw));
  return [...without.slice(0, at), name, ...without.slice(at)];
}

/**
 * Where the right-click menu opens, and on what.
 *
 * Four kinds open one — a bot, one of its conversations, a group, and the
 * rail's own empty space — and they share a single piece of state so only one
 * menu is ever open, and one Escape/outside-click handler closes whichever it
 * is.
 *
 * The fourth is the list's *background*. Right-clicking there used to fall
 * through to the browser's own Back / Reload / Inspect Element menu, which is
 * both useless and the one place a person reaches for when they want to make
 * something new — so that gesture now offers exactly what the rail can create.
 */
type RailMenuState =
  | { kind: "bot"; x: number; y: number; bot: string }
  | { kind: "thread"; x: number; y: number; row: ChatRow }
  | { kind: "group"; x: number; y: number; group: GroupSummary }
  | { kind: "rail"; x: number; y: number }
  | { kind: "panel"; x: number; y: number; to: string; section: NavSection };

export function BotRail({
  panelRef,
  agents,
  agent,
  badges,
  previews,
  rows,
  activeKey,
  threadFilter,
  loading,
  asDrawer,
  drawerOpen,
  panelOpen,
  canCreateBots,
  groupsEnabled,
  groups,
  groupPreviews,
  activeGroupId,
  onSelectBot,
  onExpandBot,
  onOpenThread,
  onOpenDraft,
  onNewChat,
  onNewChatFor,
  onDeleteThread,
  onRename,
  onNewBot,
  onSelectGroup,
  onNewGroup,
  onShowProfile,
  onShowAllThreads,
  onMarkRead,
  onThreadFilter,
  onRailWidth,
  panels,
  onNavigatePanel,
  collapsed,
  onToggleCollapsed,
  onClose,
}: {
  panelRef: React.RefObject<HTMLElement | null>;
  agents: Agent[];
  agent: string;
  badges: Map<string, BotBadge>;
  /** Last-message line per bot, keyed by name. Missing = show the description. */
  previews: Map<string, RailPreview>;
  rows: ChatRow[];
  activeKey: string | undefined;
  /** The selected bot's source filter (SimpleApp owns and persists it). */
  threadFilter: ThreadFilterId;
  loading: boolean;
  /** Below the breakpoint the rail is an off-canvas drawer. */
  asDrawer: boolean;
  /** Whether that drawer is currently open (ignored on a wide viewport). */
  drawerOpen: boolean;
  /**
   * A More panel (Issues, Insights, …) holds the centre instead of the
   * conversation. A click on the selected bot's row then means "back to the
   * chat" and goes through `onSelectBot`, rather than folding a list the
   * reader is not looking at.
   */
  panelOpen: boolean;
  canCreateBots: boolean;
  /**
   * Group chats are a deployment feature that is OFF by default. This is only
   * ever `true` on an explicit capability — never on the tri-state's
   * `undefined` — so the section simply does not exist on a default install.
   */
  groupsEnabled: boolean;
  groups: GroupSummary[];
  groupPreviews: Map<number, RailPreview>;
  activeGroupId: number | null;
  onSelectBot: (name: string) => void;
  /**
   * Select that bot and unfold its chats WITHOUT closing the drawer — the
   * row's chevron. `onSelectBot` is the tap that goes to the conversation
   * (and shuts a phone's drawer on the way); this one is for browsing the
   * list, which is the whole reason the chevron exists.
   */
  onExpandBot: (name: string) => void;
  onOpenThread: (threadId: string) => void;
  onOpenDraft: (key: string) => void;
  onNewChat: () => void;
  /** New chat with a NAMED bot (the context menu works on any row). */
  onNewChatFor: (name: string) => void;
  onDeleteThread: (threadId: string) => void;
  /**
   * Persist a row's new name. The key is `t:<thread id>` for a conversation
   * and `g:<group id>` for a group — prefixed because the two id spaces are
   * different types that would otherwise collide on "1".
   */
  onRename: (key: string, name: string) => Promise<void>;
  onNewBot: () => void;
  onSelectGroup: (id: number) => void;
  onNewGroup: () => void;
  /** Select that bot and open its profile panel. */
  onShowProfile: (name: string) => void;
  /** Select that bot and open the all-chats panel on the right. */
  onShowAllThreads: (name: string) => void;
  /** Clear every unread badge that bot carries. */
  onMarkRead: (name: string) => void;
  onThreadFilter: (id: ThreadFilterId) => void;
  /**
   * Live width while the edge handle is dragged, in px; `null` = reset to the
   * default. Absent below the breakpoint, where the rail is a drawer.
   */
  onRailWidth: (px: number | null) => void;
  /**
   * The dashboard panels to list under **More**, already filtered by the
   * deployment's `tab_*` settings. Empty draws no More section at all rather
   * than a heading over nothing.
   */
  panels: SimplePanel[];
  /** A panel link was followed — the shell closes the mobile drawer on it. */
  onNavigatePanel: () => void;
  /**
   * Collapsed to a strip of faces: names, previews, search and the selected
   * bot's chats are all hidden, and only the marks (with their online pip) and
   * the section dividers remain. Always `false` in drawer mode — a phone's
   * drawer is already the compact form, and a 76px strip over a conversation
   * would be a worse one.
   */
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onClose: () => void;
}) {
  const t = useT(STR);
  // Channel product names + origin labels are the dashboard rail's own strings
  // (channels.tsx resolves against them), so the pills read identically in
  // both shells.
  const tc = useT(CHAT_STR);
  const tp = useT(PRESENCE_STR);
  const { lang } = useI18n();
  // A group somebody else shared with this reader wears a small tag in the
  // rail, so "mine" and "let in" read apart without opening each one.
  const { user: me } = useAuth();
  const [query, setQuery] = useState("");
  const [pins, setPins] = useState<string[]>(readPins);
  // The hand-arranged order of the unpinned bots (see ORDER_KEY).
  const [order, setOrder] = useState<string[]>(() => readNames(ORDER_KEY));
  // Folded sections, and the row currently being dragged with where it would
  // land. `drop` is null whenever the pointer is not over a valid target, so
  // releasing outside the rail is a no-op rather than a move to nowhere.
  const [folded, setFolded] = useState<Set<SectionId>>(readFolded);
  const [moreOpen, setMoreOpen] = useState<boolean>(readMoreOpen);
  const [offlineOpen, setOfflineOpenState] = useState<boolean>(readOfflineOpen);
  // The panel split, plus the live in-flight arrangement while one is dragged.
  // `panelWork` is the preview: it shows the drop before it is committed, and a
  // cancelled drag (Escape, released outside) throws it away without persisting.
  const [panelSplit, setPanelSplit] = useState<NavSplit | null>(readPanelSplit);
  const [dragPanel, setDragPanel] = useState<string | null>(null);
  const [panelWork, setPanelWork] = useState<NavSplit | null>(null);
  const [dragName, setDragName] = useState<string | null>(null);
  const [drop, setDrop] = useState<{ section: BotSection; index: number } | null>(null);
  // What the two panel groups hold right now: the in-flight preview while a row
  // is being dragged, the saved split otherwise.
  const shownPanels = useMemo(
    () => splitPanels(panels, panelWork ?? panelSplit),
    [panels, panelWork, panelSplit],
  );
  const scrollRef = useRef<HTMLDivElement>(null);

  // Whether the selected bot's conversation block is folded away. A second
  // click on the selected row toggles it; switching bots always unfolds, so a
  // fold can never hide the list you just asked for.
  const [threadsHidden, setThreadsHidden] = useState(false);
  useEffect(() => setThreadsHidden(false), [agent]);

  // The right-click menu — on a bot row, a conversation, or a group.
  const [menu, setMenu] = useState<RailMenuState | null>(null);
  // Inline rename: which row is being edited and its draft. `renamingRef`
  // mirrors it for synchronous reads, so the blur that fires as the input
  // unmounts (Escape, or switching rows) reads the live value rather than the
  // stale closure — the same guard the dashboard rail's rename uses.
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const renamingRef = useRef<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menu) return;
    function onDoc(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(null);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setMenu(null);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  // Edge-drag resize (wide viewports only — the drawer sizes itself). The
  // handle reports the raw pointer-derived width; SimpleApp clamps + persists.
  const [resizing, setResizing] = useState(false);
  function startResize(e: React.MouseEvent) {
    if (e.button !== 0) return;
    e.preventDefault();
    setResizing(true);
    const el = panelRef.current;
    const rtl = el ? getComputedStyle(el).direction === "rtl" : false;
    const move = (ev: MouseEvent) => {
      const rect = panelRef.current?.getBoundingClientRect();
      if (!rect) return;
      onRailWidth(rtl ? rect.right - ev.clientX : ev.clientX - rect.left);
    };
    const up = () => {
      setResizing(false);
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }

  // One coarse clock for every stamp in the rail: "2:39" only changes by the
  // minute, so a per-row timer would be 20 timers for one visible change.
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  const pinned = useMemo(() => new Set(pins), [pins]);
  // A group's mark wears its members' real faces, so it needs the roster the
  // rail already has rather than a second prop threaded down from SimpleApp.
  const agentsByName = useMemo(
    () => new Map(agents.map((a) => [a.name, a] as const)),
    [agents],
  );

  function togglePin(name: string) {
    setPins((prev) => {
      const next = prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name];
      writePins(next);
      return next;
    });
  }

  // Pinned bots first (in pin order, which drag-and-drop also rewrites), then
  // everyone else in the reader's own arrangement — falling back to
  // alphabetical for any bot that arrangement has never seen. A stable order is
  // what makes a rail scannable, and "stable" has to include a bot that was
  // created after the last drag.
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matches = q
      ? agents.filter(
          (a) =>
            a.name.toLowerCase().includes(q) || (a.description || "").toLowerCase().includes(q),
        )
      : agents;
    const top = pins.map((n) => matches.find((a) => a.name === n)).filter((a): a is Agent => !!a);
    const rank = new Map(order.map((n, i) => [n, i] as const));
    const rest = matches
      .filter((a) => !pinned.has(a.name))
      .sort(
        (a, b) =>
          (rank.get(a.name) ?? Number.MAX_SAFE_INTEGER) -
            (rank.get(b.name) ?? Number.MAX_SAFE_INTEGER) || a.name.localeCompare(b.name),
      );
    return { top, rest };
  }, [agents, order, pins, pinned, query]);

  // Whether each bot can answer (views/agents/presence.ts). Status never moves
  // a bot between Pinned and All agents: it only decides, inside All agents,
  // whether the row sits above the "N offline" fold or behind it. The open
  // bot (its chats are unfolded under it) and any bot with unread replies stay
  // above it.
  const presence = useMemo(
    () => new Map(agents.map((a) => [a.name, presenceOf(a)] as const)),
    [agents],
  );
  const presenceFor = (n: string): Presence => presence.get(n) ?? "offline";
  const filtering = !!query.trim();
  const bands = useMemo(() => {
    const byRest = new Map(visible.rest.map((a) => [a.name, a] as const));
    const { awake, asleep } = splitAsleep(
      visible.rest.map((a) => a.name),
      (n) => presence.get(n) ?? "offline",
      (n) => (n === agent && activeGroupId === null) || (badges.get(n)?.unread ?? 0) > 0,
    );
    const pick = (names: string[]) => names.map((n) => byRest.get(n)).filter((a): a is Agent => !!a);
    return { awake: pick(awake), asleep: pick(asleep), awakeNames: awake, asleepNames: asleep };
  }, [visible.rest, presence, agent, activeGroupId, badges]);
  const showAsleep = asleepShown({ storedOpen: offlineOpen, awakeCount: bands.awake.length, filtering });
  const asleepToggle = asleepToggleShown({
    asleepCount: bands.asleep.length,
    awakeCount: bands.awake.length,
    filtering,
  });
  // The All agents rows as drawn, which is what a drop index is measured in.
  const restShown = showAsleep ? [...bands.awake, ...bands.asleep] : bands.awake;

  function toggleOffline() {
    setOfflineOpenState((open) => {
      writeOfflineOpen(!open);
      return !open;
    });
  }

  // ── drag to rearrange ──────────────────────────────────────────────────────
  // Two sections, one gesture: dragging a row inside its own section reorders
  // it, and dragging it across the divider pins or unpins it AND places it —
  // which is the whole reason the drop carries an index rather than just a
  // section.
  //
  // Deliberately off while a search filter is active. The order this writes is
  // the full list, and a filtered rail only shows part of it, so a drop there
  // would silently rewrite the position of bots the reader cannot even see.
  const canReorder = !asDrawer && !query.trim();

  function beginDrag(e: React.DragEvent, name: string) {
    if (!canReorder) return;
    e.dataTransfer.effectAllowed = "move";
    // Some engines refuse to start a drag with an empty payload, and the name
    // is the honest thing to carry anyway.
    e.dataTransfer.setData("text/plain", name);
    setDragName(name);
  }

  function endDrag() {
    setDragName(null);
    setDrop(null);
  }

  /** Hovering a row: the pointer's half decides above-it or below-it. */
  function hoverRow(e: React.DragEvent, section: BotSection, index: number) {
    if (!dragName) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = "move";
    const rect = e.currentTarget.getBoundingClientRect();
    const after = e.clientY > rect.top + rect.height / 2;
    setDrop({ section, index: after ? index + 1 : index });
  }

  /** Hovering the section's own box — the gap under the last row, or an empty
   *  Pinned section, both of which have to accept a drop or "drag it up here
   *  to pin it" is a promise the rail doesn't keep. */
  function hoverSection(e: React.DragEvent, section: BotSection, count: number) {
    if (!dragName) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDrop((prev) => (prev && prev.section === section ? prev : { section, index: count }));
  }

  function applyDrop(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    const name = dragName;
    const target = drop;
    endDrag();
    if (!name || !target) return;

    if (target.section === "pinned") {
      const next = moveInto(pins, name, target.index);
      setPins(next);
      writePins(next);
      return;
    }
    // Into "All bots": unpin first if it was pinned, then place it. The order
    // list only ever describes unpinned bots, so a pinned name never sits in
    // both.
    if (pinned.has(name)) {
      const nextPins = pins.filter((n) => n !== name);
      setPins(nextPins);
      writePins(nextPins);
    }
    // Materialise the section's current display order before moving inside it:
    // until the first drag `order` is empty and the rail is alphabetical, and
    // writing only the moved name would leave every other bot unranked. The
    // display draws the awake band then the offline tail, so the move is kept
    // inside those bands: a bot dragged past its band's end rests at the edge
    // instead of being written somewhere the next render moves it away from.
    const next = moveWithinBands(bands.awakeNames, bands.asleepNames, name, target.index);
    setOrder(next);
    writeNames(ORDER_KEY, next);
  }

  /**
   * The keyboard/touch route to the same rearrangement, offered on the row's
   * context menu. HTML5 drag-and-drop reaches neither a screen reader nor a
   * phone, so a rail that can ONLY be arranged by dragging cannot be arranged
   * at all by a good share of the people using it.
   */
  function nudge(name: string, delta: -1 | 1) {
    if (pinned.has(name)) {
      const at = pins.indexOf(name);
      if (at === -1) return;
      const next = moveInto(pins, name, at + (delta > 0 ? 2 : -1));
      setPins(next);
      writePins(next);
      return;
    }
    // A step is measured inside the band the row is drawn in (see applyDrop).
    const band = bandOf(bands.awakeNames, bands.asleepNames, name);
    if (!band) return;
    const at = band.indexOf(name);
    const offset = band === bands.awakeNames ? 0 : bands.awakeNames.length;
    const next = moveWithinBands(bands.awakeNames, bands.asleepNames, name, offset + at + (delta > 0 ? 2 : -1));
    setOrder(next);
    writeNames(ORDER_KEY, next);
  }

  function toggleSection(id: SectionId) {
    setFolded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      writeNames(FOLDED_KEY, [...next]);
      return next;
    });
  }

  // Groups match on their own name and on the bots inside them, so "merli"
  // finds every group Merli is in — the search box says so.
  const visibleGroups = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return groups;
    return groups.filter(
      (g) =>
        g.name.toLowerCase().includes(q) ||
        g.members.some((m) => m.toLowerCase().includes(q)),
    );
  }, [groups, query]);

  // The selected bot's conversations through its filter, capped for the rail.
  // Counts come from the FULL list so a chip can say what it would reveal.
  const filterOptions = useMemo(() => buildFilterOptions(rows), [rows]);
  const filteredRows = useMemo(() => filterThreadRows(rows, threadFilter), [rows, threadFilter]);
  const shownRows = filteredRows.slice(0, RAIL_THREAD_LIMIT);
  const hiddenCount = filteredRows.length - shownRows.length;

  const empty = !loading && agents.length === 0;
  const noMatches =
    !loading &&
    agents.length > 0 &&
    visible.top.length + visible.rest.length + visibleGroups.length === 0;

  // Up/Down walk the rows the way a native list does. Rows are buttons, so Tab
  // already reaches them; this is the ergonomic half — a long rail should not
  // cost twenty Tabs to cross, and Home/End jump the ends.
  function onRailKeyDown(e: React.KeyboardEvent) {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const root = scrollRef.current;
    if (!root) return;
    const items = Array.from(root.querySelectorAll<HTMLButtonElement>("button.simple-row"));
    if (items.length === 0) return;
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    if (at === -1 && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? items.length - 1
          : e.key === "ArrowDown"
            ? Math.min(at + 1, items.length - 1)
            : Math.max(at - 1, 0);
    items[next]?.focus();
  }

  // ── inline rename ──────────────────────────────────────────────────────────
  // One editor for both kinds of row: a conversation (`t:<thread id>`) and a
  // group (`g:<id>`). The key is prefixed because the two id spaces are
  // different types that would otherwise collide on "1".
  function startRename(key: string, current: string) {
    renamingRef.current = key;
    setRenaming(key);
    setRenameDraft(current);
  }

  function cancelRename() {
    renamingRef.current = null;
    setRenaming(null);
    setRenameDraft("");
  }

  async function commitRename(key: string, current: string) {
    if (renamingRef.current !== key) return; // already settled (Escape, then blur)
    renamingRef.current = null;
    const name = renameDraft.trim();
    setRenaming(null);
    // Unchanged, or emptied — not a rename. Refusing it here keeps the error
    // path for the cases that are actually a problem.
    if (!name || name === current) return;
    await onRename(key, name);
  }

  function renameInput(key: string, current: string, placeholder: string) {
    return (
      <input
        className="simple-rename-input"
        autoFocus
        value={renameDraft}
        maxLength={120}
        placeholder={placeholder}
        aria-label={placeholder}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => setRenameDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void commitRename(key, current);
          } else if (e.key === "Escape") {
            e.preventDefault();
            cancelRename();
          }
        }}
        onBlur={() => void commitRename(key, current)}
      />
    );
  }

  function renderThread(row: ChatRow) {
    const key = `t:${row.threadId ?? ""}`;
    const title = row.title || t("rail.chatFallback");
    if (row.threadId && renaming === key) {
      return (
        <li key={row.key}>
          <div className="simple-thread is-renaming">
            {renameInput(key, row.title, t("rail.renamePlaceholder"))}
          </div>
        </li>
      );
    }
    return (
      <li key={row.key}>
        <div
          className={`simple-thread${activeKey === row.key ? " is-active" : ""}`}
          onContextMenu={(e) => {
            // A draft has no thread to name or delete yet — nothing the menu
            // could offer, so it stays the browser's own menu.
            if (!row.threadId) return;
            e.preventDefault();
            setMenu({ kind: "thread", x: e.clientX, y: e.clientY, row });
          }}
        >
          <button
            type="button"
            className="simple-thread-btn"
            onClick={() => (row.threadId ? onOpenThread(row.threadId) : onOpenDraft(row.key))}
          >
            {/* The same provenance badges the dashboard rail wears: automated
                rows get their origin pill; platform chats their channel pill
                (icon-only beside an origin pill, so two badges never crowd
                out the title). An ordinary chat is the unbadged baseline. */}
            <OriginPill origin={row.origin} t={tc} />
            {isPlatformChannel(row.channel) && (
              <ChannelPill channel={row.channel} t={tc} compact={!!ORIGIN_ICONS[row.origin]} />
            )}
            <span className="simple-thread-title">{title}</span>
            {row.streaming && (
              <span className="simple-thread-dot" title={t("rail.working")} aria-label={t("rail.working")} />
            )}
            {row.unread > 0 && (
              <span className="simple-count is-sm" title={t("rail.unread", { n: String(row.unread) })}>
                {row.unread}
              </span>
            )}
          </button>
          {row.threadId && (
            <button
              type="button"
              className="simple-thread-del"
              aria-label={t("rail.deleteChat")}
              title={t("rail.deleteChat")}
              onClick={() => onDeleteThread(row.threadId as string)}
            >
              <Trash2 size={13} strokeWidth={1.75} />
            </button>
          )}
        </div>
      </li>
    );
  }

  function renderBot(bot: Agent, index: number, section: BotSection) {
    const selected = bot.name === agent && activeGroupId === null;
    const badge = badges.get(bot.name);
    const preview = previews.get(bot.name);
    const isPinned = pinned.has(bot.name);
    const state = presenceFor(bot.name);
    const asleep = isAsleep(state);
    // An amber or red pip has to explain itself somewhere a phone can read
    // (there is no hover there), so for those states the status IS the line.
    const flagged = state !== "online" && !asleep;
    const stateText = state === "online" ? "" : presenceText(tp, state, bot);
    // What the muted line says, in priority order: what the bot is doing right
    // now, then why it cannot answer, then the last thing said, then who it
    // is. Never nothing.
    const sub = badge?.busy
      ? t("rail.working")
      : flagged
        ? stateText
        : preview?.text || bot.description || t("rail.noDescription");
    const stamp = preview?.ts ? railTime(preview.ts, nowTick, lang, t) : "";
    const threadsOpen = selected && !threadsHidden && !collapsed;
    // On a wide viewport the selected row doubles as the fold: the second
    // click collapses the conversations, the third brings them back. Not in
    // the drawer, where a tap on any row — the selected one included — goes to
    // that bot's conversation and closes the drawer (folding is the chevron's
    // job there, so a tap never leaves the drawer open over a conversation the
    // reader just asked for); and not while a More panel holds the centre,
    // where the click means "back to the chat".
    const rowFolds = selected && !asDrawer && !panelOpen;
    // The insertion line, drawn as a pseudo-element on the row it would land
    // above (or, for the very last slot, below).
    const dropping = drop?.section === section;
    const li = [
      "simple-bot",
      selected ? " is-active" : "",
      asleep ? " is-asleep" : "",
      dragName === bot.name ? " is-dragging" : "",
      dropping && drop?.index === index ? " is-drop-before" : "",
      dropping && drop?.index === index + 1 ? " is-drop-after" : "",
    ].join("");
    const dragProps = canReorder
      ? {
          draggable: true,
          onDragStart: (e: React.DragEvent) => beginDrag(e, bot.name),
          onDragEnd: endDrag,
          onDragOver: (e: React.DragEvent) => hoverRow(e, section, index),
          onDrop: applyDrop,
        }
      : {};

    // Collapsed: the face is the whole row. Name, preview, stamp, pin and the
    // unfolded chats all go — what is left is exactly what a person needs to
    // find the right bot in a strip 76px wide, plus the online pip that was
    // always on the mark. The name lives on the tooltip and on aria-label, so
    // neither a mouse nor a screen reader loses it.
    if (collapsed) {
      return (
        <li key={bot.name} className={li} data-presence={state} {...dragProps}>
          <div
            className="simple-row-wrap"
            onContextMenu={(e) => {
              e.preventDefault();
              setMenu({ kind: "bot", x: e.clientX, y: e.clientY, bot: bot.name });
            }}
          >
            <button
              type="button"
              className="simple-row is-mini"
              aria-current={selected ? "true" : undefined}
              aria-label={stateText ? `${bot.name}. ${stateText}` : bot.name}
              title={`${bot.name}: ${asleep ? stateText : sub}`}
              onClick={() => onSelectBot(bot.name)}
            >
              <BotMark
                name={bot.name}
                avatarUrl={bot.avatar_url}
                emoji={bot.emoji}
                size={38}
                tone={pipTone(state) ?? "off"}
              />
              {!!badge?.unread && (
                <span
                  className="simple-count is-corner"
                  title={t("rail.unread", { n: String(badge.unread) })}
                >
                  {badge.unread}
                </span>
              )}
            </button>
          </div>
        </li>
      );
    }

    const foldTitle = rowFolds ? t(threadsOpen ? "rail.foldChats" : "rail.unfoldChats") : "";
    const rowTitle = [foldTitle, stateText].filter(Boolean).join("\n") || undefined;
    return (
      <li key={bot.name} className={li} data-presence={state} {...dragProps}>
        <div
          className="simple-row-wrap"
          onContextMenu={(e) => {
            e.preventDefault();
            setMenu({ kind: "bot", x: e.clientX, y: e.clientY, bot: bot.name });
          }}
        >
          <button
            type="button"
            className="simple-row"
            aria-current={selected ? "true" : undefined}
            aria-expanded={rowFolds ? threadsOpen : undefined}
            aria-description={stateText || undefined}
            title={rowTitle}
            onClick={() => {
              if (rowFolds) setThreadsHidden((h) => !h);
              else onSelectBot(bot.name);
            }}
          >
            <BotMark
              name={bot.name}
              avatarUrl={bot.avatar_url}
              emoji={bot.emoji}
              size={40}
              tone={pipTone(state) ?? "off"}
            />
            <span className="simple-row-text">
              <span className="simple-row-top">
                <span className="simple-row-name">{bot.name}</span>
                {isPinned && (
                  <Pin className="simple-row-pinned" size={11} strokeWidth={2} aria-hidden />
                )}
                {!!stamp && <span className="simple-row-time">{stamp}</span>}
              </span>
              <span className="simple-row-sub">
                {badge?.busy && <span className="simple-row-live" aria-hidden />}
                {/* The text is its own element: `text-overflow: ellipsis` has no
                    effect on the anonymous text of a flex container, so the
                    preview would otherwise be chopped mid-word with no "…". */}
                <span className="simple-row-subtext">{sub}</span>
              </span>
            </span>
            {!!badge?.unread && (
              <span className="simple-count" title={t("rail.unread", { n: String(badge.unread) })}>
                {badge.unread}
              </span>
            )}
          </button>
          {/* The fold, as its own control: points right while the chats are
              tucked away, down while they are open — the disclosure grammar
              every file tree already taught. Always on the selected row, on
              hover elsewhere, and on every row where there is no hover: a
              phone's drawer is otherwise a list of taps that each leave it.
              On another bot it selects that bot and unfolds its chats in
              place, so one agent's conversations can be browsed without going
              to the chat first. A sibling of the row, not a child — a button
              cannot nest inside a button. */}
          <button
            type="button"
            className={`simple-row-chev${threadsOpen ? " is-open" : ""}`}
            aria-expanded={selected ? threadsOpen : false}
            aria-label={
              selected
                ? t(threadsOpen ? "rail.foldChats" : "rail.unfoldChats")
                : t("rail.expandBot", { name: bot.name })
            }
            title={
              selected
                ? t(threadsOpen ? "rail.foldChats" : "rail.unfoldChats")
                : t("rail.expandBot", { name: bot.name })
            }
            data-testid="simple-bot-chevron"
            onClick={() => {
              if (selected) setThreadsHidden((h) => !h);
              else onExpandBot(bot.name);
            }}
          >
            <ChevronDown size={15} strokeWidth={2} aria-hidden />
          </button>
          <button
            type="button"
            className="simple-pin"
            aria-pressed={isPinned}
            aria-label={
              isPinned ? t("rail.unpin", { name: bot.name }) : t("rail.pin", { name: bot.name })
            }
            title={isPinned ? t("rail.unpin", { name: bot.name }) : t("rail.pin", { name: bot.name })}
            onClick={() => togglePin(bot.name)}
          >
            {isPinned ? <PinOff size={13} strokeWidth={1.75} /> : <Pin size={13} strokeWidth={1.75} />}
          </button>
        </div>

        {/* The selected bot unfolds into its conversations — threads live
            inside a bot, never beside it. */}
        {threadsOpen && (
          <div className="simple-threads">
            <button type="button" className="simple-newchat" onClick={onNewChat}>
              <MessageSquarePlus size={14} strokeWidth={1.75} /> {t("rail.newChat")}
            </button>
            <ThreadFilterChips options={filterOptions} value={threadFilter} t={tc} onChange={onThreadFilter} />
            {rows.length === 0 ? (
              <p className="simple-threads-empty muted">{t("rail.noThreads")}</p>
            ) : filteredRows.length === 0 ? (
              <p className="simple-threads-empty muted">{t("rail.filterEmpty")}</p>
            ) : (
              <ul className="simple-thread-list" title={t("rail.renameHint")}>
                {shownRows.map(renderThread)}
              </ul>
            )}
            {hiddenCount > 0 && (
              <button
                type="button"
                className="simple-showall"
                title={t("rail.showAllHelp")}
                onClick={() => onShowAllThreads(bot.name)}
              >
                <History size={13} strokeWidth={1.75} />
                {t("rail.showAll", { n: String(filteredRows.length) })}
              </button>
            )}
          </div>
        )}
      </li>
    );
  }

  /** "N offline": opens and closes the All agents tail. */
  function renderAsleepToggle() {
    const n = String(bands.asleep.length);
    return (
      <li key="__asleep" className="simple-asleep-item">
        <button
          type="button"
          className={`simple-asleep-toggle${showAsleep ? " is-open" : ""}${collapsed ? " is-mini" : ""}`}
          aria-expanded={showAsleep}
          aria-label={showAsleep ? tp("presence.asleepHide", { n }) : tp("presence.asleepShow", { n })}
          title={tp("presence.asleepHelp")}
          data-testid="simple-offline-toggle"
          onClick={toggleOffline}
        >
          <ChevronDown className="simple-asleep-caret" size={12} strokeWidth={2.25} aria-hidden />
          {/* Collapsed the strip is 76px wide: the count alone, the words on
              the tooltip and the aria-label. */}
          <span>{collapsed ? n : tp("presence.asleepCount", { n })}</span>
        </button>
      </li>
    );
  }

  // A group is a row of exactly the same shape as a bot's — same height, same
  // rhythm — wearing a stacked mark instead of a single bead. That is the whole
  // visual difference, and it is enough: a person reads "several bots" without
  // being told.
  function renderGroup(group: GroupSummary) {
    const selected = group.id === activeGroupId;
    const preview = groupPreviews.get(group.id);
    const sub = preview?.text || group.members.join(", ");
    const stamp = preview?.ts ? railTime(preview.ts, nowTick, lang, t) : "";
    const key = `g:${group.id}`;
    if (collapsed) {
      return (
        <li key={group.id} className={`simple-bot${selected ? " is-active" : ""}`}>
          <div
            className="simple-row-wrap"
            onContextMenu={(e) => {
              e.preventDefault();
              setMenu({ kind: "group", x: e.clientX, y: e.clientY, group });
            }}
          >
            <button
              type="button"
              className="simple-row is-mini"
              aria-current={selected ? "true" : undefined}
              aria-label={t("groups.open", { name: group.name })}
              title={`${group.name} — ${group.members.join(", ")}`}
              onClick={() => onSelectGroup(group.id)}
            >
              <GroupMark members={group.members} agentsByName={agentsByName} size={38} />
            </button>
          </div>
        </li>
      );
    }
    if (renaming === key) {
      return (
        <li key={group.id} className="simple-bot">
          <div className="simple-row-wrap is-renaming">
            {renameInput(key, group.name, t("groups.renamePlaceholder"))}
          </div>
        </li>
      );
    }
    return (
      <li key={group.id} className={`simple-bot${selected ? " is-active" : ""}`}>
        <div
          className="simple-row-wrap"
          onContextMenu={(e) => {
            e.preventDefault();
            setMenu({ kind: "group", x: e.clientX, y: e.clientY, group });
          }}
        >
          <button
            type="button"
            className="simple-row"
            aria-current={selected ? "true" : undefined}
            aria-label={t("groups.open", { name: group.name })}
            onClick={() => onSelectGroup(group.id)}
          >
            <GroupMark members={group.members} agentsByName={agentsByName} size={40} />
            <span className="simple-row-text">
              <span className="simple-row-top">
                <span className="simple-row-name">{group.name}</span>
                {!!me && group.created_by !== me.id && (group.shared_with ?? []).includes(me.id) && (
                  <span className="simple-row-tag" title={t("groups.sharedTagHelp")}>
                    {t("groups.sharedTag")}
                  </span>
                )}
                {!!stamp && <span className="simple-row-time">{stamp}</span>}
              </span>
              <span className="simple-row-sub">
                <span className="simple-row-subtext">{sub}</span>
              </span>
            </span>
          </button>
        </div>
      </li>
    );
  }

  // The right-click menu, drawn over everything at the pointer. Each entry is
  // something the row can already do (or one hover away from doing) — the menu
  // is a shortcut, never the only door.
  function renderMenu() {
    if (!menu) return null;
    const run = (fn: () => void) => () => {
      setMenu(null);
      fn();
    };
    // Clamp so the menu never opens half off-screen.
    const left = Math.min(menu.x, window.innerWidth - 230);
    const top = Math.min(menu.y, window.innerHeight - 230);
    const shell = (label: string, children: React.ReactNode) => (
      <div
        ref={menuRef}
        className="menu simple-bot-menu"
        role="menu"
        aria-label={label}
        style={{ left, top }}
      >
        {children}
      </div>
    );

    if (menu.kind === "panel") {
      const { to, section } = menu;
      const panel = panels.find((p) => p.to === to);
      if (!panel) return null;
      // Dragging is the fast way; this is the only way on a phone, which has no
      // drag gesture at all, and from a keyboard.
      const move = (into: NavSection) => () => {
        const next = placeItem(panelBase(), to, into, null);
        setPanelSplit(next);
        writePanelSplit(next);
      };
      return shell(
        t(panel.key),
        section === "more" ? (
          <button type="button" className="menu-item" role="menuitem" onClick={run(move("primary"))}>
            <ArrowUp size={14} strokeWidth={1.75} /> {t("more.moveOut")}
          </button>
        ) : (
          <button type="button" className="menu-item" role="menuitem" onClick={run(move("more"))}>
            <ArrowDown size={14} strokeWidth={1.75} /> {t("more.moveIn")}
          </button>
        ),
      );
    }

    if (menu.kind === "rail") {
      return shell(
        t("menu.railLabel"),
        <>
          {canCreateBots && (
            <button type="button" className="menu-item" role="menuitem" onClick={run(onNewBot)}>
              <PlusIcon size={14} /> {t("rail.newBot")}
            </button>
          )}
          {groupsEnabled && (
            <button type="button" className="menu-item" role="menuitem" onClick={run(onNewGroup)}>
              <UsersRound size={14} strokeWidth={1.75} /> {t("groups.new")}
            </button>
          )}
        </>,
      );
    }

    if (menu.kind === "thread") {
      const row = menu.row;
      const title = row.title || t("rail.chatFallback");
      return shell(
        t("menu.threadLabel", { name: title }),
        <>
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            onClick={run(() => startRename(`t:${row.threadId}`, row.title))}
          >
            <Pencil size={14} strokeWidth={1.75} /> {t("menu.rename")}
          </button>
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            onClick={run(() => onOpenThread(row.threadId as string))}
          >
            <History size={14} strokeWidth={1.75} /> {t("menu.openChat")}
          </button>
          <button
            type="button"
            className="menu-item danger"
            role="menuitem"
            onClick={run(() => onDeleteThread(row.threadId as string))}
          >
            <Trash2 size={14} strokeWidth={1.75} /> {t("rail.deleteChat")}
          </button>
        </>,
      );
    }

    if (menu.kind === "group") {
      const group = menu.group;
      return shell(
        t("menu.groupLabel", { name: group.name }),
        <>
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            onClick={run(() => startRename(`g:${group.id}`, group.name))}
          >
            <Pencil size={14} strokeWidth={1.75} /> {t("menu.rename")}
          </button>
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            onClick={run(() => onSelectGroup(group.id))}
          >
            <UsersRound size={14} strokeWidth={1.75} /> {t("menu.openGroup")}
          </button>
        </>,
      );
    }

    const bot = agents.find((a) => a.name === menu.bot);
    if (!bot) return null;
    const selected = bot.name === agent && activeGroupId === null;
    const isPinned = pinned.has(bot.name);
    const unread = badges.get(bot.name)?.unread ?? 0;
    return shell(
      t("menu.label", { name: bot.name }),
      <>
        <button type="button" className="menu-item" role="menuitem" onClick={run(() => onNewChatFor(bot.name))}>
          <MessageSquarePlus size={14} strokeWidth={1.75} /> {t("menu.newChat")}
        </button>
        <button type="button" className="menu-item" role="menuitem" onClick={run(() => onShowAllThreads(bot.name))}>
          <History size={14} strokeWidth={1.75} /> {t("menu.allChats")}
        </button>
        <button type="button" className="menu-item" role="menuitem" onClick={run(() => onShowProfile(bot.name))}>
          <Info size={14} strokeWidth={1.75} /> {t("menu.profile")}
        </button>
        <button type="button" className="menu-item" role="menuitem" onClick={run(() => togglePin(bot.name))}>
          {isPinned ? <PinOff size={14} strokeWidth={1.75} /> : <Pin size={14} strokeWidth={1.75} />}
          {isPinned ? t("rail.unpinShort") : t("rail.pinShort")}
        </button>
        {/* The pointer-free half of drag-to-rearrange. Dragging is the fast
            way; these two are the only way on a phone or from a keyboard, so
            they are not a nicety. Hidden while a search filter is on for the
            same reason a drop is refused there: the order they write covers
            bots the filter is hiding. */}
        {!query.trim() && (
          <>
            <button
              type="button"
              className="menu-item"
              role="menuitem"
              onClick={run(() => nudge(bot.name, -1))}
            >
              <ArrowUp size={14} strokeWidth={1.75} /> {t("menu.moveUp")}
            </button>
            <button
              type="button"
              className="menu-item"
              role="menuitem"
              onClick={run(() => nudge(bot.name, 1))}
            >
              <ArrowDown size={14} strokeWidth={1.75} /> {t("menu.moveDown")}
            </button>
          </>
        )}
        {unread > 0 && (
          <button type="button" className="menu-item" role="menuitem" onClick={run(() => onMarkRead(bot.name))}>
            <BellOff size={14} strokeWidth={1.75} /> {t("menu.markRead", { n: String(unread) })}
          </button>
        )}
        {selected && (
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            onClick={run(() => setThreadsHidden((h) => !h))}
          >
            <ChevronDown size={14} strokeWidth={1.75} />
            {threadsHidden ? t("menu.showChats") : t("menu.hideChats")}
          </button>
        )}
      </>,
    );
  }

  // ── section header ─────────────────────────────────────────────────────────
  // The divider IS the control: click it to fold the section away. It keeps its
  // label in the collapsed rail (wrapping to two lines if the language needs
  // it) because a strip of faces with no dividers is a pile, not a list.
  function sectionHead(id: SectionId, label: string, count: number, icon?: React.ReactNode) {
    const open = !folded.has(id);
    return (
      <button
        type="button"
        className={`simple-section simple-section-btn${open ? " is-open" : ""}`}
        aria-expanded={open}
        title={open ? t("rail.foldSection", { name: label }) : t("rail.unfoldSection", { name: label })}
        onClick={() => toggleSection(id)}
      >
        <ChevronDown className="simple-section-caret" size={11} strokeWidth={2.5} aria-hidden />
        {icon}
        <span className="simple-section-label">{label}</span>
        <span className="simple-section-count">{count}</span>
      </button>
    );
  }

  // ── More ───────────────────────────────────────────────────────────────────
  // The dashboard panels this shell also offers (see ./panels.tsx), in the two
  // drag-and-drop groups the console's own nav uses: an always-visible group and
  // the collapsible More below it. Rendered as NavLinks rather than buttons
  // because the panel selection IS the URL — that is what makes Back work, a
  // reload keep the panel, and a middle-click open it in a new tab the way every
  // other link in the app does.

  /** The split being previewed mid-drag, or the saved one. */
  const panelBase = (): NavSplit =>
    panelWork ?? {
      primary: shownPanels.primary.map((p) => p.to),
      more: shownPanels.more.map((p) => p.to),
    };

  function startPanelDrag(e: React.DragEvent, to: string) {
    setDragPanel(to);
    setPanelWork(panelBase());
    e.dataTransfer.effectAllowed = "move";
    // Firefox refuses to start a drag with no data set.
    e.dataTransfer.setData("text/plain", to);
    e.stopPropagation();
  }

  // Over a row: drop just before it. stopPropagation keeps the surrounding zone
  // from also firing, which would append instead.
  function panelOverItem(e: React.DragEvent, overTo: string, section: NavSection) {
    if (!dragPanel) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = "move";
    if (overTo === dragPanel) return;
    setPanelWork((cur) => placeItem(cur ?? panelBase(), dragPanel, section, overTo));
  }

  // Over a group's own padding (or an empty group): append to it.
  function panelOverZone(e: React.DragEvent, section: NavSection) {
    if (!dragPanel) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setPanelWork((cur) => placeItem(cur ?? panelBase(), dragPanel, section, null));
  }

  // A completed drop persists both groups. `drop` fires before `dragend`, and is
  // the only place that persists — so a cancelled drag (Escape, released outside
  // the rail) skips it, hits `dragend` alone, and reverts to what was saved.
  function panelDrop(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (panelWork && dragPanel) {
      setPanelSplit(panelWork);
      writePanelSplit(panelWork);
    }
    endPanelDrag();
  }

  function endPanelDrag() {
    setDragPanel(null);
    setPanelWork(null);
  }

  function panelRow(panel: SimplePanel, section: NavSection) {
    const { to, key, Icon } = panel;
    return (
      <li key={to}>
        <NavLink
          to={to}
          draggable={!collapsed}
          className={({ isActive }) =>
            `simple-more-item${isActive ? " is-active" : ""}${dragPanel === to ? " is-dragging" : ""}`
          }
          // Collapsed the label is gone, so the tooltip carries it — the same
          // trade the bot rows make in the strip.
          title={collapsed ? t(key) : undefined}
          aria-label={collapsed ? t(key) : undefined}
          onClick={onNavigatePanel}
          onContextMenu={(e) => {
            // The pointer-free half of the same gesture. A phone cannot drag at
            // all, so without this the two groups would be a desktop-only idea.
            e.preventDefault();
            setMenu({ kind: "panel", x: e.clientX, y: e.clientY, to, section });
          }}
          onDragStart={(e) => startPanelDrag(e, to)}
          onDragOver={(e) => panelOverItem(e, to, section)}
          onDrop={panelDrop}
          onDragEnd={endPanelDrag}
        >
          <Icon size={16} strokeWidth={1.75} aria-hidden />
          {!collapsed && <span className="simple-more-label">{t(key)}</span>}
        </NavLink>
      </li>
    );
  }

  function renderMore() {
    if (panels.length === 0) return null;
    // A drag forces More open even when it is collapsed, so a row can be pulled
    // out of it — and pushed back in — without a second gesture first.
    const showMore = moreOpen || dragPanel != null;
    return (
      <>
        {/* A rule, not a heading: everything above is a conversation, everything
            below is a place. The dashboard says the same thing by putting More
            at the foot of its own nav. */}
        <div className="simple-rail-divider" role="presentation" />
        {/* The group a human has dragged panels out into. Empty by default, and
            then it is only a drop target — it draws no heading of its own,
            because a heading over nothing is worse than the divider alone. */}
        <ul
          className="simple-more-list simple-more-primary"
          onDragOver={(e) => panelOverZone(e, "primary")}
          onDrop={panelDrop}
        >
          {shownPanels.primary.map((p) => panelRow(p, "primary"))}
          {shownPanels.primary.length === 0 && dragPanel && (
            <li className="simple-more-dropzone" aria-hidden />
          )}
        </ul>
        <button
          type="button"
          className={`simple-section simple-section-btn${showMore ? " is-open" : ""}`}
          aria-expanded={showMore}
          title={showMore ? t("rail.foldSection", { name: t("nav.more") }) : t("rail.unfoldSection", { name: t("nav.more") })}
          onClick={() => {
            setMoreOpen((open) => {
              writeMoreOpen(!open);
              return !open;
            });
          }}
        >
          <ChevronDown className="simple-section-caret" size={11} strokeWidth={2.5} aria-hidden />
          {/* No count beside the label, unlike the bot sections above: those
              stand over a list that can be any length, this one over five fixed
              rows — and a count in there would put "More 5" in the heading's
              accessible name for the sake of a number nobody needs. */}
          <span className="simple-section-label">{t("nav.more")}</span>
        </button>
        {showMore && (
          <ul
            className="simple-more-list simple-more-group"
            onDragOver={(e) => panelOverZone(e, "more")}
            onDrop={panelDrop}
          >
            {shownPanels.more.map((p) => panelRow(p, "more"))}
            {shownPanels.more.length === 0 && dragPanel && (
              <li className="simple-more-dropzone" aria-hidden />
            )}
          </ul>
        )}
      </>
    );
  }

  return (
    <aside
      ref={panelRef}
      className={`simple-rail${asDrawer ? " is-drawer" : collapsed ? " is-collapsed" : ""}${
        dragName ? " is-reordering" : ""
      }`}
      aria-label={t("rail.title")}
      // A closed drawer is parked off-canvas rather than unmounted (so it can
      // slide), which would otherwise leave every bot in the tab order and in
      // the accessibility tree behind the conversation. `inert` takes the whole
      // subtree out of both for exactly as long as it is off screen.
      inert={asDrawer && !drawerOpen}
      data-simple-bot-rail
      data-testid="simple-rail"
    >
      <div className="simple-rail-head">
        {!collapsed && <strong className="simple-rail-title">{t("rail.title")}</strong>}
        <div className="simple-rail-head-actions">
          {/* The same bell the dashboard carries, and the same store behind it.
              Anything that needs a human — a channel that failed to start, a
              connector asking to be signed in, an approval — announced itself
              only in the console before this, which is precisely the shell the
              people it is asking are not looking at. */}
          <NotificationBell />
          {/* The collapse toggle. Absent in drawer mode, where the drawer itself
              is the compact form and this would only offer a second one. */}
          {!asDrawer && (
            <button
              type="button"
              className="simple-rail-collapse"
              aria-expanded={!collapsed}
              aria-label={collapsed ? t("rail.expand") : t("rail.collapse")}
              title={collapsed ? t("rail.expandHelp") : t("rail.collapseHelp")}
              onClick={onToggleCollapsed}
            >
              {collapsed ? (
                <PanelLeftOpen size={16} strokeWidth={1.9} />
              ) : (
                <PanelLeftClose size={16} strokeWidth={1.9} />
              )}
            </button>
          )}
          {asDrawer && (
            <button
              type="button"
              className="btn ghost xs"
              onClick={onClose}
              aria-label={t("rail.close")}
              title={t("rail.close")}
            >
              <X size={15} strokeWidth={2} />
            </button>
          )}
        </div>
      </div>

      {/* Search, the hints and the full "New bot" button all belong to the open
          rail. Collapsed they are not merely hidden by CSS but unrendered, so
          they cannot be reached by Tab from a strip that does not show them. */}
      {!collapsed && (
        <>
          <div className="simple-search">
            <Search size={14} strokeWidth={1.75} aria-hidden />
            <input
              className="simple-search-input"
              type="search"
              value={query}
              placeholder={t("rail.searchPlaceholder")}
              aria-label={t("rail.searchLabel")}
              title={t("rail.searchHelp")}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        </>
      )}

      {canCreateBots &&
        (collapsed ? (
          <button
            type="button"
            className="btn primary simple-newbot is-mini"
            onClick={onNewBot}
            aria-label={t("rail.newBot")}
            title={t("rail.newBotHelp")}
          >
            <PlusIcon size={16} />
          </button>
        ) : (
          <button
            type="button"
            className="btn primary simple-newbot"
            onClick={onNewBot}
            title={t("rail.newBotHelp")}
          >
            <PlusIcon size={15} /> {t("rail.newBot")}
          </button>
        ))}

      <div
        className="simple-rail-scroll"
        ref={scrollRef}
        onKeyDown={onRailKeyDown}
        // The list's background. A row's own handler runs first and calls
        // preventDefault, so `defaultPrevented` is the signal that this event
        // has already been claimed — without it the bubble would land here and
        // replace the row menu the user actually asked for.
        onContextMenu={(e) => {
          if (e.defaultPrevented) return;
          if (!canCreateBots && !groupsEnabled) return; // nothing to offer
          e.preventDefault();
          setMenu({ kind: "rail", x: e.clientX, y: e.clientY });
        }}
      >
        {loading && <div className="spinner simple-rail-spinner" aria-label={t("rail.loading")} />}
        {empty && <p className="simple-rail-empty muted">{t("rail.empty")}</p>}
        {noMatches && <p className="simple-rail-empty muted">{t("rail.noMatches", { q: query.trim() })}</p>}

        {/* Both bot sections are shown whenever there are bots at all, EMPTY OR
            NOT, and that symmetry is the point: each one is the other's drop
            target. A "Pinned" heading that appears only once something is
            pinned is a target you cannot aim at, and — the way this actually
            broke — an "All bots" heading that disappears when the last bot is
            pinned leaves that bot with no section to be dragged back out to.
            The empty state is offered only while a drop is possible; with a
            search filter on, reordering is off and a dashed invitation would
            be promising a gesture the rail refuses. */}
        {!empty && !loading && (
          <>
            {sectionHead("pinned", t("rail.pinned"), visible.top.length)}
            {!folded.has("pinned") && (
              <ul
                className="simple-bot-list is-droppable"
                onDragOver={(e) => hoverSection(e, "pinned", visible.top.length)}
                onDrop={applyDrop}
              >
                {visible.top.map((bot, i) => renderBot(bot, i, "pinned"))}
                {visible.top.length === 0 && canReorder && !collapsed && (
                  <li className="simple-drop-empty muted">{t("rail.pinnedEmpty")}</li>
                )}
              </ul>
            )}
            {sectionHead("all", t("rail.all"), visible.rest.length)}
            {!folded.has("all") && (
              <ul
                className="simple-bot-list is-droppable"
                onDragOver={(e) => hoverSection(e, "all", restShown.length)}
                onDrop={applyDrop}
              >
                {/* The bots that can answer (or need someone to look), then
                    the paused and stopped ones behind one "N offline" row,
                    closed by default (presence.ts). Indexes run on across the
                    two so a drop lands where it is drawn. */}
                {bands.awake.map((bot, i) => renderBot(bot, i, "all"))}
                {asleepToggle && renderAsleepToggle()}
                {showAsleep && bands.asleep.map((bot, i) => renderBot(bot, bands.awake.length + i, "all"))}
                {visible.rest.length === 0 && canReorder && !collapsed && (
                  <li className="simple-drop-empty muted">{t("rail.allEmpty")}</li>
                )}
              </ul>
            )}
          </>
        )}

        {/* Groups sit in their own labelled section BELOW the bots: a group is
            something you assemble out of the list above it, so it reads as a
            second tier rather than as a stranger among the contacts. */}
        {groupsEnabled && (
          <>
            {sectionHead(
              "groups",
              t("groups.section"),
              visibleGroups.length,
              <UsersRound className="simple-section-icon" size={12} strokeWidth={1.75} aria-hidden />,
            )}
            {!folded.has("groups") && (
              <>
                {visibleGroups.length > 0 ? (
                  // The rename affordance has no visible control on a row, so
                  // the list says where it is. (A phone has no right-click at
                  // all — there the group's own info panel carries a pencil.)
                  <ul className="simple-bot-list" title={collapsed ? undefined : t("rail.renameHint")}>
                    {visibleGroups.map(renderGroup)}
                  </ul>
                ) : (
                  !query.trim() &&
                  !collapsed && <p className="simple-rail-empty muted">{t("groups.empty")}</p>
                )}
                <button
                  type="button"
                  className={collapsed ? "simple-newgroup is-mini" : "simple-newgroup"}
                  onClick={onNewGroup}
                  aria-label={collapsed ? t("groups.new") : undefined}
                  title={t("groups.newHelp")}
                >
                  <PlusIcon size={14} />
                  {!collapsed && t("groups.new")}
                </button>
              </>
            )}
          </>
        )}

        {renderMore()}
      </div>

      <AccountMenu collapsed={collapsed} />

      {renderMenu()}

      {/* Drag the rail's edge to taste; double-click puts it back. Only on a
          wide viewport — the drawer already sizes itself to the phone — and
          not while collapsed, where the width is the strip's, not the
          reader's. */}
      {!asDrawer && !collapsed && (
        <div
          className={`simple-rail-resize${resizing ? " is-active" : ""}`}
          role="separator"
          aria-orientation="vertical"
          title={t("rail.resize")}
          onMouseDown={startResize}
          onDoubleClick={() => onRailWidth(null)}
        />
      )}
    </aside>
  );
}

// How wide the account menu opens.
const MENU_MIN_W = 240;

// The account menu — theme, language and (on a multi-user install) sign out.
// The same three things the dashboard's profile button offers, minus every
// operator affordance.
function AccountMenu({ collapsed }: { collapsed: boolean }) {
  const t = useT(STR);
  const { user, multiUser, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  // The menu is portaled to <body> and positioned from the button's rect,
  // because the collapsed rail is `overflow: hidden` on a 78px strip: rendered
  // in place, a 240px menu was sliced off at the strip's edge and the user saw
  // a column of half-words with no way to reach Sign out. Fixed positioning is
  // the only anchor that survives that clip, and it costs nothing in the
  // expanded state, where the menu simply matches the button's width.
  const { rect: pos, measure: place } = useAnchor(btnRef, open, "above", MENU_MIN_W);

  useEffect(() => {
    if (!open) return;
    // Click-away close. The menu lives outside this component's wrapper now,
    // so it has to be excluded explicitly or the first click inside it closes.
    function onDoc(e: MouseEvent) {
      const target = e.target as Node;
      if (wrapRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Re-anchor when the rail folds while the menu is open.
  useEffect(() => {
    if (open) place();
  }, [collapsed, open, place]);

  const label = user?.name || user?.email || t("account.fallback");

  return (
    <div className="simple-account" ref={wrapRef}>
      <button
        type="button"
        ref={btnRef}
        className="simple-account-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("account.menu")}
        title={label}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="avatar-sm">{initials(label)}</span>
        {/* Collapsed the initials disc is the whole button — the name would
            not fit, and it is already on the tooltip and the aria-label. */}
        {!collapsed && <span className="simple-account-name">{label}</span>}
      </button>

      {open &&
        pos &&
        createPortal(
          <div
            className="menu simple-account-menu"
            role="menu"
            ref={menuRef}
            style={{ position: "fixed", left: pos.left, bottom: pos.bottom, width: pos.width }}
          >
            <div className="menu-head">
              <div style={{ fontWeight: 600 }}>{label}</div>
              {!!(user?.title || "").trim() && <div style={{ fontSize: 12 }}>{user?.title}</div>}
              {!!user?.email && (
                <div className="muted" style={{ fontSize: 12 }}>
                  {user.email}
                </div>
              )}
            </div>

            {/* Two rows rather than thirteen. As lists, three themes and ten
                languages overflowed the menu's 60vh cap on a laptop and left
                Sign out parked below an invisible scroll fold — a rendered
                button nobody could find. The native <select> popups draw
                outside the menu, so nothing here needs to scroll. */}
            <ThemeSelect label={t("account.theme")} />
            <LanguageSelect label={t("account.language")} />

            {/* The way back to the dashboard. Admin-only (the component
                renders nothing otherwise), and present here for exactly the
                reason it is present there: an admin who switches into this view
                must never need another admin, a config file or SSH to leave. */}
            <UiModeSwitch onDone={() => setOpen(false)} />

            {multiUser && (
              <>
                <div className="simple-menu-sep" />
                <button
                  type="button"
                  className="menu-item"
                  role="menuitem"
                  style={{ color: "var(--danger)" }}
                  onClick={() => void logout()}
                >
                  <SignOutIcon size={15} /> {t("account.signout")}
                </button>
              </>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}
