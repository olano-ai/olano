// The sidebar's bot list — the simple shell's rail, in the dashboard's nav.
//
// A person thinks "I want to ask Merli something", so the bots are a first-
// class list beside the route tabs: PINNED, then the groups they made, then
// ALL BOTS. Clicking a bot selects it app-wide (the same choice the picker
// above drives) and opens the chat; everything else lives on the right-click
// menu, the way the simple rail does it. The menu is a shortcut, never the
// only door — pinning and grouping are also reachable from it with a
// keyboard, and every item explains itself in its tooltip.
//
// The SELECTED agent unfolds into its recent conversations (BotThreads.tsx),
// the way the simple rail's selected bot does: New chat, the source filter,
// the newest few rows, "Show all". One list, one store — the chat view's own
// history drawer reads the same rows, so the two can never disagree. A
// chevron on every row folds and unfolds it without leaving the current page,
// which on a phone is how a person browses an agent's chats from the drawer.
//
// Arrangement (pins, groups, order, folds) is per device — see
// botArrangement.ts. The roster, the current selection and the unread
// counts are the app's own (useCurrentAgent, chatStore), so this component
// owns no data of its own beyond what it draws.
//
// Two of its doors are lent out. Every row wears the simple rail's hover pin
// button, so pinning never needs the menu. And `openSectionMenu` on the ref
// lets Layout offer "New group…" from a right-click anywhere on the sidebar —
// the menu and its inline name editor stay here, where the arrangement is.
// The "New agent" button that used to sit above PINNED lives in Layout now
// (NewAgentButton.tsx), directly under the search box.

import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
  type Ref,
} from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  ArrowDown,
  ArrowUp,
  BellOff,
  ChevronDown,
  FolderInput,
  FolderPlus,
  History,
  Info,
  MessageSquarePlus,
  Pencil,
  Pin,
  PinOff,
  Trash2,
  UsersRound,
} from "lucide-react";
import { useAuth } from "../../core/auth";
import { useCurrentAgent } from "../../core/currentAgent";
import { agentDetailPath, chatPath, parseRoute } from "../../core/routeUrls";
import { useGo } from "../../core/useUrlParam";
import { useT } from "../../core/i18n";
import type { Agent } from "../../core/types";
import { ContextMenu, type MenuItem, type MenuState } from "../../ui/ContextMenu";
import { ChevronDownIcon, PlusIcon } from "../../ui/icons";
import { useToast } from "../../ui/toast";
import { BotMark, GroupMark } from "../agents/BotMark";
import { agentLabel } from "../agents/helpers";
import {
  asleepShown,
  asleepToggleShown,
  bandOf,
  isAsleep,
  pipTone,
  presenceOf,
  splitAsleep,
  type Presence,
} from "../agents/presence";
import { PRESENCE_STR, presenceText } from "../agents/presenceStrings";
import { chatStore } from "../chat/sessionStore";
import { BotThreads } from "./BotThreads";
import { NewGroupDialog } from "../groups/NewGroupDialog";
import {
  groupErrorText,
  renameGroup as renameChatGroup,
  useGroups,
  useGroupsEnabled,
  type GroupSummary,
} from "../groups/groupsStore";
import {
  createGroup,
  deleteGroup,
  homeOf,
  membersOf,
  moveTo,
  nudge,
  renameGroup,
  setOfflineOpen,
  toggleFold,
  togglePin,
  type CustomGroup,
} from "./botArrangement";
import { STR } from "./strings";
import { useBotArrangement } from "./useBotArrangement";

const ICON = { size: 14, strokeWidth: 1.75 } as const;

/**
 * One-line name editor used for both "new group" and "rename": Enter commits,
 * Escape cancels, and a blur commits too (a tap elsewhere on a phone is how
 * the keyboard goes away). `done` guards the Enter-then-blur double fire.
 */
function InlineName({
  initial,
  placeholder,
  label,
  help,
  onCommit,
  onCancel,
}: {
  initial: string;
  placeholder: string;
  label: string;
  /** Tooltip: what the name is for and how to finish. */
  help: string;
  onCommit: (value: string) => void;
  onCancel: () => void;
}) {
  const done = useRef(false);
  const settle = (fn: () => void) => {
    if (done.current) return;
    done.current = true;
    fn();
  };
  return (
    <div className="nav-group-header bot-section-header is-editing">
      <input
        className="bot-group-rename"
        autoFocus
        defaultValue={initial}
        placeholder={placeholder}
        aria-label={label}
        title={help}
        maxLength={40}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            settle(() => onCommit(e.currentTarget.value));
          } else if (e.key === "Escape") {
            e.preventDefault();
            settle(onCancel);
          }
        }}
        onBlur={(e) => settle(() => onCommit(e.currentTarget.value))}
      />
    </div>
  );
}

