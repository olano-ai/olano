// The right-hand panel: EVERY chat with the selected bot.
//
// The rail deliberately shows only a handful of conversations — a bot that has
// run a nightly job for a year owns hundreds — so "Show all" lands here, where
// there is room to browse: a search box, the same source filter the rail uses,
// and the list revealed a page at a time so 500 rows never mount at once.
//
// Same data as the rail (SimpleApp's merged rows — no second fetch, no second
// truth), same open/delete handlers, same provenance pills. On a phone it is a
// full-screen overlay with the shared dialog behaviour; on a wide viewport it
// docks where the profile panel does — the two share the third column, one at
// a time.

import { useEffect, useMemo, useRef, useState } from "react";
import { Search, Trash2, X } from "lucide-react";
import { useI18n, useT } from "../../core/i18n";
import { useDialogA11y } from "../../ui/dialogA11y";
import { ChannelPill, isPlatformChannel, ORIGIN_ICONS, OriginPill } from "../chat/channels";
import { STR as CHAT_STR } from "../chat/strings";
import type { ChatRow } from "./BotRail";
import { railTime } from "./format";
import { STR } from "./strings";
import { ThreadFilterChips } from "../chat/ThreadFilterChips";
import { buildFilterOptions, filterThreadRows, type ThreadFilterId } from "../chat/threadFilters";

// How many rows each "Show more" click reveals. The list is already fetched —
// this bounds what the browser has to LAY OUT, not what travels the network.
const PAGE_SIZE = 30;

export function ChatsPanel({
  agent,
  rows,
  activeKey,
  threadFilter,
  asOverlay,
  onThreadFilter,
  onOpenThread,
  onOpenDraft,
  onDeleteThread,
  onClose,
}: {
  agent: string;
  /** The selected bot's merged conversations, newest first (SimpleApp owns them). */
  rows: ChatRow[];
  activeKey: string | undefined;
  threadFilter: ThreadFilterId;
  /** Full-screen overlay below the breakpoint, docked column above it. */
  asOverlay: boolean;
  onThreadFilter: (id: ThreadFilterId) => void;
  onOpenThread: (threadId: string) => void;
  onOpenDraft: (key: string) => void;
  onDeleteThread: (threadId: string) => void;
  onClose: () => void;
}) {
  const t = useT(STR);
  const tc = useT(CHAT_STR);
  const { lang } = useI18n();
  const panelRef = useRef<HTMLElement>(null);
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(PAGE_SIZE);

  // A new bot, filter or search is a new browse — start back at page one.
  useEffect(() => setLimit(PAGE_SIZE), [agent, threadFilter, query]);

  // Minute tick for the row stamps, same cadence as the rail's.
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  const filterOptions = useMemo(() => buildFilterOptions(rows), [rows]);
  const filtered = useMemo(() => {
    const bySource = filterThreadRows(rows, threadFilter);
    const q = query.trim().toLowerCase();
    if (!q) return bySource;
    return bySource.filter(
      (r) => r.title.toLowerCase().includes(q) || r.sender.toLowerCase().includes(q),
    );
  }, [rows, threadFilter, query]);
  const shown = filtered.slice(0, limit);
  const remaining = filtered.length - shown.length;

  return (
    <aside
      ref={panelRef}
      className={asOverlay ? "simple-profile simple-chats is-overlay" : "simple-profile simple-chats"}
      aria-label={t("chats.region", { agent })}
      data-testid="simple-chats-panel"
    >
      {asOverlay && <OverlayA11y panelRef={panelRef} onClose={onClose} />}

      <div className="simple-profile-head">
        <strong>{t("chats.title", { agent })}</strong>
        <button
          type="button"
          className="btn ghost xs"
          onClick={onClose}
          aria-label={t("chats.close")}
          title={t("chats.close")}
        >
          <X size={15} strokeWidth={2} />
        </button>
      </div>

      <div className="simple-chats-tools">
        <div className="simple-search">
          <Search size={14} strokeWidth={1.75} aria-hidden />
          <input
            className="simple-search-input"
            type="search"
            value={query}
            placeholder={t("chats.searchPlaceholder")}
            aria-label={t("chats.searchLabel")}
            title={t("chats.searchHelp")}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <ThreadFilterChips options={filterOptions} value={threadFilter} t={tc} onChange={onThreadFilter} />
        {/* What the chips mean, in words, once — the rail has no room for this
            line, the panel does. */}
        {filterOptions.length >= 3 && <p className="simple-hint">{tc("filter.help")}</p>}
        <p className="simple-hint simple-chats-count">
          {query.trim() || threadFilter !== "all"
            ? t("chats.countFiltered", { n: String(filtered.length), total: String(rows.length) })
            : t("chats.count", { n: String(rows.length) })}
        </p>
      </div>

      <div className="simple-chats-scroll">
        {filtered.length === 0 ? (
          <p className="simple-rail-empty muted">
            {rows.length === 0 ? t("rail.noThreads") : t("chats.noMatches")}
          </p>
        ) : (
          <ul className="simple-thread-list simple-chats-list">
            {shown.map((row) => (
              <li key={row.key}>
                <div className={`simple-thread simple-chats-row${activeKey === row.key ? " is-active" : ""}`}>
                  <button
                    type="button"
                    className="simple-thread-btn"
                    onClick={() => (row.threadId ? onOpenThread(row.threadId) : onOpenDraft(row.key))}
                  >
                    <span className="simple-chats-row-main">
                      <span className="simple-chats-row-top">
                        <OriginPill origin={row.origin} t={tc} />
                        {isPlatformChannel(row.channel) && (
                          <ChannelPill channel={row.channel} t={tc} compact={!!ORIGIN_ICONS[row.origin]} />
                        )}
                        <span className="simple-thread-title">{row.title || t("rail.chatFallback")}</span>
                        {row.streaming && (
                          <span className="simple-thread-dot" title={t("rail.working")} aria-label={t("rail.working")} />
                        )}
                        {row.unread > 0 && (
                          <span className="simple-count is-sm" title={t("rail.unread", { n: String(row.unread) })}>
                            {row.unread}
                          </span>
                        )}
                      </span>
                      {/* Line 2: who the conversation is with (platform chats),
                          and when it last moved. */}
                      <span className="simple-chats-row-meta">
                        {row.sender && <span className="simple-chats-row-sender">{row.sender}</span>}
                        {!!row.sortTs && (
                          <span className="simple-row-time">{railTime(row.sortTs, nowTick, lang, t)}</span>
                        )}
                      </span>
                    </span>
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
            ))}
          </ul>
        )}

        {remaining > 0 && (
          <button
            type="button"
            className="btn simple-chats-more"
            title={t("chats.showMoreHelp")}
            onClick={() => setLimit((n) => n + PAGE_SIZE)}
          >
            {t("chats.showMore", { n: String(Math.min(remaining, PAGE_SIZE)), left: String(remaining) })}
          </button>
        )}
      </div>
    </aside>
  );
}

// Hook host for the mobile overlay — mounted only while the panel is an
// overlay, so the dialog behaviour applies exactly then, never to the column.
function OverlayA11y({
  panelRef,
  onClose,
}: {
  panelRef: React.RefObject<HTMLElement | null>;
  onClose: () => void;
}) {
  useDialogA11y(panelRef, onClose);
  return null;
}
