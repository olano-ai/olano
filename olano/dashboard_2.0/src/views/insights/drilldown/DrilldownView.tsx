import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  ChevronRight,
  Loader,
  RotateCw,
  User,
  Zap,
} from "lucide-react";
import { api } from "../../../core/api";
import { useCurrentAgent } from "../../../core/currentAgent";
import { usePersistentState } from "../../../core/usePersistentState";
import { parseRoute, viewPath } from "../../../core/routeUrls";
import { fmtInt, fmtCreditsAmount, fmtUsd, type Window } from "../helpers";
import { billingBooks, useCostDisplay } from "../../../core/costDisplay";
import { useT } from "../../../core/i18n";
import { STR } from "../strings";
import { LangfuseLink } from "../../observability/LangfuseLink";
import { sessionUrl, useLangfuseLinks } from "../../observability/links";
import { RawCallInspector } from "./RawCallInspector";
import { TurnTree } from "./TurnTree";
import type {
  TcAgentsResp,
  TcOrigins,
  TcThreadCalls,
  TcThreadsResp,
  TcToolRow,
  TcToolsResp,
} from "./drilldownTypes";

// Drill-down: agent → thread → individual LLM call, with a turn execution tree,
// per-tool cost attribution, an interactive/autonomous split, and a raw prompt
// inspector. Reads the checkpoint-mined /api/analytics/threadcost/* endpoints.
//
// EVERY dollar figure on this page is one book: an estimate of the customer's
// own provider bill, priced from list rates, with Olano-managed calls costed at
// $0 because this box is never told the gateway's rate. So it renders in
// dollars unconditionally (fmtUsd) and is never restated in credits — a unit
// that prices something else entirely, and at a 100×-a-cent rate produced the
// headline "~118,447 credits" for an agent on a fleet whose whole credit spend
// that month was 122. The managed book appears only where it genuinely exists:
// per agent, from the usage ledger. Threads and calls have no credits column
// because the ledger records per turn per agent, with no thread id to join on.

type View =
  | { level: "agents" }
  | { level: "threads"; agent: string }
  | { level: "calls"; agent: string; threadId: string; threadName: string };

// Last-resort label for a thread with no name and no preview. Threads written
// under the retired per-human namespace (`u:<user_id>:<id>`) exist on every
// multi-user box; a bare 8-char slice of one prints `u:abc12` — the namespace,
// with the id it was meant to show cut off — so the prefix comes off first.
function shortThreadId(id: string): string {
  return id.replace(/^u:[^:]+:/, "").slice(0, 8);
}

/** How many thread display names the drill-down remembers (ids only in the URL). */
const NAME_MEMORY = 200;

/** A drill position's address (see the `view` comment in DrilldownView). */
function drillHref(v: View): string {
  if (v.level === "agents") return "/insights/drill";
  const base = viewPath("insights", { section: "drill", agent: v.agent });
  return v.level === "calls" ? `${base}?thread=${encodeURIComponent(v.threadId)}` : base;
}

// `refreshKey` is an externally-driven reload (the toolbar's Refresh button); the
// returned `reload` is the local one (a panel's own retry). Either re-runs the
// fetch — they're summed so the two can't cancel each other out.
function useFetch<T>(
  path: string | null,
  refreshKey = 0,
): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!path) return;
    let live = true;
    setLoading(true);
    setError(null);
    api
      .get<T>(path)
      .then((d) => live && setData(d))
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : "error"))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [path, nonce, refreshKey]);
  return { data, error, loading, reload: () => setNonce((n) => n + 1) };
}

