// The daily work journal: a day picker + the selected day's metrics, AI
// highlights (generated on demand), per-agent table, and notable activity.
// "Copy markdown" renders the same shape as scripts/journal_to_markdown.py so
// operators get an identical digest without the backend.

import { useCallback, useEffect, useState } from "react";
import { Sparkles, ClipboardCopy, RefreshCw, ChevronLeft, ChevronRight } from "lucide-react";
import { api } from "../../core/api";
import { useI18n, useT } from "../../core/i18n";
import { useToast } from "../../ui/toast";
import { useAuth } from "../../core/auth";
import { Markdown } from "../../ui/Markdown";
import { usePersistentState } from "../../core/usePersistentState";
import { fmtDay, fmtDayLabel, fmtInt, fmtCreditsAmount, fmtUsd, shiftDay } from "./helpers";
import { billingBooks, useCostDisplay } from "../../core/costDisplay";
import type { BillingBook } from "../../core/costDisplay";
import { STR } from "./strings";
import { ensureInsightsStyles } from "./styles";
import { ReportPanel } from "./ReportPanel";
import type { JournalAgentRow, JournalEntry, JournalList } from "./types";

// A journal day carries both accounting books, and they are never summed:
// `credits` is the Olano gateway's authoritative charge for managed calls,
// `usd` the list-price estimate of the customer's own provider bill (0 for
// managed calls by design). Which columns a deployment shows follows the plan —
// see billingBooks(). Before this split the panel rendered `usd` alone through
// the display-unit conversion, so a credits deployment saw its BYOK cents
// *converted* and labelled "credits": a number from the wrong book, in a unit it
// was never in, with the day's actual credit charge missing entirely.
const journalCredits = (r: { credits?: number }): number => r.credits ?? 0;

function journalToMarkdown(
  e: JournalEntry,
  books: BillingBook[],
  t: (key: string, vars?: Record<string, string>) => string,
): string {
  const m = e.metrics;
  const showCredits = books.includes("credits");
  const showByok = books.includes("byok");
  const L: string[] = [t("journal.md.title", { date: e.date }), ""];
  if (e.narrative) L.push(`> ${e.narrative.replace(/\n/g, "\n> ")}`, "");
  L.push(t("journal.md.summary"), "");
  if (showCredits) L.push(t("journal.md.creditsLine", { value: fmtCreditsAmount(journalCredits(m)) }));
  if (showByok) L.push(t("journal.md.apiCostLine", { value: fmtUsd(m.usd) }));
  L.push(
    t("journal.md.callsLine", { value: fmtInt(m.calls) }),
    t("journal.md.toolCallsLine", { value: fmtInt(m.tool_calls) }),
    t("journal.md.a2aLine", { value: fmtInt(m.a2a_messages) }),
    t("journal.md.activeAgentsLine", {
      value: `${m.active_agents.length}${m.active_agents.length ? ` (${m.active_agents.join(", ")})` : ""}`,
    }),
    "",
  );
  if (e.by_agent.length) {
    const cols = [
      ...(showCredits ? [t("journal.md.colCredits")] : []),
      ...(showByok ? [t("journal.md.colApiCost")] : []),
      t("journal.md.colModelCalls"),
      t("journal.md.colToolCalls"),
      t("journal.md.colA2a"),
      t("journal.md.colTopTools"),
    ];
    L.push(
      t("journal.md.byAgent"),
      "",
      `| ${t("journal.md.colAgent")} | ${cols.join(" | ")} |`,
      `|---|${cols.map((_, i) => (i === cols.length - 1 ? "---" : "--:")).join("|")}|`,
    );
    for (const a of e.by_agent) {
      const top = (a.top_tools || []).slice(0, 5).map((tt) => `\`${tt.tool}\`×${tt.count}`).join(", ");
      const cells = [
        ...(showCredits ? [fmtCreditsAmount(journalCredits(a))] : []),
        ...(showByok ? [fmtUsd(a.usd)] : []),
        fmtInt(a.calls),
        fmtInt(a.tool_calls),
        `${a.a2a_sent}/${a.a2a_received}`,
        top || "—",
      ];
      L.push(`| ${a.agent} | ${cells.join(" | ")} |`);
    }
    L.push("");
  }
  const acts = e.events?.cognition_actions || [];
  const cx = e.events?.cortex || [];
  if (acts.length || cx.length) {
    L.push(t("journal.md.notableActivity"), "");
    for (const a of acts) L.push(`- **${a.action}** — ${a.summary || a.action}`);
    for (const c of cx)
      L.push(
        `- ${c.created ? t("journal.cortexCreated") : t("journal.cortexUpdated")} ${c.kind} _${c.title}_ → \`${c.status}\``,
      );
    L.push("");
  }
  return L.join("\n").trimEnd() + "\n";
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div className="muted" style={{ fontSize: 11 }}>{label}</div>
      <div style={{ fontSize: 17, fontWeight: 700, lineHeight: 1.2, fontVariantNumeric: "tabular-nums" }}>{value}</div>
    </div>
  );
}

