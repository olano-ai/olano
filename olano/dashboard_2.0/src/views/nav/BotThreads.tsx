// The selected agent's conversations, unfolded under its row in the sidebar —
// the simple rail's chat list, in the dashboard's nav.
//
// New chat, the source filter chips, the newest few conversations (badged by
// provenance: a Telegram row says so, a scheduled run says so, a plain
// dashboard chat is the unbadged baseline), then "Show all N chats", which
// opens the chat view's full history drawer with its search and bulk delete.
// Right-click a row for Open / Rename / Delete; the pencil-less rename is an
// inline editor in place of the title, Enter saves, Escape cancels — exactly
// the simple rail's grammar, so a person who lives in both shells learns one.
//
// Rows are links to `/chat/<agent>?thread=<id>`: a real navigation, so the mobile
// drawer closes on the tap the way it does for every other link, and a copied
// row address reopens the same conversation. The rows come from the shared
// per-agent thread store, so the chat view's drawer, this list and the simple
// rail can never show different names for the same conversation.

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { History, MessageSquarePlus, Pencil, Trash2 } from "lucide-react";
import { useI18n, useT } from "../../core/i18n";
import { ContextMenu, type MenuState } from "../../ui/ContextMenu";
import { useConfirm } from "../../ui/confirm";
import { useToast } from "../../ui/toast";
import { ChannelPill, isPlatformChannel, ORIGIN_ICONS, OriginPill } from "../chat/channels";
import { chatStore, type Session } from "../chat/sessionStore";
import { STR as CHAT_STR } from "../chat/strings";
import { chatPath, parseRoute } from "../../core/routeUrls";
import { ThreadFilterChips } from "../chat/ThreadFilterChips";
import {
  buildFilterOptions,
  filterThreadRows,
  readThreadFilter,
  writeThreadFilter,
  type ThreadFilterId,
} from "../chat/threadFilters";
import { threadListStore, useThreadList } from "../chat/threadListStore";
import { railTime } from "../simple/format";
import { STR } from "./strings";

/** How many rows the sidebar shows before "Show all" — the simple rail's cap. */
const LIMIT = 7;

const ICON = { size: 14, strokeWidth: 1.75 } as const;

interface Row {
  key: string;
  threadId: string | null;
  title: string;
  origin: string;
  channel: string;
  streaming: boolean;
  unread: number;
  sortTs: number;
}

function parseTs(iso: string | null | undefined): number {
  if (!iso) return 0;
  const n = Date.parse(iso);
  return Number.isNaN(n) ? 0 : n;
}

/** First human turn of a live session — the label for a chat with no thread yet. */
function firstUserText(s: Session): string {
  const m = s.messages.find((x) => x.role === "user" && x.body.trim());
  return m ? m.body.trim().split("\n")[0].slice(0, 80) : "";
}

