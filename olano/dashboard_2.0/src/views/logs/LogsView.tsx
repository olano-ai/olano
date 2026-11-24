import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ScrollText, X } from "lucide-react";
import { api } from "../../core/api";
import { usePersistentState } from "../../core/usePersistentState";
import { usePathAgent } from "../../core/usePathAgent";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { useToast } from "../../ui/toast";
import type { Agent } from "../../core/types";
import {
  LEVELS,
  LEVEL_BADGE,
  formatLogLine,
  highlightSegments,
  inferLevel,
  type LevelFilter,
} from "./logFormat";
import { STR } from "./strings";
import {
  subscribeStream,
  subscribeStreamState,
  getStreamState,
} from "../../core/agentStream";

// GET /api/logs?lines=N → { logs: string[], total: number }.
interface LogsResponse {
  logs: string[];
  total: number;
}

// Live SSE frame off /api/stream. The Logs view only cares about `log` events
// (whose `data` is the formatted log line as a plain string); every other event
// type — agent_activity, worker_done, scheduler_update … — is ignored here.
interface StreamEvent {
  type: string;
  data?: unknown;
}

const MAX_LINES = 1000;
const FETCH_LINES = 300;
const SEARCH_DEBOUNCE_MS = 80;

// Buffer entries carry a stable id: with `key={index}` the FIFO trim shifted
// every row's key, forcing React to re-render the entire 1000-row list on
// each appended line under load. Stable keys + a memoized LogRow turn an
// append into exactly one new row.
interface LogEntry {
  id: number;
  text: string;
}
let logSeq = 0;
const mkEntry = (text: string): LogEntry => ({ id: ++logSeq, text });