// A ranked row with a faint proportional cost bar behind the content. The
// right-hand metrics are the row's model-call count (work volume) and its
// dollar estimate — token columns were dropped from every usage surface.
// `credits` sits between them where the row has a managed charge too, kept as
// its own column rather than folded into the dollar one: the books price
// different calls and a single figure would be a sum of two currencies.
function CostRow({
  title,
  sub,
  usd,
  credits,
  calls,
  pct,
  estimated,
  onClick,
}: {
  title: React.ReactNode;
  sub?: React.ReactNode;
  usd: number;
  /** Olano-managed charge, rendered verbatim. Omitted where none applies. */
  credits?: number;
  /** Model calls this row covers. Omitted where the row isn't a call bucket. */
  calls?: number;
  pct: number;
  estimated?: boolean;
  onClick?: () => void;
}) {
  const t = useT(STR);
  return (
    <button className="lc-cost-row" onClick={onClick} disabled={!onClick} type="button">
      <span className="lc-cost-fill" style={{ width: `${Math.max(2, pct)}%` }} aria-hidden />
      <span className="lc-cost-main">
        <span className="lc-cost-title">{title}</span>
        {sub != null && <span className="muted lc-cost-sub">{sub}</span>}
      </span>
      <span className="lc-cost-metrics">
        {calls != null && (
          <span className="muted tabular" style={{ fontSize: 12, minWidth: 68, textAlign: "right" }}>
            {calls === 1 ? t("drill.callsBadge.one", { n: fmtInt(calls) }) : t("drill.callsBadge", { n: fmtInt(calls) })}
          </span>
        )}
        {credits != null && (
          <span className="tabular" style={{ fontSize: 12, minWidth: 84, textAlign: "right", color: "var(--accent)" }}>
            {fmtCreditsAmount(credits)}
          </span>
        )}
        <span className="tabular" style={{ fontWeight: 600, minWidth: 70, textAlign: "right" }}>
          {estimated ? "~" : ""}
          {fmtUsd(usd)}
        </span>
        {onClick && <ChevronRight size={14} className="lc-cost-chev" />}
      </span>
    </button>
  );
}

