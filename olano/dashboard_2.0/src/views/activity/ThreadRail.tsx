// The conversation rail: one row per agent pair, newest activity first.
//
// This replaces a list that grew one row per 2-minute burst — 234 rows for 251
// messages, i.e. the raw log with extra chrome. Rows are fixed height so the
// list can be windowed without measuring, which is what keeps a fleet with
// thousands of exchanges from mounting thousands of nodes.

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowLeftRight, ArrowRight, X } from "lucide-react";
import type { Agent } from "../../core/types";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import { isExternalKind, type PairThread } from "./threads";
import { Party, fmtRel, peek } from "./ui";

export type KindFilter = "all" | "peer" | "subagent" | "external" | "live";

// Fixed row stride (px) — the windowing maths depends on it, so any style
// change to ThreadRow must keep box + margin exactly this tall. ROW_GAP mirrors
// the `.a2a-thread { margin-bottom }` rule.
const ROW_GAP = 2;
const ROW_H = 76;
const OVERSCAN = 6;
// Below this many rows, windowing costs more than it saves.
const WINDOW_MIN_ROWS = 40;

export const ALL_KEY = "all";

export function ThreadRail({
  threads,
  totalMessages,
  selectedKey,
  onSelect,
  queryInput,
  onQueryChange,
  kind,
  onKindChange,
  roster,
  loading,
}: {
  threads: PairThread[];
  totalMessages: number;
  selectedKey: string;
  onSelect: (key: string) => void;
  queryInput: string;
  onQueryChange: (q: string) => void;
  kind: KindFilter;
  onKindChange: (k: KindFilter) => void;
  roster: Map<string, Agent>;
  loading: boolean;
}) {
  const t = useT(STR);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportH, setViewportH] = useState(600);

  // Per-segment counts, so the pills say how much each filter would show
  // rather than making the user click to find out.
  const counts = useMemo(
    () => ({
      all: threads.length,
      peer: threads.filter((x) => x.kind === "peer").length,
      subagent: threads.filter((x) => x.kind === "subagent").length,
      external: threads.filter((x) => isExternalKind(x.kind)).length,
      live: threads.filter((x) => x.in_flight > 0).length,
    }),
    [threads],
  );

  const rows = useMemo(() => {
    if (kind === "all") return threads;
    if (kind === "live") return threads.filter((x) => x.in_flight > 0);
    if (kind === "external") return threads.filter((x) => isExternalKind(x.kind));
    return threads.filter((x) => x.kind === kind);
  }, [threads, kind]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setViewportH(el.clientHeight || 600);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (el) setScrollTop(el.scrollTop);
  }, []);

  // Reset the scroll offset when the visible set changes under us, or the
  // window maths would keep rendering a slice that no longer exists.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && el.scrollTop > rows.length * ROW_H) {
      el.scrollTop = 0;
      setScrollTop(0);
    }
  }, [rows.length]);

  const windowed = rows.length >= WINDOW_MIN_ROWS;
  const start = windowed ? Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN) : 0;
  const end = windowed
    ? Math.min(rows.length, Math.ceil((scrollTop + viewportH) / ROW_H) + OVERSCAN)
    : rows.length;
  const visible = rows.slice(start, end);

  const segments: { id: KindFilter; label: string; n: number }[] = [
    { id: "all", label: t("seg.all"), n: counts.all },
    { id: "peer", label: t("seg.peer"), n: counts.peer },
    { id: "subagent", label: t("seg.subagent"), n: counts.subagent },
    { id: "external", label: t("seg.external"), n: counts.external },
    { id: "live", label: t("seg.live"), n: counts.live },
  ];

  return (
    <aside className="card a2a-rail" data-testid="a2a-rail">
      <div className="a2a-rail-head">
        <div className="a2a-segs">
          {segments.map((s) => (
            <button
              key={s.id}
              className={`a2a-seg ${kind === s.id ? "is-active" : ""}`}
              onClick={() => onKindChange(s.id)}
              disabled={s.n === 0 && s.id !== "all"}
            >
              {s.label}
              <span className="a2a-seg-n">{s.n}</span>
            </button>
          ))}
        </div>

        <div style={{ position: "relative" }}>
          <input
            className="input"
            style={{ background: "var(--surface-2)", padding: "7px 11px", paddingRight: 30 }}
            placeholder={t("filter.placeholder")}
            value={queryInput}
            onChange={(e) => onQueryChange(e.target.value)}
            aria-label={t("filter.aria")}
          />
          {queryInput && (
            <button
              className="a2a-clear"
              onClick={() => onQueryChange("")}
              title={t("filter.clear")}
              aria-label={t("filter.clear")}
            >
              <X size={15} strokeWidth={1.75} />
            </button>
          )}
        </div>
      </div>

      <div ref={scrollRef} onScroll={onScroll} className="a2a-rail-scroll">
        <div
          className={`a2a-thread ${selectedKey === ALL_KEY ? "is-active" : ""}`}
          onClick={() => onSelect(ALL_KEY)}
          style={{ height: "auto" }}
        >
          <div className="row" style={{ gap: 8, fontWeight: 600, fontSize: 13 }}>
            <Activity size={14} strokeWidth={1.75} /> {t("all")}
          </div>
          <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
            {t("summary", {
              threads: t(threads.length === 1 ? "count.threads.one" : "count.threads.other", {
                n: String(threads.length),
              }),
              msgs: t(totalMessages === 1 ? "count.msgs.one" : "count.msgs.other", {
                n: String(totalMessages),
              }),
            })}
          </div>
        </div>

        {rows.length === 0 && !loading && (
          <div className="a2a-rail-empty">
            {queryInput.trim()
              ? t("filter.noMatch", { query: queryInput.trim() })
              : t("rail.empty")}
          </div>
        )}

        <div style={{ paddingTop: start * ROW_H, paddingBottom: (rows.length - end) * ROW_H }}>
          {visible.map((th) => (
            <ThreadRow
              key={th.key}
              thread={th}
              active={th.key === selectedKey}
              onSelect={onSelect}
              roster={roster}
            />
          ))}
        </div>
      </div>
    </aside>
  );
}