export function LogsView() {
  const t = useT(STR);
  const toast = useToast();

  const [logs, setLogs] = useState<LogEntry[] | null>(null);
  const [total, setTotal] = useState(0);
  const [level, setLevel] = usePersistentState<LevelFilter>("logs.level", "all");
  // `queryInput` is the raw text box; `query` is the debounced value the filter
  // actually runs on — matches the original's debounce(…, 80).
  const [queryInput, setQueryInput] = usePersistentState("logs.queryInput", "");
  const [query, setQuery] = useState("");
  const [autoScroll, setAutoScroll] = usePersistentState("logs.autoScroll", true);
  const [connected, setConnected] = useState(false);
  const [paused, setPaused] = useState(false);
  // Whether the stream is scrolled to (near) the bottom. Drives the floating
  // "jump to latest" affordance when the user scrolls up to read history.
  const [atBottom, setAtBottom] = useState(true);

  // Agent scope. The classic dashboard filtered log lines by the topbar's
  // "context agent"; 2.0 has no global topbar agent, so we surface a local
  // <select> (empty value = all agents) seeded from GET /api/agents.
  const [agents, setAgents] = useState<Agent[]>([]);
  // /logs/<agent> narrows the stream to one agent (none = all); a filter, so
  // changing it is not a Back step.
  const [agentFilter, setAgentFilter] = usePathAgent("logs", "logs.agentFilter", "replace");

  const containerRef = useRef<HTMLDivElement>(null);
  // Buffer lines that arrive while paused so resuming flushes them in order.
  const pausedBufferRef = useRef<string[]>([]);

  // ── data ────────────────────────────────────────────────────────────────
  const reload = useCallback(() => {
    setLogs(null);
    pausedBufferRef.current = [];
    api
      .get<LogsResponse>(`/api/logs?lines=${FETCH_LINES}`)
      .then((data) => {
        setLogs((data.logs ?? []).map(mkEntry));
        setTotal(data.total ?? (data.logs ?? []).length);
      })
      .catch(() => {
        setLogs([]);
        toast.push(t("toast.loadError"), "error");
      });
  }, [toast, t]);

  useEffect(reload, [reload]);

  useEffect(() => {
    api
      .cachedGet<Agent[]>("/api/agents")
      .then(setAgents)
      .catch(() => {
        /* agent scope is optional — silently fall back to "all agents" */
      });
  }, []);

  // ── debounce the search box ─────────────────────────────────────────────
  useEffect(() => {
    const t = window.setTimeout(() => setQuery(queryInput), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [queryInput]);

  // Keep a live ref of `paused` so the (mount-once) SSE handler reads the
  // current value without re-subscribing.
  const pausedRef = useRef(paused);
  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  const appendLine = useCallback((line: string) => {
    setLogs((prev) => {
      const next = [...(prev ?? []), mkEntry(line)];
      return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
    });
  }, []);

  // ── live tail over SSE ──────────────────────────────────────────────────
  // EventSource is GET-only, which matches /api/stream. We append every `log`
  // frame, FIFO-capping the buffer at MAX_LINES like the original. While paused
  // we stash incoming lines and flush them on resume.
  useEffect(() => {
    setConnected(getStreamState() === "open");
    const offState = subscribeStreamState((s) => setConnected(s === "open"));
    const offStream = subscribeStream((frame) => {
      const event = frame as StreamEvent;
      if (event.type === "connected") {
        setConnected(true);
        return;
      }
      if (event.type !== "log" || typeof event.data !== "string") return;
      const line = event.data;
      setTotal((n) => n + 1);
      if (pausedRef.current) {
        pausedBufferRef.current.push(line);
        return;
      }
      appendLine(line);
    });
    return () => {
      offState();
      offStream();
    };
  }, [appendLine]);

  function togglePause() {
    setPaused((wasPaused) => {
      const nowPaused = !wasPaused;
      if (!nowPaused && pausedBufferRef.current.length) {
        // Resuming — flush everything that queued up while paused.
        const buffered = pausedBufferRef.current;
        pausedBufferRef.current = [];
        setLogs((prev) => {
          const next = [...(prev ?? []), ...buffered.map(mkEntry)];
          return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
        });
      }
      return nowPaused;
    });
  }

  // ── filtering ───────────────────────────────────────────────────────────
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const ctx = agentFilter.trim().toLowerCase();
    return (logs ?? []).filter((entry) => {
      if (level !== "all" && inferLevel(entry.text) !== level) return false;
      const lower = entry.text.toLowerCase();
      if (q && !lower.includes(q)) return false;
      if (ctx && !lower.includes(ctx)) return false;
      return true;
    });
  }, [logs, level, query, agentFilter]);

  // Count of entries per level (over the agent+search-filtered set, ignoring
  // the active level) so the segmented control can show live counts.
  const levelCounts = useMemo(() => {
    const q = query.trim().toLowerCase();
    const ctx = agentFilter.trim().toLowerCase();
    const counts: Record<LevelFilter, number> = {
      all: 0,
      info: 0,
      warn: 0,
      error: 0,
      debug: 0,
    };
    for (const entry of logs ?? []) {
      const lower = entry.text.toLowerCase();
      if (q && !lower.includes(q)) continue;
      if (ctx && !lower.includes(ctx)) continue;
      counts.all += 1;
      counts[inferLevel(entry.text)] += 1;
    }
    return counts;
  }, [logs, query, agentFilter]);

  // ── auto-scroll ─────────────────────────────────────────────────────────
  // Stick to the bottom while tailing, but never yank the user down if they've
  // scrolled up to read history (atBottom === false).
  useEffect(() => {
    if (!autoScroll || paused || !atBottom) return;
    const c = containerRef.current;
    if (c) c.scrollTop = c.scrollHeight;
  }, [filtered, autoScroll, paused, atBottom]);

  // Track whether we're pinned to the bottom (within a small slack) so the
  // jump-to-latest button only shows when there's something below the fold.
  const onScroll = useCallback(() => {
    const c = containerRef.current;
    if (!c) return;
    const distance = c.scrollHeight - c.scrollTop - c.clientHeight;
    setAtBottom(distance < 40);
  }, []);

  const jumpToBottom = useCallback(() => {
    const c = containerRef.current;
    if (c) c.scrollTop = c.scrollHeight;
    setAtBottom(true);
  }, []);

  // ── actions ─────────────────────────────────────────────────────────────
  const clear = useCallback(() => {
    setLogs([]);
    pausedBufferRef.current = [];
  }, []);

  const copyVisible = useCallback(async () => {
    const text = filtered.map((e) => e.text).join("\n");
    if (!text) {
      toast.push(t("toast.nothingCopy"), "info");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      const count = String(filtered.length);
      toast.push(
        t(filtered.length === 1 ? "toast.copiedOne" : "toast.copiedMany", { count }),
        "info",
      );
    } catch {
      toast.push(t("toast.clipboardUnavailable"), "error");
    }
  }, [filtered, toast, t]);

  const download = useCallback(() => {
    const text = filtered.map((e) => e.text).join("\n");
    if (!text) {
      toast.push(t("toast.nothingDownload"), "info");
      return;
    }
    const blob = new Blob([text + "\n"], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const a = document.createElement("a");
    a.href = url;
    a.download = `olano-logs-${stamp}.log`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }, [filtered, toast, t]);

  const resetFilters = useCallback(() => {
    setLevel("all");
    setQueryInput("");
    setQuery("");
    setAgentFilter("");
  }, []);

  const hasActiveFilter = level !== "all" || query.trim() !== "" || agentFilter !== "";

  // ── render ──────────────────────────────────────────────────────────────
  return (
    <>
      <div className="topbar">
        <BrandWordmark />
        <div className="row" style={{ gap: 8 }}>
          <span
            className="badge"
            style={{ gap: 6 }}
            title={connected ? t("conn.streaming") : t("conn.reconnecting")}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: connected ? "var(--success)" : "var(--text-4)",
              }}
            />
            {connected ? t("conn.live") : t("conn.offline")}
          </span>
          <button
            className="btn"
            onClick={togglePause}
            title={paused ? t("btn.resumeTitle") : t("btn.pauseTitle")}
          >
            {paused
              ? pausedBufferRef.current.length
                ? t("btn.resumeCount", { count: String(pausedBufferRef.current.length) })
                : t("btn.resume")
              : t("btn.pause")}
          </button>
          <button className="btn" onClick={reload} title={t("btn.reloadTitle")}>
            {t("btn.reload")}
          </button>
        </div>
      </div>

      <div className="chat">
        <div
          className="page"
          style={{ paddingBottom: 0, maxWidth: "none", width: "100%", flexShrink: 0 }}
        >
          <h1>{t("title")}</h1>
          <p className="muted">{t("subtitle")}</p>

          {/* toolbar: level segmented control + search + agent scope + actions */}
          <div className="row" style={{ marginTop: 14, flexWrap: "wrap", gap: 8 }}>
            <div
              className="row"
              style={{
                gap: 2,
                padding: 2,
                borderRadius: "var(--r-sm)",
                background: "var(--surface-2)",
              }}
            >
              {LEVELS.map((l) => {
                const active = l === level;
                return (
                  <button
                    key={l}
                    onClick={() => setLevel(l)}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      padding: "5px 11px",
                      borderRadius: "var(--r-xs)",
                      border: "none",
                      cursor: "pointer",
                      fontSize: 12.5,
                      textTransform: "capitalize",
                      fontWeight: active ? 600 : 500,
                      background: active ? "var(--surface)" : "transparent",
                      color: active ? "var(--text)" : "var(--text-3)",
                      boxShadow: active ? "var(--shadow-sm)" : "none",
                    }}
                  >
                    {t(`level.${l}`)}
                    <span
                      style={{
                        fontSize: 10.5,
                        fontVariantNumeric: "tabular-nums",
                        color: active ? "var(--text-3)" : "var(--text-4)",
                      }}
                    >
                      {levelCounts[l]}
                    </span>
                  </button>
                );
              })}
            </div>

            <div style={{ position: "relative", flex: 1, minWidth: 180, maxWidth: 380 }}>
              <input
                className="input"
                placeholder={t("search.placeholder")}
                value={queryInput}
                onChange={(e) => setQueryInput(e.target.value)}
                style={{ paddingRight: queryInput ? 30 : undefined }}
              />
              {queryInput && (
                <button
                  onClick={() => setQueryInput("")}
                  title={t("search.clear")}
                  aria-label={t("search.clear")}
                  style={{
                    position: "absolute",
                    right: 6,
                    top: "50%",
                    transform: "translateY(-50%)",
                    display: "inline-flex",
                    alignItems: "center",
                    border: "none",
                    background: "transparent",
                    color: "var(--text-4)",
                    cursor: "pointer",
                    lineHeight: 1,
                    padding: "2px 4px",
                  }}
                >
                  <X size={15} strokeWidth={1.75} />
                </button>
              )}
            </div>

            {agents.length > 0 && (
              <select
                className="input"
                style={{ width: "auto", minWidth: 150 }}
                value={agentFilter}
                onChange={(e) => setAgentFilter(e.target.value)}
                title={t("agent.scopeTitle")}
              >
                <option value="">{t("agent.all")}</option>
                {agents.map((a) => (
                  <option key={a.name} value={a.name}>
                    {a.name}
                  </option>
                ))}
              </select>
            )}

            <span className="spacer" style={{ flex: 1 }} />

            <label
              className="row"
              style={{ gap: 6, fontSize: 12.5, color: "var(--text-3)", cursor: "pointer" }}
            >
              <input
                type="checkbox"
                checked={autoScroll}
                onChange={(e) => setAutoScroll(e.target.checked)}
              />
              {t("autoScroll")}
            </label>
            <button className="btn" onClick={copyVisible} title={t("btn.copyTitle")}>
              {t("btn.copy")}
            </button>
            <button className="btn" onClick={download} title={t("btn.downloadTitle")}>
              {t("btn.download")}
            </button>
            <button className="btn" onClick={clear} title={t("btn.clearTitle")}>
              {t("btn.clear")}
            </button>
          </div>

          <div
            className="row"
            style={{ marginTop: 10, fontSize: 12, color: "var(--text-3)", gap: 12 }}
          >
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {filtered.length} {filtered.length === 1 ? t("count.entry") : t("count.entries")}
              {hasActiveFilter && logs ? ` ${t("count.ofBuffered", { total: String(logs.length) })}` : ""}
            </span>
            {total > (logs?.length ?? 0) && (
              <span style={{ color: "var(--text-4)" }}>
                {t("count.sinceBoot", { total: String(total) })}
              </span>
            )}
            {paused && (
              <span style={{ color: "var(--warn)", fontWeight: 600 }}>
                {t("status.paused")}
                {pausedBufferRef.current.length
                  ? ` · ${t("status.queued", { count: String(pausedBufferRef.current.length) })}`
                  : ""}
              </span>
            )}
          </div>
        </div>

        {/* log stream — glassy panel that owns the remaining height */}
        <div
          style={{
            position: "relative",
            flex: 1,
            minHeight: 0,
            margin: "12px 20px 20px",
          }}
        >
          <div
            className="chat-log"
            ref={containerRef}
            onScroll={onScroll}
            style={{
              gap: 0,
              height: "100%",
              padding: "12px 16px",
              borderRadius: "var(--r-md)",
              background: "var(--bg-elev)",
              boxShadow: "var(--shadow-sm)",
              fontFamily: "var(--font-mono)",
              fontSize: 11.5,
              lineHeight: 1.65,
            }}
          >
            {logs === null ? (
              <div className="row" style={{ gap: 10, color: "var(--text-3)" }}>
                <span className="spinner" />
                {t("loading")}
              </div>
            ) : filtered.length === 0 ? (
              <div
                style={{
                  margin: "auto",
                  textAlign: "center",
                  color: "var(--text-3)",
                  fontFamily: "var(--font-sans)",
                  padding: "32px 16px",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "center",
                    marginBottom: 6,
                    color: "var(--text-4)",
                  }}
                >
                  <ScrollText size={30} strokeWidth={1.5} />
                </div>
                <p style={{ margin: 0 }}>
                  {logs.length === 0 ? t("empty.none") : t("empty.noMatch")}
                </p>
                {logs.length > 0 && hasActiveFilter && (
                  <button
                    className="btn ghost"
                    style={{ marginTop: 12 }}
                    onClick={resetFilters}
                  >
                    {t("btn.clearFilters")}
                  </button>
                )}
              </div>
            ) : (
              filtered.map((entry) => <LogRow key={entry.id} line={entry.text} query={query} />)
            )}
          </div>

          {/* jump-to-latest — appears when scrolled away from the tail */}
          {!atBottom && filtered.length > 0 && (
            <button
              className="btn primary"
              onClick={jumpToBottom}
              title={t("jump.latestTitle")}
              style={{
                position: "absolute",
                bottom: 16,
                left: "50%",
                transform: "translateX(-50%)",
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                boxShadow: "var(--glow)",
                fontSize: 12.5,
                padding: "6px 14px",
              }}
            >
              <ArrowDown size={15} strokeWidth={1.75} /> {t("jump.latest")}
            </button>
          )}
        </div>
      </div>
    </>
  );
}

