// Agent-to-Agent — live feed of peer messages (talk_to_agent) and subagent
// delegations (task), organised as conversations.
//
// The shell: loads the conversation rollup + the selected conversation's
// transcript, folds live SSE frames into both, and owns the layout. The
// conversation model is in threads.ts (pure), the two panes in ThreadRail.tsx
// and Transcript.tsx.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, Trash2 } from "lucide-react";
import { usePersistentState } from "../../core/usePersistentState";
import { useRememberedUrlParam } from "../../core/useUrlParam";
import { subscribeStream, subscribeStreamState, getStreamState } from "../../core/agentStream";
import { subscribeResume, shouldRefetch } from "../../core/lifecycle";
import { api, ApiError } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { useMediaQuery, MOBILE_QUERY } from "../../core/useMediaQuery";
import { useToast } from "../../ui/toast";
import { useConfirm } from "../../ui/confirm";
import type { Agent } from "../../core/types";
import { STR } from "./strings";
import { activityStore } from "./activityStore";
import { ALL_KEY, ThreadRail, type KindFilter } from "./ThreadRail";
import { Transcript } from "./Transcript";
import type { A2AEvent, PairThread } from "./threads";
import { applyThreadFrame, foldExchanges, foldThreads, pairKey } from "./threads";

type T = (key: string, vars?: Record<string, string>) => string;

const SEARCH_DEBOUNCE_MS = 80;
// One transcript page, in raw events (a full exchange is two: send + reply).
const PAGE_EVENTS = 120;
// The "All activity" firehose is capped — it exists for a quick scan, not for
// reading history. That's what the per-conversation transcripts are for.
const ALL_FEED_EVENTS = 200;
// Fallback path only (runtime without /api/a2a/threads).
const FALLBACK_EVENTS = 500;

function threadsLabel(t: T, n: number): string {
  return t(n === 1 ? "count.threads.one" : "count.threads.other", { n: String(n) });
}
function msgsLabel(t: T, n: number): string {
  return t(n === 1 ? "count.msgs.one" : "count.msgs.other", { n: String(n) });
}

