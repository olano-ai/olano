// One conversation's transcript: every exchange between a pair, in order,
// broken by day.
//
// The old view stacked *every* conversation in this pane and pinned it to the
// bottom on every SSE frame, so reading history was impossible. Here the pane
// shows one conversation, pages older exchanges in on demand, and only follows
// the tail when the reader is already at the tail.

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowLeftRight, ArrowRight, Activity } from "lucide-react";
import type { Agent } from "../../core/types";
import { useI18n, useT } from "../../core/i18n";
import { Markdown } from "./markdown";
import { STR } from "./strings";
import { externalParty, type Exchange, type PairThread } from "./threads";
import { groupByDay, localDayKey } from "./threads";
import { AgentAvatar, agentColor, ExternalAvatar, fmtDuration, fmtRel, fmtTime, Party, ProtocolBadge } from "./ui";

type T = (key: string, vars?: Record<string, string>) => string;

/** "Today" / "Yesterday" / a localized date, from a local-day bucket key. */
function dayLabel(day: string, lang: string, t: T): string {
  if (!day) return "";
  const now = new Date();
  if (day === localDayKey(now.toISOString())) return t("day.today");
  const yesterday = new Date(now.getTime() - 86_400_000);
  if (day === localDayKey(yesterday.toISOString())) return t("day.yesterday");
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(lang, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

export function Transcript({
  thread,
  exchanges,
  roster,
  loading,
  hasMore,
  onLoadOlder,
  onBack,
  showBack,
  showPairHeaders,
}: {
  thread: PairThread | null;
  exchanges: Exchange[];
  roster: Map<string, Agent>;
  loading: boolean;
  hasMore: boolean;
  onLoadOlder: () => void;
  onBack: () => void;
  showBack: boolean;
  /** "All activity" mixes pairs, so each card needs its own participants. */
  showPairHeaders: boolean;
}) {
  const t = useT(STR);
  const { lang } = useI18n();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);

  // Anchor for "load older": the element we keep visually still across the
  // prepend, captured just before the fetch resolves.
  const anchor = useRef<{ id: string; top: number } | null>(null);
  const prevFirstId = useRef<string | null>(null);

  const days = useMemo(() => groupByDay(exchanges), [exchanges]);
  const key = thread?.key ?? "all";

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  }, []);

  const jumpToLatest = useCallback(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setAtBottom(true);
  }, []);

  // A different conversation starts at its newest message.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setAtBottom(true);
    anchor.current = null;
    prevFirstId.current = null;
  }, [key]);

  // Follow the tail only when the reader is already there — never yank them
  // down mid-read, which is what the unconditional pin used to do.
  useEffect(() => {
    if (!atBottom) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [exchanges, atBottom]);

  // Older exchanges arrived at the top: restore the anchor element's on-screen
  // position so the view doesn't jump. Measuring a real element (rather than a
  // scrollHeight delta) stays correct with content-visibility in play.
  useLayoutEffect(() => {
    const first = exchanges[0]?.id ?? null;
    const el = scrollRef.current;
    const a = anchor.current;
    if (a && el && first !== prevFirstId.current) {
      const node = el.querySelector<HTMLElement>(`[data-ex="${CSS.escape(a.id)}"]`);
      if (node) el.scrollTop += node.getBoundingClientRect().top - a.top;
      anchor.current = null;
    }
    prevFirstId.current = first;
  }, [exchanges]);

  const loadOlder = useCallback(() => {
    const el = scrollRef.current;
    const id = exchanges[0]?.id;
    if (el && id) {
      const node = el.querySelector<HTMLElement>(`[data-ex="${CSS.escape(id)}"]`);
      if (node) anchor.current = { id, top: node.getBoundingClientRect().top };
    }
    onLoadOlder();
  }, [exchanges, onLoadOlder]);

  if (!thread && !showPairHeaders) {
    return (
      <div className="a2a-feed-empty">
        <span className="a2a-empty-icon">
          <Activity size={20} strokeWidth={1.75} />
        </span>
        <div style={{ fontSize: 14.5, fontWeight: 600, color: "var(--text)", marginBottom: 4 }}>
          {t("transcript.pick")}
        </div>
        <div style={{ fontSize: 12.5, lineHeight: 1.55 }}>{t("transcript.pickSub")}</div>
      </div>
    );
  }

  return (
    <div className="a2a-pane">
      <div className="a2a-pane-head">
        {showBack && (
          <button className="btn" onClick={onBack} style={{ padding: "5px 10px" }}>
            <ArrowLeft size={14} strokeWidth={1.75} /> {t("transcript.back")}
          </button>
        )}
        {thread ? (
          <PairHeading thread={thread} roster={roster} />
        ) : (
          <div className="row" style={{ gap: 8, fontWeight: 600, fontSize: 13 }}>
            <Activity size={14} strokeWidth={1.75} /> {t("all")}
          </div>
        )}
        <div className="a2a-pane-meta">
          {thread && (
            <>
              <span>
                {t(
                  thread.messages === 1 ? "count.exchanges.one" : "count.exchanges.other",
                  { n: String(thread.messages) },
                )}
              </span>
              {thread.avg_ms !== null && (
                <span>{t("transcript.avg", { n: fmtDuration(thread.avg_ms) })}</span>
              )}
              <span>{fmtRel(t, thread.last_ts)}</span>
            </>
          )}
        </div>
      </div>

      <div ref={scrollRef} onScroll={onScroll} className="a2a-pane-scroll">
        {loading && exchanges.length === 0 ? (
          <div className="spinner" style={{ margin: "32px auto" }} />
        ) : (
          <>
            <div className="a2a-older">
              {hasMore ? (
                <button className="btn" onClick={loadOlder} disabled={loading}>
                  {t("loadOlder")}
                </button>
              ) : (
                exchanges.length > 0 && <span className="muted">{t("loadOlder.done")}</span>
              )}
            </div>
            {days.map(({ key: dayKey, day, items }) => (
              <div key={dayKey}>
                <div className="a2a-day">
                  <span>{dayLabel(day, lang, t)}</span>
                </div>
                {items.map((ex) => (
                  <ExchangeCard
                    key={ex.id}
                    ex={ex}
                    roster={roster}
                    showPair={showPairHeaders}
                  />
                ))}
              </div>
            ))}
          </>
        )}
      </div>

      {!atBottom && exchanges.length > 0 && (
        <button className="btn primary a2a-jump" onClick={jumpToLatest}>
          <ArrowDown size={15} strokeWidth={1.75} /> {t("jump.latest")}
        </button>
      )}
    </div>
  );
}