// ── single rendered line ────────────────────────────────────────────────────
// Memoized: with stable entry keys, appending under heavy log throughput
// renders only the new row instead of re-parsing all 1000.
const LogRow = memo(function LogRow({ line, query }: { line: string; query: string }) {
  const t = useT(STR);
  const [hover, setHover] = useState(false);
  const [copied, setCopied] = useState(false);
  const lvl = inferLevel(line);
  const { ts, rest } = formatLogLine(line);
  const badge = LEVEL_BADGE[lvl];
  const segments = highlightSegments(rest, query);

  async function copyLine() {
    try {
      await navigator.clipboard.writeText(line);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard blocked — silently ignore for a single line */
    }
  }

  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "grid",
        gridTemplateColumns: "auto auto 1fr auto",
        gap: 8,
        alignItems: "baseline",
        padding: "1px 6px",
        margin: "0 -6px",
        borderRadius: "var(--r-xs)",
        background: hover ? "var(--surface-2)" : "transparent",
        color:
          lvl === "error"
            ? "var(--danger)"
            : lvl === "warn"
              ? "var(--warn)"
              : lvl === "debug"
                ? "var(--text-4)"
                : "var(--text-2)",
      }}
    >
      <span
        style={{
          color: "var(--text-4)",
          fontSize: 11,
          flexShrink: 0,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {ts}
      </span>
      <span
        style={{
          textTransform: "uppercase",
          fontSize: 9.5,
          fontWeight: 700,
          padding: "0 5px",
          borderRadius: 3,
          letterSpacing: "0.05em",
          flexShrink: 0,
          alignSelf: "center",
          background: badge.bg,
          color: badge.fg,
        }}
      >
        {lvl}
      </span>
      <span style={{ wordBreak: "break-word", whiteSpace: "pre-wrap" }}>
        {segments.map((seg, i) =>
          seg.match ? (
            <mark
              key={i}
              style={{
                background: "var(--accent-soft)",
                color: "var(--accent)",
                borderRadius: 2,
                padding: "0 1px",
              }}
            >
              {seg.text}
            </mark>
          ) : (
            <span key={i}>{seg.text}</span>
          ),
        )}
      </span>
      <button
        onClick={copyLine}
        title={t("row.copyTitle")}
        aria-label={t("row.copyTitle")}
        style={{
          alignSelf: "center",
          flexShrink: 0,
          border: "none",
          background: "transparent",
          color: copied ? "var(--success)" : "var(--text-4)",
          cursor: "pointer",
          fontSize: 10.5,
          fontFamily: "var(--font-sans)",
          padding: "1px 6px",
          borderRadius: "var(--r-xs)",
          opacity: hover || copied ? 1 : 0,
          transition: "opacity var(--t-fast)",
        }}
      >
        {copied ? t("row.copied") : t("row.copy")}
      </button>
    </div>
  );
});