const SCOPED_CSS = `
/* Fill the shell's scroller exactly (globals.css .main is height:100%), rather
   than doing viewport-minus-topbar arithmetic — this view renders no .topbar of
   its own, and the desktop/mobile topbars differ. Only the rail and the
   transcript scroll; .main never gets a second scrollbar. */
.a2a-root { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.a2a-head { padding: 18px 24px 14px; flex-shrink: 0; }
/* min-width:0 on both cells: without it a long preview or a wide bubble makes
   the grid track overflow the page instead of ellipsing inside its own box. */
.a2a-split { flex: 1; min-height: 0; display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 16px; padding: 0 24px 20px; }
.a2a-split > * { min-width: 0; }

/* ── rail ── */
.a2a-rail { display: flex; flex-direction: column; min-height: 0; min-width: 0; padding: 8px; gap: 8px; }
.a2a-rail-head { display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; }
/* Block, not flex: flex would shrink the fixed-height rows the windowing
   maths depends on, and squash the auto-height "All activity" row. */
.a2a-rail-scroll { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; }
.a2a-rail-empty { padding: 18px 14px; color: var(--text-4); font-size: 12px; text-align: center; }
.a2a-segs { display: flex; flex-wrap: wrap; gap: 2px; padding: 2px; border-radius: var(--r-sm); background: var(--surface-2); }
.a2a-seg { flex: 1 1 auto; min-width: 0; display: inline-flex; align-items: center; justify-content: center; gap: 4px; padding: 5px 4px; font-size: 11.5px; font-weight: 600; white-space: nowrap; border: none; border-radius: var(--r-xs); background: transparent; color: var(--text-3); cursor: pointer; transition: all var(--t-fast) var(--ease); }
.a2a-seg:hover:not(:disabled) { color: var(--text); }
.a2a-seg.is-active { background: var(--surface); color: var(--text); box-shadow: var(--shadow-sm); }
.a2a-seg:disabled { opacity: 0.45; cursor: default; }
.a2a-seg-n { font-size: 10.5px; font-weight: 500; color: var(--text-4); font-variant-numeric: tabular-nums; }
.a2a-seg.is-active .a2a-seg-n { color: var(--text-3); }
.a2a-clear { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); display: inline-flex; align-items: center; border: none; background: transparent; color: var(--text-4); cursor: pointer; line-height: 1; padding: 2px 4px; }
.a2a-thread { padding: 9px 10px; margin-bottom: 2px; border-radius: var(--r-sm); border: 1px solid transparent; cursor: pointer; overflow: hidden; box-sizing: border-box; transition: background var(--t-fast) var(--ease), border-color var(--t-fast) var(--ease); }
.a2a-thread:hover { background: var(--surface-2); }
.a2a-thread.is-active { background: var(--accent-gradient-soft); border-color: color-mix(in srgb, var(--accent) 28%, transparent); }
.a2a-thread-top { display: flex; align-items: center; gap: 5px; min-width: 0; }
.a2a-thread-preview { font-size: 12px; color: var(--text-3); margin-top: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.a2a-thread-meta { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--text-4); margin-top: 5px; }
.a2a-name { font-family: var(--font-mono); font-weight: 600; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }

/* ── transcript pane ── */
.a2a-pane { display: flex; flex-direction: column; min-height: 0; min-width: 0; position: relative; background: var(--surface); border-radius: var(--r); box-shadow: var(--shadow-sm); }
.a2a-pane-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 14px; border-bottom: 1px solid var(--border); background: var(--surface-2); border-radius: var(--r) var(--r) 0 0; flex-shrink: 0; }
.a2a-pane-meta { display: flex; gap: 8px; margin-left: auto; font-size: 11.5px; color: var(--text-3); flex-shrink: 0; }
.a2a-pane-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 4px 14px 16px; }
.a2a-older { display: flex; justify-content: center; padding: 10px 0 4px; font-size: 11.5px; }
.a2a-day { display: flex; align-items: center; gap: 10px; margin: 14px 0 10px; font-size: 11px; font-weight: 600; color: var(--text-4); text-transform: uppercase; letter-spacing: 0.04em; }
.a2a-day::before, .a2a-day::after { content: ""; flex: 1; height: 1px; background: var(--border); }
.a2a-exchange { display: flex; flex-direction: column; gap: 8px; margin-bottom: 14px; content-visibility: auto; contain-intrinsic-size: 0 220px; }
.a2a-exchange-pair { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--text-4); }
.a2a-bubble-row { display: flex; align-items: flex-start; gap: 10px; max-width: 720px; min-width: 0; }
.a2a-bubble { min-width: 0; border-radius: 12px; padding: 10px 13px; display: flex; flex-direction: column; }
.a2a-bubble-head { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 4px; }
.a2a-bubble-head.is-in { flex-direction: row-reverse; }
.a2a-stamp { color: var(--text-4); font-size: 10.5px; font-variant-numeric: tabular-nums; }
.a2a-elapsed { color: var(--text-3); font-size: 10.5px; font-variant-numeric: tabular-nums; background: var(--surface-3); padding: 0 5px; border-radius: var(--r-pill); }
.a2a-noreply { align-self: flex-end; font-size: 11.5px; color: var(--text-4); font-style: italic; border: 1px dashed var(--border-strong); border-radius: var(--r-pill); padding: 3px 10px; cursor: help; }
.a2a-jump { position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%); display: inline-flex; align-items: center; gap: 6px; box-shadow: var(--glow); font-size: 12.5px; padding: 6px 14px; }
.a2a-feed-empty { margin: auto; text-align: center; color: var(--text-3); max-width: 420px; padding: 40px 20px; }
.a2a-empty-icon { display: inline-flex; width: 44px; height: 44px; align-items: center; justify-content: center; border-radius: 50%; background: var(--surface-2); color: var(--text-4); margin-bottom: 12px; }

/* ── live pill ── */
.a2a-live { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 600; padding: 3px 10px 3px 8px; border-radius: var(--r-pill); border: 1px solid var(--border); background: var(--surface-2); color: var(--text-3); }
.a2a-live.is-live { color: var(--success); border-color: color-mix(in srgb, var(--success) 35%, transparent); background: var(--success-soft); }
.a2a-live .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-4); flex-shrink: 0; }
.a2a-live.is-live .dot { background: var(--success); box-shadow: 0 0 0 0 color-mix(in srgb, var(--success) 70%, transparent); animation: a2a-pulse 1.8s ease-out infinite; }
@keyframes a2a-pulse { 0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--success) 60%, transparent); } 70% { box-shadow: 0 0 0 6px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
.a2a-root .typing-dots { display: inline-flex; gap: 4px; align-items: center; padding: 6px 0; }
.a2a-root .typing-dots span { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); animation: a2a-bounce 1.4s infinite ease-in-out; }
.a2a-root .typing-dots span:nth-child(2) { animation-delay: 0.15s; }
.a2a-root .typing-dots span:nth-child(3) { animation-delay: 0.3s; }
@keyframes a2a-bounce { 0%,60%,100% { transform: translateY(0); opacity: 0.5; } 30% { transform: translateY(-4px); opacity: 1; } }

/* ── markdown body (globals.css is off-limits to this view) ── */
.a2a-root .md-body { font-size: 13px; line-height: 1.6; color: var(--text); word-wrap: break-word; overflow-wrap: anywhere; min-width: 0; width: 100%; max-width: 100%; }
.a2a-root .md-body > :first-child { margin-top: 0; }
.a2a-root .md-body > :last-child { margin-bottom: 0; }
.a2a-root .md-body p { margin: 0 0 6px; }
.a2a-root .md-body p:last-child { margin-bottom: 0; }
.a2a-root .md-body h1, .a2a-root .md-body h2, .a2a-root .md-body h3 { margin: 8px 0 4px; line-height: 1.25; }
.a2a-root .md-body h1 { font-size: 1.25em; }
.a2a-root .md-body h2 { font-size: 1.15em; }
.a2a-root .md-body h3 { font-size: 1.05em; }
.a2a-root .md-body ul, .a2a-root .md-body ol { margin: 4px 0 4px 22px; padding: 0; }
.a2a-root .md-body li { margin-bottom: 2px; }
.a2a-root .md-body a { color: var(--accent); }
.a2a-root .md-body code:not(pre code) {
  background: var(--code-bg); padding: 1px 5px; border-radius: 4px;
  font-size: 0.9em; border: 1px solid var(--code-border); font-family: var(--font-mono);
}
.a2a-root .md-body pre {
  position: relative; background: var(--code-bg); border: 1px solid var(--code-border);
  border-radius: 8px; padding: 10px 12px; margin: 6px 0; overflow-x: auto; max-width: 100%;
  font-size: 12px; line-height: 1.55; font-family: var(--font-mono);
}
.a2a-root .md-body pre code { background: none; padding: 0; border: 0; }
.a2a-root .md-body pre .code-copy {
  position: absolute; top: 6px; right: 6px; padding: 2px 8px; font-size: 11px;
  border-radius: var(--r-xs); border: 1px solid var(--border-strong);
  background: var(--surface); color: var(--text-2); cursor: pointer; opacity: 0.85;
  font-family: var(--font-sans); transition: all var(--t-fast) var(--ease);
}
.a2a-root .md-body pre .code-copy:hover { opacity: 1; background: var(--surface-2); }
.a2a-root .md-body .md-table-wrap { overflow-x: auto; max-width: 100%; margin: 6px 0; }
.a2a-root .md-body table { border-collapse: collapse; margin: 0; font-size: 12px; }
.a2a-root .md-body th, .a2a-root .md-body td { border: 1px solid var(--border); padding: 4px 8px; text-align: left; }
.a2a-root .md-body th { background: var(--surface-2); font-weight: 600; }

/* ── mobile: drill down instead of squashing both panes together ── */
@media (max-width: 900px) {
  .a2a-head { padding: 12px 14px 10px; }
  /* The blurb costs a third of a phone screen and the list is the point. */
  .a2a-desc { display: none; }
  .a2a-split { grid-template-columns: minmax(0, 1fr); padding: 0 10px 10px; }
  .a2a-bubble-row { max-width: 100%; }
  .a2a-pane-scroll { padding: 4px 10px 16px; }
}
`;