/** What the sidebar can ask of the list from outside it (see Layout). */
export interface BotNavHandle {
  /** Open the list's own "New group…" menu at a pointer position. */
  openSectionMenu(x: number, y: number): void;
}

export function BotNav({
  ref,
  collapsed = false,
}: {
  ref?: Ref<BotNavHandle>;
  /**
   * The sidebar is the 68px icon strip. Two things follow that CSS cannot do
   * on its own: a mark's size is a component prop (a scaled 22px disc is a
   * blurred one), and the selected agent's unfolded chat list has nowhere to
   * render, so it should not be mounted — and therefore not fetched — at all.
   */
  collapsed?: boolean;
}) {
  const t = useT(STR);
  const tp = useT(PRESENCE_STR);
  const toast = useToast();
  const navigate = useNavigate();
  const go = useGo();
  const { pathname } = useLocation();
  const { agents, agent, setAgent, loading } = useCurrentAgent();

  const [arr, update] = useBotArrangement();
  const [menu, setMenu] = useState<MenuState | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);
  /** Group id being renamed inline, if any. */
  const [renaming, setRenaming] = useState<string | null>(null);
  /** The inline "new group" row is open. */
  const [creating, setCreating] = useState(false);

  // Group CHATS — conversations several bots share (views/groups/). A
  // deployment feature: the section exists only on an explicit capability,
  // never on the tri-state's `undefined`, and `useGroups(false)` issues no
  // request at all. Not to be confused with the reader's own groups above,
  // which are folders of this list and live in this browser.
  const groupsEnabled = useGroupsEnabled() === true;
  const chats = useGroups(groupsEnabled);
  const [newGroupOpen, setNewGroupOpen] = useState(false);
  // A group somebody else shared with this reader wears a small tag, so the
  // list tells "mine" from "let in" without opening each one.
  const { user: me } = useAuth();
  const sharedWithMe = (g: GroupSummary) =>
    !!me && g.created_by !== me.id && (g.shared_with ?? []).includes(me.id);
  /** Group chat id being renamed inline, if any. */
  const [renamingChat, setRenamingChat] = useState<number | null>(null);

  // Live badges: unread replies and "thinking" per bot, from the app-level
  // chat store, so they update on every tab — streams run on while the chat
  // view is unmounted.
  const version = useSyncExternalStore(chatStore.subscribe, chatStore.getSnapshot);
  const names = useMemo(() => agents.map((a) => a.name), [agents]);
  const byName = useMemo(() => new Map(agents.map((a) => [a.name, a] as const)), [agents]);
  const stats = useMemo(() => {
    const m = new Map<string, { unread: number; busy: boolean }>();
    for (const n of names) {
      const s = chatStore.agentStats(n);
      if (s.unread || s.busy) m.set(n, s);
    }
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `version` is the store's change signal
  }, [version, names]);

  // Whether each agent can answer (views/agents/presence.ts). Status never
  // moves an agent between sections: it only decides, inside All agents,
  // whether the row sits above the "N offline" fold or behind it.
  const presence = useMemo(
    () => new Map(agents.map((a) => [a.name, presenceOf(a)] as const)),
    [agents],
  );
  const presenceFor = useCallback((n: string): Presence => presence.get(n) ?? "offline", [presence]);

  const sections = useMemo(() => {
    const all = membersOf(arr, "all", names);
    // The open agent and anything with unread replies stay above the fold:
    // the first carries its unfolded chat list, the second something unseen.
    const { awake, asleep } = splitAsleep(
      all,
      presenceFor,
      (n) => n === agent || (stats.get(n)?.unread ?? 0) > 0,
    );
    return {
      pinned: membersOf(arr, "pinned", names),
      groups: arr.groups.map((g) => ({ group: g, members: membersOf(arr, { group: g.id }, names) })),
      all,
      awake,
      asleep,
    };
  }, [arr, names, presenceFor, agent, stats]);
  const onChat = parseRoute(pathname).view === "chat";

  // The selected agent's chats are unfolded under its row unless the reader
  // tucked them away; a fold is forgotten on the next agent switch, so the
  // list you just asked for can never arrive hidden.
  const [threadsHidden, setThreadsHidden] = useState(false);
  useEffect(() => setThreadsHidden(false), [agent]);

  // ── actions ──────────────────────────────────────────────────────────────
  function newChatFor(name: string) {
    chatStore.newChat(name);
    setAgent(name, { navigate: false });
    go(chatPath(name));
  }
  function allChatsFor(name: string) {
    setAgent(name, { navigate: false });
    // The chat view reads `history=1` once and opens its thread rail.
    go(`${chatPath(name)}?history=1`);
  }
  function profileFor(name: string) {
    navigate(agentDetailPath(name));
  }
  /**
   * The chevron: fold the selected agent's chats, or select another agent and
   * unfold its. Selecting is the same app-wide choice a click on the name
   * makes: on a per-agent page (/chat/<a>) the URL is the selection, so it
   * moves to /chat/<b>; elsewhere only the remembered choice changes. Either
   * way the page key (core/routeUrls.ts shellKey) is unchanged, so on a phone
   * the drawer stays open for the reader to pick a chat from.
   */
  function toggleThreads(name: string) {
    if (name === agent) setThreadsHidden((h) => !h);
    else {
      setAgent(name);
      setThreadsHidden(false);
    }
  }

  function commitNewGroup(raw: string) {
    setCreating(false);
    if (!raw.trim()) return; // Escape, or an empty Enter: nothing to make
    const r = createGroup(arr, raw);
    if ("error" in r) {
      toast.push(t(r.error === "duplicate" ? "bots.nameDuplicate" : "bots.nameEmpty"), "error");
      return;
    }
    update(() => r.next);
  }

  async function commitChatRename(g: GroupSummary, raw: string) {
    setRenamingChat(null);
    const name = raw.trim();
    if (!name || name === g.name) return;
    try {
      await renameChatGroup(g.id, name);
    } catch (e) {
      toast.push(groupErrorText(e, t("bots.renameChatFailed")), "error");
    }
  }

  function commitRename(id: string, raw: string) {
    setRenaming(null);
    const r = renameGroup(arr, id, raw);
    if ("error" in r) {
      if (r.error === "duplicate") toast.push(t("bots.nameDuplicate"), "error");
      else if (r.error === "empty") toast.push(t("bots.nameEmpty"), "error");
      return;
    }
    update(() => r.next);
  }

  // ── menus ────────────────────────────────────────────────────────────────
  function botMenu(bot: Agent, x: number, y: number): MenuState {
    const name = bot.name;
    const home = homeOf(arr, name);
    const inGroup = home !== "pinned" && home !== "all" ? home.group : null;
    // Under All agents a step is measured inside the run the row is drawn in
    // (the awake band or the offline tail), the run the reader can see.
    const band = home === "all" ? (bandOf(sections.awake, sections.asleep, name) ?? undefined) : undefined;
    const unread = stats.get(name)?.unread ?? 0;
    const selected = name === agent;
    const items: MenuItem[] = [
      { label: t("bots.newChat"), title: t("bots.newChatHelp"), icon: <MessageSquarePlus {...ICON} />, onClick: () => newChatFor(name) },
      {
        // The keyboard/touch door to the chevron's fold: a right-click reaches
        // it on desktop, a long-press on a phone.
        label: selected && !threadsHidden ? t("bots.foldChats", { name: agentLabel(bot) }) : t("bots.unfoldChats", { name: agentLabel(bot) }),
        title: t("bots.chevHelp"),
        icon: <ChevronDown {...ICON} />,
        onClick: () => toggleThreads(name),
      },
      { label: t("bots.allChats"), title: t("bots.allChatsHelp"), icon: <History {...ICON} />, onClick: () => allChatsFor(name) },
      { label: t("bots.about"), title: t("bots.aboutHelp"), icon: <Info {...ICON} />, onClick: () => profileFor(name) },
      {
        label: home === "pinned" ? t("bots.unpin") : t("bots.pin"),
        title: home === "pinned" ? t("bots.unpinHelp") : t("bots.pinHelp"),
        icon: home === "pinned" ? <PinOff {...ICON} /> : <Pin {...ICON} />,
        separatorBefore: true,
        onClick: () => update((a) => togglePin(a, name)),
      },
      { label: t("bots.moveUp"), title: t("bots.moveHelp"), icon: <ArrowUp {...ICON} />, onClick: () => update((a) => nudge(a, name, -1, names, band)) },
      { label: t("bots.moveDown"), title: t("bots.moveHelp"), icon: <ArrowDown {...ICON} />, onClick: () => update((a) => nudge(a, name, 1, names, band)) },
    ];
    // Every group but the one it is in, then All bots when it is in one.
    const targets = arr.groups.filter((g) => g.id !== inGroup);
    targets.forEach((g, i) =>
      items.push({
        label: t("bots.moveTo", { name: g.name }),
        title: t("bots.moveToHelp"),
        icon: <FolderInput {...ICON} />,
        separatorBefore: i === 0,
        onClick: () => update((a) => moveTo(a, name, { group: g.id })),
      }),
    );
    if (inGroup) {
      items.push({
        label: t("bots.moveToAll"),
        title: t("bots.moveToHelp"),
        icon: <FolderInput {...ICON} />,
        separatorBefore: targets.length === 0,
        onClick: () => update((a) => moveTo(a, name, "all")),
      });
    }
    if (unread > 0) {
      items.push({
        label: t("bots.markRead", { n: String(unread) }),
        title: t("bots.markReadHelp"),
        icon: <BellOff {...ICON} />,
        separatorBefore: true,
        onClick: () => chatStore.clearUnread(name),
      });
    }
    return { x, y, items, label: t("bots.menuLabel", { name: agentLabel(bot) }) };
  }

  function groupMenu(g: CustomGroup, x: number, y: number): MenuState {
    const count = membersOf(arr, { group: g.id }, names).length;
    return {
      x,
      y,
      label: t("bots.groupMenuLabel", { name: g.name }),
      items: [
        { label: t("bots.rename"), title: t("bots.renameHelp"), icon: <Pencil {...ICON} />, onClick: () => setRenaming(g.id) },
        { label: t("bots.newGroup"), title: t("bots.newGroupHelp"), icon: <FolderPlus {...ICON} />, onClick: () => setCreating(true) },
        {
          label: t("bots.deleteGroup"),
          icon: <Trash2 {...ICON} />,
          danger: true,
          separatorBefore: true,
          // Greyed with the reason rather than hidden: a Delete that comes
          // and goes leaves the reader guessing what has to happen first.
          disabled: count > 0,
          title: count > 0 ? t("bots.deleteBlocked", { n: String(count) }) : t("bots.deleteHelp"),
          onClick: () =>
            update((a) => {
              const r = deleteGroup(a, g.id, names);
              return "next" in r ? r.next : a;
            }),
        },
      ],
    };
  }

  /** The reserved headers and the empty space: one door — make a group. */
  function sectionMenu(x: number, y: number): MenuState {
    return {
      x,
      y,
      label: t("bots.sectionMenuLabel"),
      items: [
        { label: t("bots.newGroup"), title: t("bots.newGroupHelp"), icon: <FolderPlus {...ICON} />, onClick: () => setCreating(true) },
        ...(groupsEnabled ? [newChatGroupItem(false)] : []),
      ],
    };
  }

  // The same menu, opened by the sidebar for a right-click that landed outside
  // this list (the brand row, the gap under More, the profile footer): the
  // whole left rail is "the agent list" to a person, so the gesture works
  // wherever it lands, as it does on the simple shell's rail.
  useImperativeHandle(ref, () => ({ openSectionMenu: (x, y) => setMenu(sectionMenu(x, y)) }));

  /** "New group chat…", greyed with the reason while there is nobody to group. */
  function newChatGroupItem(separator: boolean): MenuItem {
    const tooFew = agents.length < 2;
    return {
      label: t("bots.newChatGroup"),
      title: tooFew ? t("bots.newChatGroupNeedsTwo") : t("bots.newChatGroupHelp"),
      icon: <UsersRound {...ICON} />,
      separatorBefore: separator,
      disabled: tooFew,
      onClick: () => setNewGroupOpen(true),
    };
  }

  function chatsSectionMenu(x: number, y: number): MenuState {
    return { x, y, label: t("bots.chatsMenuLabel"), items: [newChatGroupItem(false)] };
  }

  function chatMenu(g: GroupSummary, x: number, y: number): MenuState {
    return {
      x,
      y,
      label: t("bots.chatMenuLabel", { name: g.name }),
      items: [
        { label: t("bots.openChat"), title: t("bots.openChatHelp"), icon: <UsersRound {...ICON} />, onClick: () => navigate(`/groups/${g.id}`) },
        { label: t("bots.renameChat"), title: t("bots.renameChatHelp"), icon: <Pencil {...ICON} />, onClick: () => setRenamingChat(g.id) },
        newChatGroupItem(true),
      ],
    };
  }

  // ── rows ─────────────────────────────────────────────────────────────────
  function renderBot(name: string) {
    const bot = byName.get(name);
    if (!bot) return null;
    const label = agentLabel(bot);
    const s = stats.get(name);
    const selected = name === agent;
    const active = onChat && selected;
    const pinned = homeOf(arr, name) === "pinned";
    const threadsOpen = selected && !threadsHidden && !collapsed;
    const state = presenceFor(name);
    // The tooltip says who it is, then whether it will answer. An agent that
    // answers is the baseline and says nothing extra.
    const who = bot.description ? `${label}: ${bot.description}` : t("bots.open", { name: label });
    const title = state === "online" ? who : `${who}\n${presenceText(tp, state, bot)}`;
    // The row is a link and the pin is a button, and a button may not sit
    // inside a link — so they are siblings in a wrapper, the pin floated over
    // the row's trailing edge on hover (the simple rail's `.simple-row-wrap`
    // shape). The wrapper takes the right-click so the menu opens from the
    // pin's corner too. The chevron is a third sibling: always shown on the
    // selected row (it says whether the chats below are folded), on hover for
    // the rest, and always on touch — where hover does not exist and a phone
    // needs a way to unfold an agent's chats without leaving the drawer.
    return (
      <div key={name} className={`bot-row-group${threadsOpen ? " is-open" : ""}`}>
        <div
          className={`bot-row-wrap${selected ? " is-selected" : ""}${isAsleep(state) ? " is-asleep" : ""}`}
          data-presence={state}
          onContextMenu={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setMenu(botMenu(bot, e.clientX, e.clientY));
          }}
        >
          <Link
            to={chatPath(name)}
            className={`nav-link bot-row${active ? " active" : ""}`}
            aria-current={active ? "page" : undefined}
            title={title}
            draggable={false}
            onClick={() => setAgent(name, { navigate: false })}
          >
            {/* In the strip the mark IS the row, so it takes the size the
                simple rail's collapsed faces take rather than the 22px disc
                that reads as an icon beside a name. */}
            <BotMark
              name={name}
              avatarUrl={bot.avatar_url}
              emoji={bot.emoji}
              size={collapsed ? 34 : 22}
              tone={pipTone(state) ?? "off"}
            />
            <span className="nav-label bot-row-name">{label}</span>
            {s && (
              <span className="nav-indicators">
                {s.busy && <span className="thread-dot" title={t("bots.thinking")} aria-label={t("bots.thinking")} />}
                {s.unread > 0 && (
                  <span className="thread-badge" title={t("bots.unread", { n: String(s.unread) })}>
                    {s.unread > 99 ? "99+" : s.unread}
                  </span>
                )}
              </span>
            )}
          </Link>
          <button
            type="button"
            className="bot-pin"
            aria-pressed={pinned}
            aria-label={pinned ? t("bots.unpinButton", { name: label }) : t("bots.pinButton", { name: label })}
            title={pinned ? t("bots.unpinButton", { name: label }) : t("bots.pinButton", { name: label })}
            onClick={() => update((a) => togglePin(a, name))}
          >
            {pinned ? <PinOff size={13} strokeWidth={1.75} /> : <Pin size={13} strokeWidth={1.75} />}
          </button>
          <button
            type="button"
            className={`bot-chev${threadsOpen ? " is-open" : ""}`}
            aria-expanded={threadsOpen}
            aria-label={threadsOpen ? t("bots.foldChats", { name: label }) : t("bots.unfoldChats", { name: label })}
            title={t("bots.chevHelp")}
            onClick={() => toggleThreads(name)}
            data-testid="nav-bot-chevron"
          >
            <ChevronDown size={14} strokeWidth={2} />
          </button>
        </div>
        {threadsOpen && <BotThreads agent={name} label={label} />}
      </div>
    );
  }

  function renderChat(g: GroupSummary) {
    if (renamingChat === g.id) {
      return (
        <InlineName
          key={g.id}
          initial={g.name}
          placeholder={t("bots.groupNamePlaceholder")}
          label={t("bots.renameChat")}
          help={t("bots.renameChatHelp")}
          onCommit={(v) => void commitChatRename(g, v)}
          onCancel={() => setRenamingChat(null)}
        />
      );
    }
    const to = `/groups/${g.id}`;
    const active = pathname === to;
    return (
      <Link
        key={g.id}
        to={to}
        className={`nav-link bot-row${active ? " active" : ""}`}
        aria-current={active ? "page" : undefined}
        title={t("bots.openChatTitle", { name: g.name, members: g.members.join(", ") })}
        draggable={false}
        onContextMenu={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setMenu(chatMenu(g, e.clientX, e.clientY));
        }}
      >
        <GroupMark members={g.members} agentsByName={byName} size={collapsed ? 34 : 22} />
        <span className="nav-label bot-row-name">{g.name}</span>
        {sharedWithMe(g) && (
          <span className="nav-label bot-row-tag" title={t("bots.sharedChatHelp")}>
            {t("bots.sharedChat")}
          </span>
        )}
      </Link>
    );
  }

  /** The divider IS the control: click folds, right-click offers the menu. */
  function sectionHead(
    id: string,
    label: string,
    count: number,
    menuFor: (x: number, y: number) => MenuState,
  ) {
    const open = !arr.folded.includes(id);
    return (
      <button
        type="button"
        className={`nav-group-header bot-section-header${open ? "" : " collapsed"}`}
        aria-expanded={open}
        title={`${open ? t("bots.fold", { name: label }) : t("bots.unfold", { name: label })}. ${t("bots.sectionHelp")}`}
        onClick={() => update((a) => toggleFold(a, id))}
        onContextMenu={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setMenu(menuFor(e.clientX, e.clientY));
        }}
      >
        <ChevronDownIcon className="nav-group-chev" size={15} strokeWidth={2} />
        <span className="nav-label">{label}</span>
        <span className="nav-label bot-section-count">{count}</span>
      </button>
    );
  }

  function section(id: string, label: string, members: string[], empty: ReactNode, group?: CustomGroup) {
    const open = !arr.folded.includes(id);
    return (
      <div key={id} className="bot-section" data-section={id}>
        {group && renaming === group.id ? (
          <InlineName
            initial={group.name}
            placeholder={t("bots.groupNamePlaceholder")}
            label={t("bots.rename")}
            help={t("bots.renameHelp")}
            onCommit={(v) => commitRename(group.id, v)}
            onCancel={() => setRenaming(null)}
          />
        ) : (
          sectionHead(id, label, members.length, group ? (x, y) => groupMenu(group, x, y) : sectionMenu)
        )}
        {open && (members.length > 0 ? members.map(renderBot) : <div className="bot-section-empty nav-label">{empty}</div>)}
      </div>
    );
  }

  /**
   * All agents: the agents that can answer (or need attention), then the
   * paused and not-running ones behind one "N offline" disclosure row. Closed
   * by default and remembered per device; drawn flat when nothing would be
   * left above it (presence.ts `asleepShown`).
   */
  function allSection() {
    const id = "all";
    const label = t("bots.all");
    const open = !arr.folded.includes(id);
    const { awake, asleep } = sections;
    const total = awake.length + asleep.length;
    const showTail = asleepShown({ storedOpen: arr.offlineOpen, awakeCount: awake.length, filtering: false });
    const toggle = asleepToggleShown({ asleepCount: asleep.length, awakeCount: awake.length, filtering: false });
    const n = String(asleep.length);
    return (
      <div key={id} className="bot-section" data-section={id}>
        {sectionHead(id, label, total, sectionMenu)}
        {open &&
          (total === 0 ? (
            <div className="bot-section-empty nav-label">{t("bots.allEmpty")}</div>
          ) : (
            <>
              {awake.map(renderBot)}
              {toggle && (
                <button
                  type="button"
                  className={`nav-link bot-asleep-toggle${showTail ? " is-open" : ""}`}
                  aria-expanded={showTail}
                  aria-label={showTail ? tp("presence.asleepHide", { n }) : tp("presence.asleepShow", { n })}
                  title={tp("presence.asleepHelpGroups")}
                  data-testid="nav-offline-toggle"
                  onClick={() => update((a) => setOfflineOpen(a, !showTail))}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setMenu(sectionMenu(e.clientX, e.clientY));
                  }}
                >
                  <ChevronDown className="bot-asleep-chev" size={13} strokeWidth={2} aria-hidden />
                  {/* The count stays in the 68px strip, where the words do not
                      fit; the words carry it in the open sidebar. */}
                  <span className="bot-asleep-count" aria-hidden>{n}</span>
                  <span className="nav-label bot-asleep-label">{tp("presence.asleepCount", { n })}</span>
                </button>
              )}
              {showTail && asleep.map(renderBot)}
            </>
          ))}
      </div>
    );
  }

  const none = !loading && agents.length === 0;

  // Up/Down walk the rows the way a native list does (the simple rail's rule).
  // Rows are links and buttons, so Tab already reaches them; this is the
  // ergonomic half — a long list should not cost twenty Tabs to cross, and
  // Home/End jump the ends. Section headers are skipped: they fold, they are
  // not places to go.
  const rootRef = useRef<HTMLDivElement>(null);
  function onKeyDown(e: React.KeyboardEvent) {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const root = rootRef.current;
    if (!root) return;
    if ((e.target as HTMLElement).tagName === "INPUT") return; // the name editor owns its keys
    // Agent rows only, as on the simple rail: the arrows are for moving between
    // agents, and the unfolded chat rows (Tab reaches them) would otherwise
    // put seven stops between one agent and the next.
    const items = Array.from(root.querySelectorAll<HTMLElement>(".bot-row:not(:disabled)"));
    if (items.length === 0) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
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

  return (
    <div
      ref={rootRef}
      className="nav-group nav-bots"
      data-testid="nav-bot-sections"
      onKeyDown={onKeyDown}
      onContextMenu={(e) => {
        // Empty space (padding, the gaps) — the rows and headers stop their
        // own events before they get here.
        e.preventDefault();
        setMenu(sectionMenu(e.clientX, e.clientY));
      }}
    >
      {loading && agents.length === 0 && <div className="bot-section-empty nav-label">{t("bots.loading")}</div>}
      {none && <div className="bot-section-empty nav-label">{t("bots.none")}</div>}

      {!none && (
        <>
          {section("pinned", t("bots.pinned"), sections.pinned, t("bots.pinnedEmpty"))}
          {sections.groups.map(({ group, members }) =>
            section(group.id, group.name, members, t("bots.groupEmpty", { name: group.name }), group),
          )}
          {creating && (
            <InlineName
              initial=""
              placeholder={t("bots.groupNamePlaceholder")}
              label={t("bots.newGroup")}
              help={t("bots.newGroupHelp")}
              onCommit={commitNewGroup}
              onCancel={() => setCreating(false)}
            />
          )}
          {allSection()}
        </>
      )}

      {groupsEnabled && (
        <div className="bot-section" data-section="chats">
          {sectionHead("chats", t("bots.chats"), chats.groups.length, chatsSectionMenu)}
          {!arr.folded.includes("chats") && (
            <>
              {chats.groups.map(renderChat)}
              {chats.groups.length === 0 && (
                <div className="bot-section-empty nav-label">
                  {chats.status === "loading" ? t("bots.chatsLoading") : t("bots.chatsEmpty")}
                </div>
              )}
              <button
                type="button"
                className="nav-link bot-row bot-row-new"
                onClick={() => setNewGroupOpen(true)}
                disabled={agents.length < 2}
                title={agents.length < 2 ? t("bots.newChatGroupNeedsTwo") : t("bots.newChatGroupHelp")}
                data-testid="nav-new-group-chat"
              >
                <PlusIcon size={14} />
                <span className="nav-label">{t("bots.newChatGroup")}</span>
              </button>
            </>
          )}
        </div>
      )}

      {menu && <ContextMenu menu={menu} onClose={closeMenu} />}
      {newGroupOpen && (
        <NewGroupDialog
          agents={agents}
          onClose={() => setNewGroupOpen(false)}
          onCreated={(g) => {
            setNewGroupOpen(false);
            navigate(`/groups/${g.id}`);
          }}
        />
      )}
    </div>
  );
}