function Panel({ title, children, control }: { title: string; children: React.ReactNode; control?: React.ReactNode }) {
  return (
    <div className="card" style={{ padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
        <span style={{ fontWeight: 600, fontSize: 13.5 }}>{title}</span>
        {control}
      </div>
      {children}
    </div>
  );
}

function IndexBanner({ remaining, onRefresh }: { remaining: number; onRefresh: () => void }) {
  const t = useT(STR);
  if (!remaining) return null;
  return (
    <div className="card" style={{ padding: "10px 14px", marginBottom: 12, display: "flex", alignItems: "center", gap: 10, borderColor: "color-mix(in srgb, var(--warn) 35%, var(--border))" }}>
      <Loader size={15} className="lc-spin" style={{ color: "var(--warn)" }} />
      <span style={{ flex: 1, fontSize: 12.5 }}>
        {t("drill.indexing.before")}<strong>{fmtInt(remaining)}</strong>{t("drill.indexing.after")}
      </span>
      <button className="btn" style={{ display: "inline-flex", alignItems: "center", gap: 6 }} onClick={onRefresh}>
        <RotateCw size={14} /> {t("drill.indexMore")}
      </button>
    </div>
  );
}

// What this page reads, where someone comparing it to the Overview will see it.
// The agent ranking and the two origin cards are the same per-call usage ledger
// the Overview quotes, so those numbers match. Opening an agent switches to
// stored conversation history — the only record that ties a cost to a thread or
// a tool — and that history is compacted as conversations grow, so it accounts
// for a *part* of the total. Saying which half is which is what stops the gap
// from reading as one of the two pages being broken.
function SourceNote() {
  const t = useT(STR);
  return (
    <p className="muted" style={{ fontSize: 12, margin: "0 0 12px" }}>
      {t("drill.sourceNote")}
    </p>
  );
}

// "Openable conversations account for $X of $Y." Rendered only when the two
// genuinely differ — on a fleet whose threads are short they are the same
// number and the line would be noise.
function Coverage({ total, inspectable }: { total: number; inspectable?: number }) {
  const t = useT(STR);
  if (typeof inspectable !== "number" || !(total > 0)) return null;
  if (inspectable >= total * 0.995) return null;
  return (
    <p className="muted" style={{ fontSize: 11.5, marginTop: 10 }}>
      {t("drill.coverage", {
        inspectable: fmtUsd(inspectable),
        total: fmtUsd(total),
        pct: `${Math.round((inspectable / total) * 100)}%`,
      })}
    </p>
  );
}

// Descriptions for the two synthetic (non-tool) buckets. Keyed by the literal
// bucket name the backend returns (also rendered verbatim as the row title),
// so the key itself is a data identifier and stays in English — only the
// description text is localized.
function syntheticSub(tool: string, t: (key: string, vars?: Record<string, string>) => string): string | undefined {
  if (tool === "(system + context)") return t("drill.syntheticSystemContext");
  if (tool === "(model output)") return t("drill.syntheticModelOutput");
  return undefined;
}

function ToolCostPanel({ tools, note }: { tools: TcToolRow[]; note?: string }) {
  const t = useT(STR);
  const max = Math.max(1, ...tools.map((row) => row.usd));
  return (
    <Panel title={t("drill.costBreakdown")}>
      {tools.length === 0 ? (
        <p className="muted">{t("drill.noActivity")}</p>
      ) : (
        <div className="lc-rows">
          {tools.map((row) => {
            const synthetic = syntheticSub(row.tool, t);
            return (
              <CostRow
                key={row.tool}
                title={row.tool}
                sub={
                  synthetic ? (
                    synthetic
                  ) : (
                    <>
                      {row.calls === 1
                        ? t("drill.callsFed.one", { n: fmtInt(row.calls) })
                        : t("drill.callsFed", { n: fmtInt(row.calls) })}
                      {row.subagent_internal_usd > 0 &&
                        t("drill.downstreamSuffix", { usd: fmtUsd(row.subagent_internal_usd) })}
                    </>
                  )
                }
                usd={row.usd}
                pct={(row.usd / max) * 100}
              />
            );
          })}
        </div>
      )}
      <p className="muted" style={{ fontSize: 11, marginTop: 10 }}>
        {t("drill.toolCost.p1")}<b>{t("drill.toolCost.llmCost")}</b>
        {t("drill.toolCost.p2")}
        <b>(system + context)</b>{t("drill.toolCost.p3")}<b>(model output)</b>
        {t("drill.toolCost.p4")}
        {note}
      </p>
    </Panel>
  );
}

export function DrilldownView({
  days,
  refreshKey = 0,
  onLoadingChange,
}: {
  days?: Window;
  /** Bumped by the toolbar's Refresh button to re-run whatever level is showing. */
  refreshKey?: number;
  /** Reports in-flight state up so the toolbar button can spin for this tab's work. */
  onLoadingChange?: (loading: boolean) => void;
}) {
  const t = useT(STR);
  const { agent: currentAgent } = useCurrentAgent();
  // Not for the dollar figures — those never convert — but for which books this
  // deployment keeps, which decides whether a credits column belongs on the
  // agent rows at all. Subscribing also re-renders once the plan lands.
  const showCredits = billingBooks(useCostDisplay()).includes("credits");
  // The drill position (agent → thread → call) is the address:
  // /insights/drill, /insights/drill/<agent>, /insights/drill/<agent>?thread=<id>,
  // so a reload or a copied link lands on it and each step down is a Back
  // step. It is remembered for the session too, so the sidebar's bare link
  // returns to the same place, and so is each thread's display name (the URL
  // carries ids only). A stale agent/thread simply lands on the fetch's retry
  // card, which is acceptable. The Tree/Calls view-mode is a preference.
  const location = useLocation();
  const navigate = useNavigate();
  const [remembered, setRemembered] = usePersistentState<View>("insights.drill.view", { level: "agents" });
  const [names, setNames] = usePersistentState<Record<string, string>>("insights.drill.names", {});
  const [arrived, setArrived] = useState(false);
  const urlAgent = parseRoute(location.pathname).agent;
  const urlThread = new URLSearchParams(location.search).get("thread");
  const view = useMemo<View>(() => {
    if (urlAgent && urlThread)
      return { level: "calls", agent: urlAgent, threadId: urlThread, threadName: names[urlThread] ?? "" };
    if (urlAgent) return { level: "threads", agent: urlAgent };
    return arrived ? { level: "agents" } : remembered;
  }, [urlAgent, urlThread, names, arrived, remembered]);
  // Admin-only "Open in Langfuse" for the thread being drilled into: the
  // drill-down's thread id is the checkpoint-scoped id sessions are named by.
  const langfuseLinks = useLangfuseLinks(view.level === "agents" ? null : view.agent);
  useEffect(() => {
    if (arrived) return;
    setArrived(true);
    if (!urlAgent && remembered.level !== "agents") navigate(drillHref(remembered), { replace: true });
  }, [arrived, urlAgent, remembered, navigate]);
  useEffect(() => {
    if (arrived && drillHref(view) !== drillHref(remembered)) setRemembered(view);
  }, [arrived, view, remembered, setRemembered]);
  const setView = useCallback(
    (next: View) => {
      if (next.level === "calls" && next.threadName) {
        setNames((m) => {
          if (m[next.threadId] === next.threadName) return m;
          // Bounded: a session that drills into many threads keeps the newest
          // few hundred names, which is every thread anyone scrolls back to.
          const entries = Object.entries(m).filter(([id]) => id !== next.threadId).slice(-NAME_MEMORY);
          return Object.fromEntries([...entries, [next.threadId, next.threadName]]);
        });
      }
      const to = drillHref(next);
      if (to !== location.pathname + location.search) navigate(to);
    },
    [location.pathname, location.search, navigate, setNames],
  );
  const [raw, setRaw] = useState<{ ns: string; seq: number } | null>(null);
  const [showTree, setShowTree] = usePersistentState("insights.drill.execView", true);

  // Time range shared with the Overview tab, and sent on the wire exactly as
  // that tab sends it — "1 day" is the rolling last 24 hours in both places, so
  // one button can never mean two windows. What the range selects here is the
  // thread's last-active moment (this tab ranks threads and tools; the per-call
  // time series lives on the Overview). Omitted ⇒ all history.
  const range = days === 1 ? "hours=24" : `days=${days}`;
  const win = days ? `?${range}` : "";
  const agents = useFetch<TcAgentsResp>(view.level === "agents" ? `/api/analytics/threadcost/agents${win}` : null, refreshKey);
  const origins = useFetch<TcOrigins>(view.level === "agents" ? `/api/analytics/threadcost/origins${win}` : null, refreshKey);
  const globalTools = useFetch<TcToolsResp>(view.level === "agents" ? `/api/analytics/threadcost/tools${win}` : null, refreshKey);
  const threads = useFetch<TcThreadsResp>(
    view.level === "threads"
      ? `/api/analytics/threadcost/agents/${encodeURIComponent(view.agent)}/threads?limit=100${
          days ? `&${range}` : ""
        }`
      : null,
    refreshKey,
  );
  const calls = useFetch<TcThreadCalls>(
    view.level === "calls" ? `/api/analytics/threadcost/threads/${encodeURIComponent(view.threadId)}/calls` : null,
    refreshKey,
  );

  // Only the fetches the current level actually issues are in flight; the others
  // hold a null path and stay false, so the toolbar spinner tracks real work.
  const busy =
    agents.loading || origins.loading || globalTools.loading || threads.loading || calls.loading;
  useEffect(() => {
    onLoadingChange?.(busy);
  }, [busy, onLoadingChange]);
  // Leaving the tab mid-flight would otherwise strand the button in "spinning".
  useEffect(() => {
    return () => onLoadingChange?.(false);
  }, [onLoadingChange]);

  const openCall = useCallback((ns: string, seq: number) => setRaw({ ns, seq }), []);

  const crumb = (
    <div className="row" style={{ gap: 6, fontSize: 13, marginBottom: 14, flexWrap: "wrap" }}>
      <button className="lc-crumb" onClick={() => setView({ level: "agents" })} disabled={view.level === "agents"}>
        {t("drill.crumbAgents")}
      </button>
      {view.level !== "agents" && (
        <>
          <ChevronRight size={13} className="muted" />
          <button
            className="lc-crumb"
            onClick={() => setView({ level: "threads", agent: view.agent })}
            disabled={view.level === "threads"}
          >
            {view.agent}
          </button>
        </>
      )}
      {view.level === "calls" && (
        <>
          <ChevronRight size={13} className="muted" />
          <span className="lc-crumb is-current">{view.threadName || shortThreadId(view.threadId)}</span>
          <LangfuseLink href={sessionUrl(langfuseLinks, view.threadId)} kind="session" />
        </>
      )}
    </div>
  );

  return (
    <div>
      {crumb}

      {/* ── AGENTS ─────────────────────────────────────────────────────────── */}
      {view.level === "agents" && (
        <>
          {agents.data && <IndexBanner remaining={agents.data.index_status.remaining} onRefresh={agents.reload} />}
          <SourceNote />
          {origins.data && <OriginCards o={origins.data} />}
          {agents.loading && !agents.data ? (
            <div className="spinner" />
          ) : agents.error ? (
            <ErrorCard msg={agents.error} onRetry={agents.reload} />
          ) : (
            <div style={{ display: "grid", gap: 12, gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", marginTop: 12 }}>
              <Panel title={t("drill.agentsByCost")}>
                <AgentList
                  resp={agents.data}
                  current={currentAgent}
                  windowed={!!days}
                  showCredits={showCredits}
                  onPick={(a) => setView({ level: "threads", agent: a })}
                />
              </Panel>
              {globalTools.data && (
                <ToolCostPanel
                  tools={globalTools.data.tools}
                  note={globalTools.data.totals.capped ? t("drill.topThreadsOnly") : ""}
                />
              )}
            </div>
          )}
        </>
      )}

      {/* ── THREADS ────────────────────────────────────────────────────────── */}
      {view.level === "threads" && (
        <Panel title={t("drill.conversationsByCost", { agent: view.agent })}>
          {threads.loading && !threads.data ? (
            <div className="spinner" />
          ) : threads.error ? (
            <ErrorCard msg={threads.error} onRetry={threads.reload} />
          ) : !threads.data || threads.data.threads.length === 0 ? (
            <p className="muted">
              {days ? t("drill.noConversationsRange") : t("drill.noConversationsYet")}
            </p>
          ) : (
            <ThreadList
              resp={threads.data}
              onPick={(id, name) => setView({ level: "calls", agent: view.agent, threadId: id, threadName: name })}
            />
          )}
        </Panel>
      )}

      {/* ── CALLS ──────────────────────────────────────────────────────────── */}
      {view.level === "calls" && (
        <>
          {calls.loading && !calls.data ? (
            <div className="spinner" />
          ) : calls.error ? (
            <ErrorCard msg={calls.error} onRetry={calls.reload} />
          ) : calls.data ? (
            <div style={{ display: "grid", gap: 12 }}>
              <div style={{ display: "grid", gap: 12, gridTemplateColumns: "minmax(0,1.2fr) minmax(0,1fr)" }}>
                <Panel
                  title={t("drill.execution")}
                  control={
                    <div className="row" style={{ gap: 2, background: "var(--surface-2)", borderRadius: "var(--r-sm)", padding: 2 }}>
                      <button className={showTree ? "btn primary" : "btn ghost"} style={{ padding: "2px 8px", fontSize: 11.5 }} onClick={() => setShowTree(true)}>
                        {t("drill.tree")}
                      </button>
                      <button className={!showTree ? "btn primary" : "btn ghost"} style={{ padding: "2px 8px", fontSize: 11.5 }} onClick={() => setShowTree(false)}>
                        {t("drill.callsTab")}
                      </button>
                    </div>
                  }
                >
                  <ThreadTotals d={calls.data} />
                  {showTree ? (
                    <TurnTree nodes={calls.data.turn_tree} onOpenCall={openCall} />
                  ) : (
                    <CallList calls={calls.data.calls} onOpen={openCall} />
                  )}
                </Panel>
                <ToolCostPanel tools={calls.data.tools} />
              </div>
            </div>
          ) : null}
        </>
      )}

      {raw && view.level === "calls" && (
        <RawCallInspector threadId={view.threadId} ns={raw.ns} seq={raw.seq} onClose={() => setRaw(null)} />
      )}
    </div>
  );
}

// ── sub-views ─────────────────────────────────────────────────────────────────

function OriginCards({ o }: { o: TcOrigins }) {
  const t = useT(STR);
  const cards = [
    { label: t("drill.originInteractive"), icon: <User size={15} />, b: o.interactive },
    { label: t("drill.originAutonomous"), icon: <Zap size={15} />, b: o.non_interactive },
  ];
  return (
    <div style={{ display: "grid", gap: 12, gridTemplateColumns: "1fr 1fr", marginBottom: 12 }}>
      {cards.map((c) => (
        <div key={c.label} className="card" style={{ padding: 14 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="muted" style={{ fontSize: 12 }}>{c.label}</span>
            <span aria-hidden style={{ color: "var(--text-3)" }}>{c.icon}</span>
          </div>
          <div style={{ fontSize: 22, fontWeight: 700, marginTop: 4 }}>{fmtUsd(c.b.usd)}</div>
          <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
            {t("drill.originStats", { threads: fmtInt(c.b.thread_count), calls: fmtInt(c.b.call_count) })}
          </div>
        </div>
      ))}
    </div>
  );
}

function AgentList({
  resp,
  current,
  windowed,
  showCredits,
  onPick,
}: {
  resp: TcAgentsResp | null;
  current: string;
  windowed?: boolean;
  /** Does this deployment keep the credits book? */
  showCredits?: boolean;
  onPick: (a: string) => void;
}) {
  const t = useT(STR);
  if (!resp || resp.agents.length === 0)
    return <p className="muted">{windowed ? t("drill.noAgentActivityRange") : t("drill.noIndexedAgents")}</p>;
  const max = Math.max(1, ...resp.agents.map((a) => a.usd));
  // Only worth a column when the plan keeps that book AND something landed in
  // it — an all-zero credits column on a BYOK-heavy fleet is just noise.
  const creditsColumn = !!showCredits && resp.agents.some((a) => (a.credits ?? 0) > 0);
  return (
    <>
      <div className="lc-rows">
        {resp.agents.map((a) => (
          <CostRow
            key={a.agent}
            title={
              <>
                {a.agent}
                {a.agent === current && (
                  <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>· {t("drill.selected")}</span>
                )}
              </>
            }
            sub={
              <>
                {t("drill.threadsCount", { n: fmtInt(a.thread_count) })}
                {a.autonomous_usd != null &&
                  a.autonomous_usd > 0 &&
                  t("drill.autonomousSuffix", { usd: fmtUsd(a.autonomous_usd) })}
              </>
            }
            usd={a.usd}
            credits={creditsColumn ? (a.credits ?? 0) : undefined}
            calls={a.call_count}
            pct={(a.usd / max) * 100}
            estimated={!a.pricing_complete}
            onClick={() => onPick(a.agent)}
          />
        ))}
      </div>
      <Coverage total={resp.totals.usd} inspectable={resp.totals.inspectable_usd} />
      {/* Say which column is which. Without it the two money figures on a row
          read as one total in two units, which is the misreading this whole
          split exists to prevent. */}
      <p className="muted" style={{ fontSize: 11, marginTop: 10 }}>
        {creditsColumn ? (
          <>
            <b style={{ color: "var(--accent)" }}>{t("drill.creditsNote.credits")}</b>
            {t("drill.creditsNote.p1")}
            <b>{t("drill.creditsNote.dollars")}</b>
            {t("drill.creditsNote.p2")}
          </>
        ) : (
          t("drill.dollarsOnlyNote")
        )}
      </p>
    </>
  );
}

function ThreadList({ resp, onPick }: { resp: TcThreadsResp; onPick: (id: string, name: string) => void }) {
  const t = useT(STR);
  const max = Math.max(1, ...resp.threads.map((th) => th.usd));
  return (
    <div className="lc-rows">
      {resp.threads.map((th) => {
        const label = th.name || th.preview || shortThreadId(th.thread_id);
        return (
          <CostRow
            key={th.thread_id}
            title={label}
            sub={
              <>
                <span className={`lc-origin lc-origin-${th.origin}`}>{th.origin}</span>
                {th.subagent_call_count > 0 && t("drill.subagentCallsCount", { n: fmtInt(th.subagent_call_count) })}
              </>
            }
            usd={th.usd}
            calls={th.call_count}
            pct={(th.usd / max) * 100}
            estimated={!th.pricing_complete}
            onClick={() => onPick(th.thread_id, label)}
          />
        );
      })}
    </div>
  );
}

function ThreadTotals({ d }: { d: TcThreadCalls }) {
  const t = useT(STR);
  return (
    <div className="row" style={{ gap: 14, marginBottom: 12, flexWrap: "wrap", fontSize: 12.5 }}>
      <span className="tabular" style={{ fontWeight: 700, fontSize: 15 }}>{fmtUsd(d.totals.usd)}</span>
      <span className="muted">{t("drill.modelCallsCount", { n: fmtInt(d.totals.call_count) })}</span>
    </div>
  );
}

function CallList({ calls, onOpen }: { calls: TcThreadCalls["calls"]; onOpen: (ns: string, seq: number) => void }) {
  const t = useT(STR);
  const max = Math.max(1, ...calls.map((c) => c.usd));
  return (
    <div className="lc-rows">
      {calls.map((c) => (
        <CostRow
          key={`${c.ns}-${c.seq}`}
          title={
            <>
              #{c.seq}{" "}
              {c.tool_calls.length > 0
                ? c.tool_calls.map((tc) => tc.subagent_type || tc.name).join(", ")
                : t("drill.response")}
              {c.ns && <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>{t("drill.subagentBadge")}</span>}
            </>
          }
          sub={c.model}
          usd={c.usd}
          // No call count here: every row in this list *is* one model call.
          pct={(c.usd / max) * 100}
          estimated={!c.pricing_known}
          onClick={() => onOpen(c.ns, c.seq)}
        />
      ))}
    </div>
  );
}

function ErrorCard({ msg, onRetry }: { msg: string; onRetry: () => void }) {
  const t = useT(STR);
  return (
    <div className="card" style={{ padding: 24, textAlign: "center", borderColor: "color-mix(in srgb, var(--danger) 35%, var(--border))" }}>
      <div style={{ display: "flex", justifyContent: "center", color: "var(--danger)" }} aria-hidden>
        <AlertTriangle size={24} />
      </div>
      <p className="muted" style={{ marginTop: 8 }}>{msg}</p>
      <button className="btn primary" style={{ marginTop: 10 }} onClick={onRetry}>
        {t("drill.retry")}
      </button>
    </div>
  );
}