export function BotThreads({ agent, label }: { agent: string; label: string }) {
  const t = useT(STR);
  const tc = useT(CHAT_STR);
  const { lang } = useI18n();
  const toast = useToast();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const onChat = parseRoute(pathname).view === "chat";

  // No failure toast from here: the chat view (and the simple shell) already
  // raise one for the same fetch, and the list is shared, so a second copy of
  // "Could not load the conversation list" would only double the noise.
  const { threads, status } = useThreadList(agent);
  const version = useSyncExternalStore(chatStore.subscribe, chatStore.getSnapshot);
  const activeKey = chatStore.activeKeyOf(agent);

  // One shared minute clock for every row's stamp.
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  // The source filter, per agent, remembered on this device (same key the
  // simple rail reads — a preference about the agent, not the shell).
  const [filter, setFilter] = useState<ThreadFilterId>(() => readThreadFilter(agent));
  useEffect(() => setFilter(readThreadFilter(agent)), [agent]);
  const changeFilter = (id: ThreadFilterId) => {
    setFilter(id);
    writeThreadFilter(agent, id);
  };

  // Backend rows merged with live sessions (streaming dot, unread count, a
  // brand-new chat still waiting for its thread id), newest first.
  const rows = useMemo<Row[]>(() => {
    const live = chatStore.sessionsFor(agent);
    const byThread = new Map<string, Session>();
    for (const s of live) if (s.threadId) byThread.set(s.threadId, s);
    const known = new Set(threads.map((th) => th.id));
    const persisted: Row[] = threads.map((th) => {
      const s = byThread.get(th.id);
      const key = s?.key ?? th.id;
      return {
        key,
        threadId: th.id,
        title: th.name || th.title || th.preview || "",
        origin: th.origin || "",
        channel: th.channel || "",
        streaming: !!s?.streaming || !!s?.activity,
        unread: key === activeKey ? 0 : (s?.unread ?? 0),
        sortTs: Math.max(parseTs(th.updated_at), s?.lastActivity ?? 0),
      };
    });
    const drafts: Row[] = live
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
        streaming: s.streaming || !!s.activity,
        unread: s.key === activeKey ? 0 : s.unread,
        sortTs: s.lastActivity,
      }));
    return [...drafts, ...persisted].sort((a, b) => b.sortTs - a.sortTs);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `version` is the store's change signal
  }, [version, threads, agent, activeKey]);

  // Counts come from the FULL list so a chip can say what it would reveal.
  const options = useMemo(() => buildFilterOptions(rows), [rows]);
  const filtered = useMemo(() => filterThreadRows(rows, filter), [rows, filter]);
  const shown = filtered.slice(0, LIMIT);
  const hidden = filtered.length - shown.length;

  // ── actions ──────────────────────────────────────────────────────────────
  function open(row: Row) {
    // Open in the store as well as by URL: the chat view's URL landing only
    // reacts to a thread id it has not seen, and it has seen this one if the
    // reader opened it, started a New chat, and now comes back to it.
    if (row.threadId) void chatStore.openThread(agent, row.threadId);
    else {
      chatStore.setActive(agent, row.key);
      chatStore.markRead(agent, row.key);
    }
  }

  function newChat() {
    chatStore.newChat(agent);
  }

  async function remove(row: Row) {
    if (!row.threadId) return;
    const ok = await confirm({
      message: tc("thread.delete.confirm"),
      confirmLabel: tc("thread.deleteBtn"),
      cancelLabel: tc("thread.cancel"),
      danger: true,
    });
    if (!ok) return;
    try {
      await threadListStore.delete(agent, row.threadId);
      // The chat view composes into the active session; deleting it leaves the
      // composer with none, so give it a draft (the store cleared the pointer).
      if (onChat) chatStore.ensureActive(agent);
    } catch {
      toast.push(tc("toast.deleteFailed"), "error");
    }
  }

  // ── inline rename ────────────────────────────────────────────────────────
  // `editingRef` mirrors `editing` so commit/cancel read the live value from
  // an input's onBlur, which fires as the field unmounts (Escape must cancel).
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const editingRef = useRef<string | null>(null);

  function startRename(row: Row) {
    if (!row.threadId) return;
    editingRef.current = row.threadId;
    setEditing(row.threadId);
    setDraft(threads.find((th) => th.id === row.threadId)?.name || "");
  }
  function cancelRename() {
    editingRef.current = null;
    setEditing(null);
    setDraft("");
  }
  async function commitRename(tid: string) {
    if (editingRef.current !== tid) return; // already settled (Escape, then blur)
    editingRef.current = null;
    setEditing(null);
    const name = draft.trim();
    const current = threads.find((th) => th.id === tid)?.name || "";
    if (name === current) return;
    try {
      await threadListStore.rename(agent, tid, name);
      toast.push(tc("rename.done", { name }));
    } catch {
      toast.push(tc("rename.failed"), "error");
    }
  }

  // ── context menu ─────────────────────────────────────────────────────────
  const [menu, setMenu] = useState<MenuState | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);
  function rowMenu(row: Row, x: number, y: number): MenuState {
    const title = row.title || tc("thread.fallback");
    return {
      x,
      y,
      label: t("bots.threadMenuLabel", { name: title }),
      items: [
        {
          label: t("bots.threadOpen"),
          title: t("bots.threadOpenHelp"),
          icon: <History {...ICON} />,
          onClick: () => {
            open(row);
            navigate(chatPath(agent, row.threadId));
          },
        },
        {
          label: t("bots.threadRename"),
          title: t("bots.threadRenameHelp"),
          icon: <Pencil {...ICON} />,
          disabled: !row.threadId,
          onClick: () => startRename(row),
        },
        {
          label: t("bots.threadDelete"),
          title: t("bots.threadDeleteHelp"),
          icon: <Trash2 {...ICON} />,
          danger: true,
          separatorBefore: true,
          disabled: !row.threadId,
          onClick: () => void remove(row),
        },
      ],
    };
  }

  return (
    <div className="bot-threads" role="group" aria-label={t("bots.threadsLabel", { name: label })}>
      <Link
        to={chatPath(agent)}
        className="bot-thread bot-thread-new"
        title={t("bots.newChatHelp")}
        draggable={false}
        onClick={newChat}
        data-testid="nav-thread-new"
      >
        <MessageSquarePlus {...ICON} />
        <span className="bot-thread-title">{t("bots.newChat")}</span>
      </Link>

      <ThreadFilterChips options={options} value={filter} t={tc} onChange={changeFilter} />

      {rows.length === 0 ? (
        <div className="bot-section-empty nav-label">
          {status === "loading" || status === "idle" ? t("bots.threadsLoading") : t("bots.threadsEmpty")}
        </div>
      ) : filtered.length === 0 ? (
        <div className="bot-section-empty nav-label">{t("bots.threadsFilterEmpty")}</div>
      ) : (
        shown.map((row) => {
          const active = onChat && activeKey === row.key;
          const title = row.title || tc("thread.fallback");
          if (row.threadId && editing === row.threadId) {
            return (
              <div key={row.key} className="bot-thread is-editing">
                <input
                  className="thread-rename-input"
                  autoFocus
                  value={draft}
                  maxLength={120}
                  placeholder={tc("thread.rename.placeholder")}
                  aria-label={tc("thread.rename.aria")}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => setDraft(e.target.value)}
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
              </div>
            );
          }
          const stamp = row.sortTs ? railTime(row.sortTs, nowTick, lang, t) : "";
          return (
            <div
              key={row.key}
              className="bot-thread-wrap"
              onContextMenu={(e) => {
                // A draft has no thread to name or delete yet — nothing the
                // menu could offer, so it stays the browser's own menu.
                if (!row.threadId) return;
                e.preventDefault();
                e.stopPropagation();
                setMenu(rowMenu(row, e.clientX, e.clientY));
              }}
            >
              <Link
                to={chatPath(agent, row.threadId)}
                className={`bot-thread${active ? " active" : ""}`}
                aria-current={active ? "page" : undefined}
                title={`${title}${stamp ? ` · ${stamp}` : ""}. ${t("bots.threadRowHelp")}`}
                draggable={false}
                data-testid="nav-thread-item"
                onClick={() => open(row)}
              >
                <OriginPill origin={row.origin} t={tc} />
                {isPlatformChannel(row.channel) && (
                  <ChannelPill
                    channel={row.channel}
                    t={tc}
                    compact={!!ORIGIN_ICONS[row.origin] && row.origin !== "external"}
                  />
                )}
                <span className="bot-thread-title">{title}</span>
                {row.streaming && (
                  <span className="thread-dot" title={t("bots.thinking")} aria-label={t("bots.thinking")} />
                )}
                {/* The recency stamp lives in the tooltip, not the row: a
                    244px sidebar has room for a channel pill OR a stamp beside
                    the title, and the pill is what tells rows apart (the
                    simple rail makes the same trade). */}
                {row.unread > 0 && (
                  <span className="thread-badge" title={t("bots.unread", { n: String(row.unread) })}>
                    {row.unread > 99 ? "99+" : row.unread}
                  </span>
                )}
              </Link>
              {row.threadId && (
                <button
                  type="button"
                  className="bot-thread-del"
                  aria-label={t("bots.threadDelete")}
                  title={t("bots.threadDeleteHelp")}
                  onClick={() => void remove(row)}
                >
                  <Trash2 size={13} strokeWidth={1.75} />
                </button>
              )}
            </div>
          );
        })
      )}

      {hidden > 0 && (
        <Link
          to={`${chatPath(agent)}?history=1`}
          className="bot-thread bot-thread-more"
          title={t("bots.showAllHelp")}
          draggable={false}
          data-testid="nav-thread-showall"
        >
          <History {...ICON} />
          <span className="bot-thread-title">{t("bots.showAll", { n: String(filtered.length) })}</span>
        </Link>
      )}

      {menu && <ContextMenu menu={menu} onClose={closeMenu} />}
    </div>
  );
}