export function ActivityView() {
  const toast = useToast();
  const t = useT(STR);
  const confirm = useConfirm();
  const isAdmin = !!useAuth().user?.is_admin;
  const isMobile = useMediaQuery(MOBILE_QUERY);

  const [agents, setAgents] = useState<Agent[]>([]);
  const [threads, setThreads] = useState<PairThread[]>([]);
  const [threadsLoading, setThreadsLoading] = useState(true);
  // Non-null ⇒ this runtime has no /api/a2a/threads; everything is derived
  // client-side from a bounded tail of the raw log instead.
  const [fallback, setFallback] = useState<A2AEvent[] | null>(null);

  // New persisted keys: the old `activity.selectedKey` held `from->to::<ts>`
  // group ids that can never match a conversation key, so they are simply
  // orphaned rather than migrated.
  //
  // The open conversation is the URL's `?pair=<key>`, read on every change, so
  // Back/Forward and a link clicked while this view is open both move it, and
  // opening one (or going back to the list on a phone) is a Back step. The key
  // is still remembered for the session, so the sidebar's bare /activity
  // reopens it, as before.
  //
  // ALL_KEY is "no conversation": null in the URL and in the memory.
  const [pairParam, setPairParam] = useRememberedUrlParam("pair", "activity.threadKey", { history: "push" });
  const selectedKey = pairParam || ALL_KEY;
  const setSelectedKey = useCallback(
    (key: string, history: "push" | "replace" = "push") =>
      setPairParam(key === ALL_KEY ? null : key, { history }),
    [setPairParam],
  );
  const [kind, setKind] = usePersistentState<KindFilter>("activity.kind", "all");
  const [queryInput, setQueryInput] = usePersistentState<string>("activity.q", "");
  const [query, setQuery] = useState(queryInput);

  const [events, setEvents] = useState<A2AEvent[]>([]);
  const [transcriptLoading, setTranscriptLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [connected, setConnected] = useState(false);
  // Re-render every 30s so relative labels ("2m ago") keep ticking on an
  // otherwise idle page, and pending exchanges age into "no reply".
  const [nowMs, setNowMs] = useState(() => Date.now());

  const selectedRef = useRef(selectedKey);
  selectedRef.current = selectedKey;

  const roster = useMemo(() => {
    const m = new Map<string, Agent>();
    for (const a of agents) m.set(a.name, a);
    return m;
  }, [agents]);

  const selectedThread = useMemo(
    () => (selectedKey === ALL_KEY ? null : threads.find((x) => x.key === selectedKey) ?? null),
    [threads, selectedKey],
  );

  useEffect(() => {
    const id = window.setInterval(() => {
      if (!document.hidden) setNowMs(Date.now());
    }, 30_000);
    return () => window.clearInterval(id);
  }, []);

  // Debounce the rail search: `q` is served by the backend, which searches
  // message and reply bodies the client never holds.
  useEffect(() => {
    const id = window.setTimeout(() => setQuery(queryInput), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [queryInput]);

  useEffect(() => {
    let cancelled = false;
    api
      .cachedGet<Agent[]>("/api/agents")
      .then((list) => {
        if (!cancelled) setAgents(list);
      })
      .catch(() => {
        if (!cancelled) toast.push(t("toast.loadAgents"), "error");
      });
    return () => {
      cancelled = true;
    };
  }, [toast, t]);

  // ── conversation rollup ─────────────────────────────────────────────────
  const loadThreads = useCallback(async () => {
    setThreadsLoading(true);
    const qs = new URLSearchParams();
    if (query.trim()) qs.set("q", query.trim());
    try {
      const rows = await api.get<PairThread[]>(`/api/a2a/threads?${qs}`);
      setThreads(rows || []);
      setFallback(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // Older runtime: fold client-side from the raw tail. Everything still
        // works, just over a much shorter history.
        try {
          const raw = await api.get<A2AEvent[]>(`/api/a2a/activity?limit=${FALLBACK_EVENTS}`);
          setFallback(raw || []);
        } catch {
          setFallback([]);
        }
      } else {
        setThreads([]);
      }
    } finally {
      setThreadsLoading(false);
    }
  }, [query]);

  useEffect(() => {
    void loadThreads();
  }, [loadThreads]);

  // Fallback mode derives the rail from the raw tail, filtered by the same
  // query the server would have applied.
  useEffect(() => {
    if (fallback === null) return;
    const q = query.trim().toLowerCase();
    const rows = foldThreads(fallback).filter(
      (r) =>
        !q ||
        r.a.toLowerCase().includes(q) ||
        r.b.toLowerCase().includes(q) ||
        r.last_preview.toLowerCase().includes(q),
    );
    setThreads(rows);
  }, [fallback, query]);

  // ── transcript ──────────────────────────────────────────────────────────
  const fetchPage = useCallback(
    async (key: string, before?: string): Promise<A2AEvent[]> => {
      const qs = new URLSearchParams();
      if (key === ALL_KEY) {
        qs.set("limit", String(ALL_FEED_EVENTS));
      } else {
        // "<kind>:<a>::<b>" — split on the FIRST colon only; the pair itself is
        // joined with "::" and a plain split(":") would shred it.
        const sep = key.indexOf(":");
        const [a, b] = key.slice(sep + 1).split("::");
        qs.set("kind", key.slice(0, sep));
        if (a) qs.set("a", a);
        if (b) qs.set("b", b);
        qs.set("limit", String(PAGE_EVENTS));
      }
      if (before) qs.set("before", before);
      return (await api.get<A2AEvent[]>(`/api/a2a/activity?${qs}`)) || [];
    },
    [],
  );

  useEffect(() => {
    if (fallback !== null) return; // fallback derives locally, below
    let cancelled = false;
    setTranscriptLoading(true);
    fetchPage(selectedKey)
      .then((page) => {
        if (cancelled) return;
        setEvents(page);
        const want = selectedKey === ALL_KEY ? ALL_FEED_EVENTS : PAGE_EVENTS;
        setHasMore(page.length >= want);
      })
      .catch(() => {
        if (!cancelled) {
          setEvents([]);
          setHasMore(false);
        }
      })
      .finally(() => {
        if (!cancelled) setTranscriptLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedKey, fetchPage, fallback]);

  useEffect(() => {
    if (fallback === null) return;
    setEvents(
      selectedKey === ALL_KEY
        ? fallback.slice(-ALL_FEED_EVENTS)
        : fallback.filter((ev) => ev.data && pairKey(ev.data) === selectedKey),
    );
    setHasMore(false);
  }, [fallback, selectedKey]);

  const loadOlder = useCallback(async () => {
    const oldest = events[0]?.ts;
    if (!oldest) return;
    setTranscriptLoading(true);
    try {
      const page = await fetchPage(selectedKey, oldest);
      if (page.length) setEvents((prev) => [...page, ...prev]);
      const want = selectedKey === ALL_KEY ? ALL_FEED_EVENTS : PAGE_EVENTS;
      setHasMore(page.length >= want);
    } catch {
      setHasMore(false);
    } finally {
      setTranscriptLoading(false);
    }
  }, [events, fetchPage, selectedKey]);

  // ── live stream ─────────────────────────────────────────────────────────
  useEffect(() => {
    setConnected(getStreamState() === "open");
    const offState = subscribeStreamState((s) => setConnected(s === "open"));
    const offStream = subscribeStream((frame) => {
      const ev = frame as A2AEvent;
      if (ev.type !== "agent_message" || !ev.data) return;
      setThreads((prev) => applyThreadFrame(prev, ev));
      const key = pairKey(ev.data);
      if (selectedRef.current === ALL_KEY || selectedRef.current === key) {
        setEvents((prev) => [...prev, ev]);
      }
    });
    return () => {
      offState();
      offStream();
    };
  }, []);

  // A suspended tab loses frames with no replay, so rebuild wholesale rather
  // than trusting the patched-in-place rollup.
  useEffect(
    () =>
      subscribeResume((e) => {
        if (!shouldRefetch(e)) return;
        void loadThreads();
        fetchPage(selectedRef.current)
          .then(setEvents)
          .catch(() => {});
      }),
    [loadThreads, fetchPage],
  );

  useEffect(() => {
    activityStore.markViewing();
    return () => activityStore.clearViewing();
  }, []);

  const exchanges = useMemo(() => foldExchanges(events, nowMs), [events, nowMs]);
  const totalMessages = useMemo(
    () => threads.reduce((n, x) => n + x.messages, 0),
    [threads],
  );

  const clearActivity = useCallback(async () => {
    if (!(await confirm({ message: t("confirm.clear"), danger: true }))) return;
    try {
      await api.del("/api/a2a/activity");
      api.invalidate("/api/a2a");
      setThreads([]);
      setEvents([]);
      setFallback(null);
      // Cleared: no conversation is left to go Back to.
      setSelectedKey(ALL_KEY, "replace");
      setQueryInput("");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "unknown error";
      toast.push(t("toast.clearFail", { error: msg }), "error");
    }
  }, [confirm, toast, t, setSelectedKey, setQueryInput]);

  // On mobile the two panes become one: the rail *is* the page until a
  // conversation is picked, then the transcript replaces it.
  const showRail = !isMobile || selectedKey === ALL_KEY;
  const showPane = !isMobile || selectedKey !== ALL_KEY;

  return (
    <div className="a2a-root">
      <style>{SCOPED_CSS}</style>

      <div className="a2a-head row" style={{ justifyContent: "space-between", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
            <div style={{ fontSize: 22, fontWeight: 600, letterSpacing: "-0.025em", lineHeight: 1.15 }}>
              {t("title")}
            </div>
            <span
              className={`a2a-live ${connected ? "is-live" : ""}`}
              title={connected ? t("live.title") : t("offline.title")}
            >
              <span className="dot" />
              {connected ? t("live") : t("offline")}
            </span>
            {fallback !== null && (
              <span className="badge" title={t("limited.title")}>
                {t("limited")}
              </span>
            )}
          </div>
          <div
            className="muted a2a-desc"
            style={{ fontSize: 13, marginTop: 6, lineHeight: 1.5, maxWidth: 720 }}
          >
            {t("desc.pre")}
            <code>talk_to_agent</code>
            {t("desc.mid")}
            <code>task</code>
            {t("desc.post")}
          </div>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {threads.length > 0 && (
            <span
              className="badge"
              title={t("summary.full", {
                threads: threadsLabel(t, threads.length),
                msgs: t(totalMessages === 1 ? "count.msgsFull.one" : "count.msgsFull.other", {
                  n: String(totalMessages),
                }),
              })}
            >
              {t("summary", {
                threads: threadsLabel(t, threads.length),
                msgs: msgsLabel(t, totalMessages),
              })}
            </span>
          )}
          {isAdmin && (
            <button
              className="btn danger"
              onClick={() => void clearActivity()}
              disabled={threads.length === 0}
            >
              <Trash2 size={14} strokeWidth={1.75} /> {t("btn.clear")}
            </button>
          )}
        </div>
      </div>

      <div className="a2a-split">
        {showRail && (
          <ThreadRail
            threads={threads}
            totalMessages={totalMessages}
            selectedKey={selectedKey}
            onSelect={(key) => setSelectedKey(key)}
            queryInput={queryInput}
            onQueryChange={setQueryInput}
            kind={kind}
            onKindChange={setKind}
            roster={roster}
            loading={threadsLoading}
          />
        )}
        {showPane &&
          (threads.length === 0 && !threadsLoading && selectedKey === ALL_KEY ? (
            <div className="a2a-pane">
              <div className="a2a-feed-empty">
                <span className="a2a-empty-icon">
                  <Activity size={20} strokeWidth={1.75} />
                </span>
                <div style={{ fontSize: 14.5, fontWeight: 600, color: "var(--text)", marginBottom: 4 }}>
                  {t("empty.title")}
                </div>
                <div style={{ fontSize: 12.5, lineHeight: 1.55 }}>
                  {t("empty.pre")}
                  <code>talk_to</code>
                  {t("empty.mid")}
                  <code>emily-assistant</code>
                  {t("empty.post")}
                </div>
              </div>
            </div>
          ) : (
            <Transcript
              thread={selectedThread}
              exchanges={exchanges}
              roster={roster}
              loading={transcriptLoading}
              hasMore={hasMore}
              onLoadOlder={() => void loadOlder()}
              onBack={() => setSelectedKey(ALL_KEY)}
              showBack={isMobile}
              showPairHeaders={selectedKey === ALL_KEY}
            />
          ))}
      </div>
    </div>
  );
}