// What the per-agent ranking sorts and sizes its bars by. The two money
// metrics are offered only where that book exists — ranking a credits-only
// fleet by a $0 column just sorts it alphabetically.
type AgentMetric = "credits" | "usd" | "calls" | "tools";
const AGENT_METRICS: { id: AgentMetric; labelKey: string; book?: BillingBook }[] = [
  { id: "credits", labelKey: "journal.credits", book: "credits" },
  { id: "usd", labelKey: "journal.apiCost", book: "byok" },
  { id: "calls", labelKey: "journal.calls" },
  { id: "tools", labelKey: "journal.toolCalls" },
];

function agentMetricValue(a: JournalAgentRow, metric: AgentMetric): number {
  if (metric === "credits") return journalCredits(a);
  if (metric === "usd") return a.usd;
  return metric === "calls" ? a.calls : a.tool_calls;
}

function formatAgentMetric(v: number, metric: AgentMetric): string {
  if (metric === "credits") return fmtCreditsAmount(v);
  if (metric === "usd") return fmtUsd(v);
  return fmtInt(v);
}

export function JournalFeed() {
  const t = useT(STR);
  // Which accounting books this deployment keeps — decides whether the day
  // shows a credits figure, a dollar figure, or both. Subscribing also
  // re-renders the panel when the deployment's unit settings land.
  const books = billingBooks(useCostDisplay());
  const showCredits = books.includes("credits");
  const showByok = books.includes("byok");
  const { lang } = useI18n();
  const toast = useToast();
  // Highlights are scoped server-side to the viewer's agents; generation (a paid
  // LLM pass over the whole fleet) is admin-only.
  const isAdmin = !!useAuth().user?.is_admin;
  // Daily journal vs. weekly/monthly rollup reports (over the same per-agent
  // highlights). Persisted so the tab stays where the user left it.
  const [period, setPeriod] = usePersistentState<"daily" | "weekly" | "monthly">(
    "insights.journal.period",
    "daily",
  );
  const [list, setList] = useState<JournalList | null>(null);
  // Persisted so returning to Insights lands on the same day the user was
  // reading. The fetch below only seeds it when nothing is selected yet.
  const [day, setDay] = usePersistentState<string | null>("insights.journal.day", null);
  const [entry, setEntry] = useState<JournalEntry | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  // Default to the money metric of the leading book (credits where one exists,
  // else dollars) — "who spent the most today" is what this list is for. A
  // choice stored before the split ("usd") is still valid and is respected.
  const [storedMetric, setMetric] = usePersistentState<AgentMetric>(
    "insights.journal.metric",
    "credits",
  );
  // Only the metrics this deployment's books support. A stored choice for a
  // book the plan doesn't keep (or a preference saved before the split) falls
  // back to the first offered one rather than ranking every agent by zero.
  const agentMetrics = AGENT_METRICS.filter((m) => !m.book || books.includes(m.book));
  const metric: AgentMetric = agentMetrics.some((m) => m.id === storedMetric)
    ? storedMetric
    : agentMetrics[0].id;

  useEffect(() => ensureInsightsStyles(), []);

  useEffect(() => {
    api
      .get<JournalList>("/api/analytics/journal")
      .then((l) => {
        setList(l);
        // Open on the most recent day that has activity (falls back to today),
        // so the panel isn't an empty "today" when work happened earlier.
        setDay((cur) => cur ?? l.latest_active ?? l.today);
      })
      .catch(() => setList({ days: [], today: "" }));
  }, []);

  const loadDay = useCallback((d: string) => {
    setLoading(true);
    api
      .get<JournalEntry>(`/api/analytics/journal/${d}`)
      .then(setEntry)
      .catch(() => setEntry(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (day) loadDay(day);
  }, [day, loadDay]);

  async function generate() {
    if (!day) return;
    setGenerating(true);
    try {
      const updated = await api.post<JournalEntry>(`/api/analytics/journal/${day}/highlights`);
      setEntry(updated);
      if (updated.narrative && !updated.narrative_error) {
        toast.push(t("toast.highlights"), "info");
      } else {
        // Surface the backend's reason (missing model, provider auth, …) so
        // the operator can actually fix it instead of guessing.
        const reason = updated.narrative_error;
        toast.push(reason ? `${t("toast.highlightsFailed")}: ${reason}` : t("toast.highlightsFailed"), "error");
      }
    } catch {
      toast.push(t("toast.highlightsFailed"), "error");
    } finally {
      setGenerating(false);
    }
  }

  function copyMarkdown() {
    if (!entry) return;
    void navigator.clipboard.writeText(journalToMarkdown(entry, books, t));
    toast.push(t("journal.copied"));
  }

  // Selectable days — union of on-disk days, today, yesterday, the latest-active
  // day (which may not have a journal file yet), and the persisted selection,
  // newest first. Today/yesterday are always offered even without activity so
  // the quick picks never dead-end.
  const yesterday = list ? shiftDay(list.today, -1) : null;
  const days = list
    ? Array.from(
        new Set([list.today, yesterday, list.latest_active, day, ...list.days].filter(Boolean) as string[]),
      )
        .sort()
        .reverse()
    : [];
  const dayIdx = day ? days.indexOf(day) : -1;
  // The list is newest-first: "older" walks forward in the array, "newer" back.
  const olderDay = dayIdx >= 0 && dayIdx < days.length - 1 ? days[dayIdx + 1] : null;
  const newerDay = dayIdx > 0 ? days[dayIdx - 1] : null;

  const dayLabel = (d: string): string => {
    if (d === list?.today) return `${t("journal.today")} · ${fmtDay(d)}`;
    if (d === yesterday) return `${t("journal.yesterday")} · ${fmtDay(d)}`;
    return fmtDayLabel(d, lang);
  };

  const isEmptyDay =
    !!entry &&
    entry.metrics.calls === 0 &&
    entry.metrics.tool_calls === 0 &&
    entry.metrics.a2a_messages === 0 &&
    entry.by_agent.length === 0;

  return (
    <div className="card" style={{ padding: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <div>
          <div style={{ fontWeight: 600, fontSize: 14 }}>{t("journal.heading")}</div>
          <p className="muted" style={{ margin: "2px 0 0", fontSize: 12.5 }}>{t("journal.desc")}</p>
        </div>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {/* Daily / Weekly / Monthly — the daily journal or a rollup report */}
          <div className="row" style={{ gap: 2, background: "var(--surface-2)", borderRadius: "var(--r-sm)", padding: 2 }}>
            {(["daily", "weekly", "monthly"] as const).map((p) => (
              <button
                key={p}
                className={p === period ? "btn primary" : "btn ghost"}
                style={{ padding: "4px 10px", fontSize: 12.5 }}
                onClick={() => setPeriod(p)}
              >
                {t(`period.${p}`)}
              </button>
            ))}
          </div>
          {period === "daily" && entry && (
            <button
              className="btn"
              style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 10px", fontSize: 12.5 }}
              onClick={copyMarkdown}
            >
              <ClipboardCopy size={14} strokeWidth={1.75} /> {t("journal.export")}
            </button>
          )}
        </div>
      </div>

      {period !== "daily" ? (
        <ReportPanel period={period} isAdmin={isAdmin} />
      ) : (
        <>
      {days.length === 0 ? (
        <p className="muted" style={{ fontSize: 12.5, marginTop: 12 }}>{t("journal.empty")}</p>
      ) : (
        <div className="row" style={{ gap: 8, flexWrap: "wrap", margin: "12px 0 4px" }}>
          {/* Quick picks */}
          <div className="row" style={{ gap: 2, background: "var(--surface-2)", borderRadius: "var(--r-sm)", padding: 2 }}>
            {[
              { d: list?.today, label: t("journal.today") },
              { d: yesterday, label: t("journal.yesterday") },
            ].map(({ d, label }) =>
              d ? (
                <button
                  key={d}
                  className={d === day ? "btn primary" : "btn ghost"}
                  style={{ padding: "4px 10px", fontSize: 12.5 }}
                  onClick={() => setDay(d)}
                >
                  {label}
                </button>
              ) : null,
            )}
          </div>
          {/* Step through recorded days + jump to any of them */}
          <div className="row" style={{ gap: 4 }}>
            <button
              className="btn lc-j-day-step"
              disabled={!olderDay}
              aria-label={t("journal.olderDay")}
              title={t("journal.olderDay")}
              onClick={() => olderDay && setDay(olderDay)}
            >
              <ChevronLeft size={15} strokeWidth={1.75} />
            </button>
            <select
              className="input lc-j-day-select"
              aria-label={t("journal.pickDay")}
              value={day ?? ""}
              onChange={(e) => setDay(e.target.value)}
            >
              {days.map((d) => (
                <option key={d} value={d}>
                  {dayLabel(d)}
                </option>
              ))}
            </select>
            <button
              className="btn lc-j-day-step"
              disabled={!newerDay}
              aria-label={t("journal.newerDay")}
              title={t("journal.newerDay")}
              onClick={() => newerDay && setDay(newerDay)}
            >
              <ChevronRight size={15} strokeWidth={1.75} />
            </button>
          </div>
        </div>
      )}

      {loading && !entry ? (
        <div className="spinner" style={{ marginTop: 16 }} />
      ) : entry && isEmptyDay ? (
        <p
          className="muted"
          style={{ marginTop: 16, fontSize: 12.5, textAlign: "center", padding: "16px 0" }}
        >
          {t("journal.dayEmpty")}
        </p>
      ) : entry ? (
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 14 }}>
          {/* Metrics strip */}
          <div
            style={{
              display: "grid",
              gap: 12,
              gridTemplateColumns: "repeat(auto-fill,minmax(96px,1fr))",
              padding: "12px 0",
              borderTop: "1px solid var(--border)",
              borderBottom: "1px solid var(--border)",
            }}
          >
            {/* One money tile per book, never a combined "spend": a credits
                charge and a dollar estimate measure different transactions and
                adding them produces a number that means nothing. */}
            {showCredits && (
              <Metric
                label={t("journal.credits")}
                value={fmtCreditsAmount(journalCredits(entry.metrics))}
              />
            )}
            {showByok && <Metric label={t("journal.apiCost")} value={fmtUsd(entry.metrics.usd)} />}
            <Metric label={t("journal.calls")} value={fmtInt(entry.metrics.calls)} />
            <Metric label={t("journal.toolCalls")} value={fmtInt(entry.metrics.tool_calls)} />
            <Metric label={t("journal.a2a")} value={fmtInt(entry.metrics.a2a_messages)} />
            <Metric label={t("journal.agents")} value={fmtInt(entry.metrics.active_agents.length)} />
          </div>

          {/* Narrative — scoped server-side to the viewer's agents (a member
              sees only their granted agents' story unless team-transparency is
              on). Generation spends model budget over the whole fleet, so the
              button stays admin-only. */}
          <div>
            <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
              <span style={{ fontWeight: 600, fontSize: 12.5 }}>{t("journal.narrative")}</span>
              {isAdmin && (
                <button
                  className="btn"
                  disabled={generating}
                  style={{ display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 10px", fontSize: 12 }}
                  onClick={() => void generate()}
                >
                  {generating ? <RefreshCw size={13} className="lc-spin" strokeWidth={1.75} /> : <Sparkles size={13} strokeWidth={1.75} />}
                  {generating ? t("journal.generating") : t("journal.generate")}
                </button>
              )}
            </div>
            {entry.narrative ? (
              <div style={{ fontSize: 13.5, lineHeight: 1.55 }}>
                <Markdown text={entry.narrative} />
              </div>
            ) : (
              <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>{t("journal.noNarrative")}</p>
            )}
          </div>

          {/* By agent — ranked by the selected metric, bar = share of the day's top agent */}
          {entry.by_agent.length > 0 && (() => {
            const ranked = [...entry.by_agent].sort((x, y) => agentMetricValue(y, metric) - agentMetricValue(x, metric));
            const maxVal = agentMetricValue(ranked[0], metric);
            const totalVal = ranked.reduce((s, a) => s + agentMetricValue(a, metric), 0);
            return (
              <div>
                <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
                  <span style={{ fontWeight: 600, fontSize: 12.5 }}>{t("journal.byAgent")}</span>
                  <div className="row" style={{ gap: 2, background: "var(--surface-2)", borderRadius: "var(--r-sm)", padding: 2 }}>
                    {agentMetrics.map((m) => (
                      <button
                        key={m.id}
                        className={m.id === metric ? "btn primary" : "btn ghost"}
                        style={{ padding: "2px 8px", fontSize: 11.5 }}
                        onClick={() => setMetric(m.id)}
                      >
                        {t(m.labelKey)}
                      </button>
                    ))}
                  </div>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {ranked.map((a) => {
                    const v = agentMetricValue(a, metric);
                    const pct = maxVal > 0 ? (v / maxVal) * 100 : 0;
                    const shareRaw = totalVal > 0 ? (v / totalVal) * 100 : 0;
                    const share = shareRaw > 0 && shareRaw < 1 ? "<1%" : `${Math.round(shareRaw)}%`;
                    // The hover carries the row's *other* numbers, so the
                    // figures the selected metric hides are still one hover
                    // away — including the book that isn't being ranked on.
                    const tip = [
                      a.agent,
                      "—",
                      ...(showCredits ? [`${fmtCreditsAmount(journalCredits(a))} ·`] : []),
                      ...(showByok ? [`${fmtUsd(a.usd)} ·`] : []),
                      `${fmtInt(a.calls)} ${t("journal.callsSuffix")} ·`,
                      `${fmtInt(a.tool_calls)} ${t("journal.toolCallsSuffix")}`,
                    ].join(" ");
                    return (
                      <div key={a.agent} className="lc-j-agent-row" title={tip}>
                        <i className="lc-j-agent-fill" style={{ width: `${pct}%` }} aria-hidden />
                        <span className="lc-j-agent-name">{a.agent}</span>
                        <span className="muted lc-j-agent-tools">
                          {(a.top_tools || []).slice(0, 4).map((tt) => `${tt.tool}×${tt.count}`).join("  ·  ") || "—"}
                        </span>
                        <span className="lc-j-agent-value">{formatAgentMetric(v, metric)}</span>
                        <span className="muted lc-j-agent-share">{share}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}

          {/* Notable activity */}
          <div>
            <div style={{ fontWeight: 600, fontSize: 12.5, marginBottom: 6 }}>{t("journal.activity")}</div>
            {entry.events.cognition_actions.length === 0 && entry.events.cortex.length === 0 ? (
              <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>{t("journal.noActivity")}</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, lineHeight: 1.6 }}>
                {entry.events.cognition_actions.map((a, i) => (
                  <li key={`a${i}`}>
                    <strong>{a.action}</strong>
                    {a.summary ? ` — ${a.summary}` : ""}
                  </li>
                ))}
                {entry.events.cortex.map((c, i) => (
                  <li key={`c${i}`}>
                    {c.created ? t("journal.cortexCreated") : t("journal.cortexUpdated")} {c.kind} <em>{c.title}</em> → {c.status}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      ) : null}
        </>
      )}
    </div>
  );
}