function PairHeading({ thread, roster }: { thread: PairThread; roster: Map<string, Agent> }) {
  const Arrow = thread.kind === "peer" ? ArrowLeftRight : ArrowRight;
  return (
    <div className="row" style={{ gap: 7, minWidth: 0, flexWrap: "wrap" }}>
      <Party name={thread.a} roster={roster} size={22} nameStyle={{ fontSize: 13 }} />
      <Arrow size={13} strokeWidth={1.75} style={{ color: "var(--text-4)", flexShrink: 0 }} />
      <Party name={thread.b} roster={roster} size={22} nameStyle={{ fontSize: 13 }} />
    </div>
  );
}

// Memoized on the exchange object: folding rebuilds the array each frame but
// replaces only the exchange that changed, so identity is exactly "this card
// moved" — without it a single message re-rendered every markdown bubble.
const ExchangeCard = memo(function ExchangeCard({
  ex,
  roster,
  showPair,
}: {
  ex: Exchange;
  roster: Map<string, Agent>;
  showPair: boolean;
}) {
  const t = useT(STR);
  const fromColor = agentColor(ex.from, roster);
  const toColor = agentColor(ex.to, roster);
  const fromAgent = roster.get(ex.from);
  const toAgent = roster.get(ex.to);
  // An outside party is shown by its label (the token / client name), with the
  // protocol badge saying what it is; the bubble avatar becomes the globe.
  const fromExt = externalParty(ex.from);
  const toExt = externalParty(ex.to);
  const fromName = fromExt ? fromExt.label : ex.from;
  const toName = toExt ? toExt.label : ex.to;

  return (
    <div className="a2a-exchange" data-ex={ex.id}>
      {showPair && (
        <div className="a2a-exchange-pair">
          <span className="a2a-name">{fromName}</span>
          {fromExt && <ProtocolBadge protocol={fromExt.protocol} />}
          <ArrowRight size={11} strokeWidth={1.75} style={{ color: "var(--text-4)" }} />
          <span className="a2a-name">{toName}</span>
          {toExt && <ProtocolBadge protocol={toExt.protocol} />}
          {ex.kind === "subagent" && <span className="badge admin">{t("badge.subagent")}</span>}
        </div>
      )}

      <Bubble
        color={fromColor}
        name={fromName}
        avatarUrl={fromAgent?.avatar_url ?? undefined}
        external={!!fromExt}
        variant="out"
      >
        <div className="a2a-bubble-head">
          <span className="a2a-name">{fromName}</span>
          {fromExt && <ProtocolBadge protocol={fromExt.protocol} />}
          <span className="a2a-stamp">{fmtTime(ex.ts_sent)}</span>
        </div>
        <Markdown text={ex.message} />
      </Bubble>

      {ex.status === "ok" ? (
        <Bubble
          color={toColor}
          name={toName}
          avatarUrl={toAgent?.avatar_url ?? undefined}
          external={!!toExt}
          variant="in"
        >
          <div className="a2a-bubble-head is-in">
            {toExt && <ProtocolBadge protocol={toExt.protocol} />}
            <span className="a2a-name">{toName}</span>
            <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
              <span className="a2a-stamp">{fmtTime(ex.ts_reply || ex.ts_sent)}</span>
              {ex.elapsedMs !== undefined && (
                <span className="a2a-elapsed">{fmtDuration(ex.elapsedMs)}</span>
              )}
            </span>
          </div>
          <Markdown text={ex.reply} />
        </Bubble>
      ) : ex.status === "pending" ? (
        <Bubble
          color={toColor}
          name={toName}
          avatarUrl={toAgent?.avatar_url ?? undefined}
          external={!!toExt}
          variant="pending"
        >
          <div className="a2a-bubble-head is-in">
            {toExt && <ProtocolBadge protocol={toExt.protocol} />}
            <span className="a2a-name">{toName}</span>
            <span style={{ color: "var(--text-3)", fontSize: 11 }}>{t("turn.thinking")}</span>
          </div>
          <div className="typing-dots">
            <span />
            <span />
            <span />
          </div>
        </Bubble>
      ) : (
        <div className="a2a-noreply" title={t("status.noReplyHint")}>
          {t("status.noReply")}
        </div>
      )}
    </div>
  );
});

function Bubble({
  color,
  name,
  avatarUrl,
  external = false,
  variant,
  children,
}: {
  color: string;
  name: string;
  avatarUrl?: string;
  external?: boolean;
  variant: "out" | "in" | "pending";
  children: React.ReactNode;
}) {
  const isOut = variant === "out";
  return (
    <div
      className="a2a-bubble-row"
      style={{
        alignSelf: isOut ? "flex-start" : "flex-end",
        flexDirection: isOut ? "row" : "row-reverse",
        opacity: variant === "pending" ? 0.85 : 1,
      }}
    >
      {external ? (
        <ExternalAvatar title={name} />
      ) : (
        <AgentAvatar name={name} color={color} avatarUrl={avatarUrl} />
      )}
      <div
        className="a2a-bubble"
        style={{
          background: `color-mix(in srgb, ${color} ${isOut ? 8 : 14}%, var(--bg-elev))`,
          border: `1px solid color-mix(in srgb, ${color} 35%, var(--border))`,
          ...(isOut ? { borderTopLeftRadius: 4 } : { borderTopRightRadius: 4, alignItems: "flex-end" }),
        }}
      >
        {children}
      </div>
    </div>
  );
}