// Memoized: a live frame rebuilds the rollup array but leaves every untouched
// row object identical, so only the conversation that actually moved re-renders.
const ThreadRow = memo(function ThreadRow({
  thread,
  active,
  onSelect,
  roster,
}: {
  thread: PairThread;
  active: boolean;
  onSelect: (key: string) => void;
  roster: Map<string, Agent>;
}) {
  const t = useT(STR);
  // Peers talk both ways; a subagent type can only ever be delegated *to*, and
  // an outside caller / remote agent only ever sits on one end.
  const Arrow = thread.kind === "peer" ? ArrowLeftRight : ArrowRight;

  return (
    <div
      className={`a2a-thread ${active ? "is-active" : ""}`}
      onClick={() => onSelect(thread.key)}
      style={{ height: ROW_H - ROW_GAP }}
    >
      <div className="a2a-thread-top">
        <Party name={thread.a} roster={roster} size={18} />
        <Arrow size={12} strokeWidth={1.75} style={{ color: "var(--text-4)", flexShrink: 0 }} />
        <Party name={thread.b} roster={roster} size={18} />
        {thread.in_flight > 0 && <span className="thread-dot" title={t("seg.live")} />}
      </div>
      <div className="a2a-thread-preview">{peek(thread.last_preview, 96)}</div>
      <div className="a2a-thread-meta">
        <span>{fmtRel(t, thread.last_ts)}</span>
        <span>·</span>
        <span>
          {t(thread.messages === 1 ? "count.msgs.one" : "count.msgs.other", {
            n: String(thread.messages),
          })}
        </span>
        {thread.kind === "subagent" && <span className="badge admin">{t("badge.subagent")}</span>}
      </div>
    </div>
  );
});
