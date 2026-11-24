import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  DollarSign,
  Gem,
  Wrench,
  Network,
  RotateCw,
  AlertTriangle,
  Cpu,
  Sparkles,
} from "lucide-react";
import { useLocation } from "react-router-dom";
import { usePathSection } from "../../core/usePathSection";
import { parseRoute, viewPath } from "../../core/routeUrls";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
import { billingBooks, useCostDisplay } from "../../core/costDisplay";
import type { BillingBook } from "../../core/costDisplay";
import { BrandWordmark } from "../../ui/BrandMark";
import { usePersistentState } from "../../core/usePersistentState";
import { importChunk } from "../../core/chunkRecovery";
import { StatCard } from "../overview/StatCard";
import {
  A2AByAgentBar,
  CallsByAgentBar,
  CallsOverTime,
  ChartCard,
  ServiceCallsBar,
  ServiceCallsByAgentBar,
  SpendByAgentBar,
  SpendByModelPie,
  SpendOverTime,
  ToolCallsByAgentBar,
  TopSubagentsBar,
  TopToolsBar,
} from "./charts";
import { FeedbackCard } from "./FeedbackCard";
import { JournalFeed } from "./JournalFeed";
// The drill-down is the non-default tab — splitting it out keeps it off the
// critical path of the view that /insights actually lands on. (The charts stay
// eager: they *are* the default tab.)
const DrilldownView = lazy(() =>
  importChunk(() => import("./drilldown/DrilldownView")).then((m) => ({
    default: m.DrilldownView,
  })),
);
import {
  DEFAULT_WINDOW,
  WINDOWS,
  bookSlice,
  fmtDay,
  fmtHourLabel,
  fmtInt,
  fmtUsd,
  headlineCredits,
  type Window,
} from "./helpers";
import { fmtCreditsAmount } from "../../core/costDisplay";
import { CreditsCard } from "../overview/CreditsCard";
import { STR } from "./strings";
import { ensureInsightsStyles } from "./styles";
import type { AnalyticsOverview, DailyPoint, UsageByOrigin } from "./types";

type Tab = "overview" | "drill";
const TABS: readonly Tab[] = ["overview", "drill"];

// Which billing books the page is showing. "all" means every book this
// deployment keeps; the other two pin it to one. The switch only appears when
// the deployment actually has both — on a single-book plan there is nothing to
// choose between, and offering the choice would imply data that doesn't exist.
type BookFilter = "all" | BillingBook;

// Services with a translated label. A new managed service reaches the ledger
// before it reaches this list, so anything unknown renders its raw name rather
// than the untranslated "service.<x>" key useT would fall back to.
const NAMED_SERVICES = new Set(["image", "vision", "tts", "stt", "web_search", "web_fetch", "linkedin"]);

// Managed chat tiers with a translated label — same fallback rule.
const NAMED_TIERS = new Set(["turbo", "pro", "max", "auto"]);

// Work origins with a translated label — same fallback rule as NAMED_SERVICES.
const NAMED_ORIGINS = new Set([
  "interactive",
  "scheduled",
  "cognition",
  "delegate",
  "pulse",
  "highlights",
  "reports",
  "prompt_gen",
  "acp",
  "peer",
  "turns",
]);

// "Where credits/spend went": one row per work origin (chat, Cortex, scheduled
// tasks, highlights, …) with a proportional bar. Hand-rolled — a full recharts
// bar earns nothing over this for a ≤10-row list.
function OriginBreakdown({
  rows,
  metric,
  fmtValue,
  labelFor,
  callsWord,
}: {
  rows: UsageByOrigin[];
  metric: "credits" | "usd";
  fmtValue: (n: number) => string;
  labelFor: (o: string) => string;
  callsWord: string;
}) {
  const val = (r: UsageByOrigin) => (metric === "credits" ? r.credits : r.usd);
  const shown = rows.filter((r) => val(r) > 0).sort((a, b) => val(b) - val(a));
  const max = shown.length ? val(shown[0]) : 1;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: "4px 2px" }}>
      {shown.map((r) => (
        <div key={r.origin} style={{ minWidth: 0 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 13 }}>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {labelFor(r.origin)}
            </span>
            <span style={{ fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>
              {fmtValue(val(r))}
              <span className="muted"> · {fmtInt(r.calls)} {callsWord}</span>
            </span>
          </div>
          <div style={{ height: 4, borderRadius: 2, background: "var(--accent-soft)", marginTop: 4 }}>
            <div
              style={{
                width: `${Math.max(2, (val(r) / max) * 100)}%`,
                height: "100%",
                borderRadius: 2,
                background: "var(--accent)",
              }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

// Cumulative/per-bucket switch shared by the two time-series cards. The
// per-bucket label follows the window: "Daily", or "Hourly" in the 1-day view.
function ModeToggle({
  cumulative,
  hourly,
  onChange,
}: {
  cumulative: boolean;
  hourly: boolean;
  onChange: (c: boolean) => void;
}) {
  const t = useT(STR);
  return (
    <div className="lc-in-seg lc-in-seg-sm">
      {[false, true].map((c) => (
        <button
          key={String(c)}
          className={c === cumulative ? "btn primary" : "btn ghost"}
          aria-pressed={c === cumulative}
          onClick={() => onChange(c)}
        >
          {c ? t("chart.cumulative") : t(hourly ? "chart.hourly" : "chart.daily")}
        </button>
      ))}
    </div>
  );
}

// Titled band around one accounting book, so a credits figure and a dollar
// figure can never be mistaken for parts of the same total.
function BookSection({
  icon,
  title,
  desc,
  children,
}: {
  icon: ReactNode;
  title: string;
  desc: string;
  children: ReactNode;
}) {
  return (
    <section className="lc-in-book">
      <div className="lc-in-book-head">
        <span className="lc-in-book-title">
          {icon}
          {title}
        </span>
        <span className="muted lc-in-book-desc">{desc}</span>
      </div>
      {children}
    </section>
  );
}

export function InsightsView() {
  const t = useT(STR);
  // Subscribing here is what makes every chart, tick and tooltip below re-render
  // when an admin flips the deployment between dollars and olano credits — the
  // formatters read a module snapshot, so nothing else would invalidate them.
  // It also carries which billing books this deployment keeps at all.
  const cost = useCostDisplay();
  // /insights/<tab> (the overview is the bare page); the drill-down adds its
  // own position after it (/insights/drill/<agent>?thread=…, DrilldownView).
  const location = useLocation();
  const drillAgent = parseRoute(location.pathname).agent;
  const [tab, setTab] = usePathSection<Tab>({
    allowed: TABS,
    fallback: "overview",
    rememberKey: "insights.tab",
    pathFor: (section) => viewPath("insights", { section, agent: section === "drill" ? drillAgent : null }),
  });
  const [days, setDays] = usePersistentState<Window>("insights.window", DEFAULT_WINDOW);
  const [cumulative, setCumulative] = usePersistentState("insights.cumulative", false);
  const [book, setBook] = usePersistentState<BookFilter>("insights.book", "all");
  const [data, setData] = useState<AnalyticsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => ensureInsightsStyles(), []);

  const load = useCallback((d: Window) => {
    setLoading(true);
    setError(null);
    // The 1-day window is a rolling last-24h view with hourly resolution.
    const query = d === 1 ? "hours=24" : `days=${d}`;
    api
      .get<AnalyticsOverview>(`/api/analytics/overview?${query}`)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "error"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => load(days), [days, load]);

  // Refresh drives whichever tab is showing. Bumping `drillNonce` re-runs the
  // drill-down's fetches; it reports its own in-flight state back so the button
  // spins for the work it actually triggered. Both tabs also re-load the
  // overview payload — the "updated HH:MM" caption is rendered on both.
  const [drillNonce, setDrillNonce] = useState(0);
  const [drillLoading, setDrillLoading] = useState(false);
  const busy = loading || (tab === "drill" && drillLoading);
  const refresh = useCallback(() => {
    load(days);
    if (tab === "drill") setDrillNonce((n) => n + 1);
  }, [load, days, tab]);

  const usage = data?.usage;
  const tools = data?.tools;
  const services = data?.services;
  const a2a = data?.a2a;
  const hasUsage = !!usage && usage.totals.calls > 0;
  const emptyLabel = t("empty.chart");
  const callsLabel = t("legend.calls");
  // Payload-driven so the charts never mismatch the data during a window switch.
  const hourly = !!data?.window_hours && !!usage?.hourly;
  const series: DailyPoint[] = hourly
    ? (usage?.hourly ?? []).map((h) => ({ ...h, date: h.hour }))
    : (usage?.daily ?? []);

  // Which books exist on this plan, and which the switch is currently showing.
  // A stored filter for a book the deployment no longer has degrades to "all"
  // rather than rendering an empty page.
  const available = billingBooks(cost);
  const mixed = available.length > 1;
  const filter: BookFilter = mixed && available.includes(book as BillingBook) ? book : "all";
  const visible = filter === "all" ? available : [filter];
  const showCredits = visible.includes("credits");
  const showByok = visible.includes("byok");

  const credits = bookSlice(usage, "olano", mixed);
  const byok = bookSlice(usage, "customer", mixed);
  const serviceCalls = services?.totals.calls ?? 0;
  // The combined LOCAL figure (managed models + priced services) — the box
  // records every olano-billed call itself, so no cloud fetch backs this.
  // Same helper the Overview teaser uses, so the two never disagree.
  const headline = headlineCredits(data?.credits_summary, usage, mixed);
  const serviceCredits = data?.credits_summary?.services ?? 0;
  const originRows = usage?.by_origin ?? [];
  const tierRows = usage?.by_tier ?? [];
  const originLabel = (o: string) => (NAMED_ORIGINS.has(o) ? t(`origin.${o}`) : o);
  const tierLabel = (tr: string) => (NAMED_TIERS.has(tr) ? t(`tier.${tr}`) : tr);

  return (
    <>
      {/* On a narrow bar the cluster restacks into two flush rows instead of a
          ragged right-aligned wrap — see the container query in styles.ts. */}
      <div className="topbar lc-in-topbar">
        <BrandWordmark />
        <div className="lc-in-controls">
          <div className="lc-in-seg lc-in-tabs" role="group" aria-label={t("aria.view")}>
            {(["overview", "drill"] as Tab[]).map((tb) => (
              <button
                key={tb}
                className={tb === tab ? "btn primary" : "btn ghost"}
                aria-pressed={tb === tab}
                onClick={() => setTab(tb)}
              >
                {t(`tab.${tb}`)}
              </button>
            ))}
          </div>
          {/* One window selector for both tabs, and one meaning per button: the
              drill-down sends the same `hours=24` / `days=N` this tab does, so
              "1 day" is the rolling last 24 hours in both places. It scopes
              threads by their last-active moment there rather than per call —
              see the API's window params. */}
          <div className="lc-in-seg lc-in-windows" role="group" aria-label={t("aria.range")}>
            {WINDOWS.map((w) => (
              <button
                key={w}
                className={w === days ? "btn primary" : "btn ghost"}
                aria-pressed={w === days}
                onClick={() => setDays(w)}
              >
                {t(`win.${w}`)}
              </button>
            ))}
          </div>
          <button
            className="btn lc-in-refresh"
            onClick={refresh}
            disabled={busy}
            aria-label={t("btn.refresh")}
            title={t("btn.refresh")}
          >
            <RotateCw size={15} strokeWidth={1.75} className={busy ? "lc-spin" : undefined} />
            <span className="lc-in-refresh-label">{t("btn.refresh")}</span>
          </button>
        </div>
      </div>

      <div className="page">
        <div>
          <h1>{t("title")}</h1>
          <p className="muted">{t("subtitle")}</p>
          {data && (
            <p className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              {t("caption.range", {
                // Hourly windows start mid-day, so the caption keeps the hour.
                from: data.window_hours ? fmtHourLabel(data.since.slice(0, 13)) : fmtDay(data.since),
                to: fmtDay(data.generated_at.slice(0, 10)),
                time: data.generated_at.slice(11, 16),
              })}
            </p>
          )}
        </div>

        {tab === "drill" ? (
          <div style={{ marginTop: 18 }}>
            <Suspense fallback={<div className="spinner" style={{ marginTop: 24 }} />}>
              <DrilldownView days={days} refreshKey={drillNonce} onLoadingChange={setDrillLoading} />
            </Suspense>
          </div>
        ) : loading && !data ? (
          <div className="spinner" style={{ marginTop: 24 }} />
        ) : error ? (
          <div
            className="card"
            style={{ marginTop: 18, padding: 28, textAlign: "center", borderColor: "color-mix(in srgb, var(--danger) 35%, var(--border))" }}
          >
            <div style={{ display: "flex", justifyContent: "center", color: "var(--danger)" }} aria-hidden>
              <AlertTriangle size={28} strokeWidth={1.5} />
            </div>
            <strong style={{ display: "block", marginTop: 8, color: "var(--danger)" }}>{t("err.load")}</strong>
            <p className="muted" style={{ marginTop: 6 }}>{error}</p>
            <button className="btn primary" style={{ marginTop: 10 }} onClick={() => load(days)}>
              {t("btn.refresh")}
            </button>
          </div>
        ) : (
          <>
            {/* ── billing-book switch (mixed plans only) ──────────────────── */}
            {mixed && (
              <div
                className="lc-in-seg"
                role="group"
                aria-label={t("aria.book")}
                style={{ marginTop: 16, width: "fit-content", maxWidth: "100%", flexWrap: "wrap" }}
              >
                {(["all", "credits", "byok"] as BookFilter[]).map((b) => (
                  <button
                    key={b}
                    className={b === filter ? "btn primary" : "btn ghost"}
                    aria-pressed={b === filter}
                    onClick={() => setBook(b)}
                  >
                    {t(`book.${b}`)}
                  </button>
                ))}
              </div>
            )}

            {/* ── fleet activity: the counts both books share ─────────────── */}
            <div className="lc-in-stats" style={{ marginTop: 14 }}>
              <StatCard
                label={t("stat.modelCalls")}
                icon={<Cpu size={16} strokeWidth={1.75} />}
                value={fmtInt(usage?.totals.calls ?? 0)}
                sub={t((usage?.totals.models ?? 0) === 1 ? "stat.modelCalls.sub.one" : "stat.modelCalls.sub", {
                  models: fmtInt(usage?.totals.models ?? 0),
                })}
              />
              <StatCard
                label={t("stat.tools")}
                icon={<Wrench size={16} strokeWidth={1.75} />}
                value={fmtInt(tools?.totals.tool_calls ?? 0)}
                sub={t((tools?.totals.distinct_tools ?? 0) === 1 ? "stat.tools.sub.one" : "stat.tools.sub", {
                  tools: fmtInt(tools?.totals.distinct_tools ?? 0),
                })}
              />
              <StatCard
                label={t("stat.a2a")}
                icon={<Network size={16} strokeWidth={1.75} />}
                value={fmtInt(a2a?.totals.messages ?? 0)}
                sub={t((a2a?.totals.agents ?? 0) === 1 ? "stat.a2a.sub.one" : "stat.a2a.sub", {
                  agents: fmtInt(a2a?.totals.agents ?? 0),
                })}
              />
            </div>

            {!hasUsage && (
              <div className="card" style={{ marginTop: 14, padding: 28, textAlign: "center" }}>
                <strong style={{ display: "block", fontSize: 15 }}>{t("empty.title")}</strong>
                <p className="muted" style={{ marginTop: 6, maxWidth: 480, marginInline: "auto" }}>{t("empty.desc")}</p>
              </div>
            )}

            {/* ── book 1: Olano credits ──────────────────────────────────── */}
            {showCredits && (
              <BookSection
                icon={<Gem size={16} strokeWidth={1.9} />}
                title={t("book.credits.title")}
                desc={t("book.credits.desc")}
              >
                {/* Three cards, one number each: the charge, the work that
                    produced it, and the non-model services beside it. The call
                    count used to be the credit card's sub-line, where it read
                    as a footnote to the money instead of the separate figure
                    people actually scan for. */}
                <div className="lc-in-stats">
                  <StatCard
                    label={t("stat.credits")}
                    icon={<Gem size={16} strokeWidth={1.75} />}
                    value={fmtCreditsAmount(headline)}
                    sub={
                      serviceCredits > 0
                        ? t("stat.credits.sub.split", {
                            model: fmtCreditsAmount(data?.credits_summary?.model ?? 0),
                            services: fmtCreditsAmount(serviceCredits),
                          })
                        : t("stat.credits.sub")
                    }
                  />
                  <StatCard
                    label={t("stat.creditCalls")}
                    icon={<Cpu size={16} strokeWidth={1.75} />}
                    value={fmtInt(credits.calls)}
                    sub={t("stat.creditCalls.sub")}
                  />
                  <StatCard
                    label={t("stat.services")}
                    icon={<Sparkles size={16} strokeWidth={1.75} />}
                    value={fmtInt(serviceCalls)}
                    sub={
                      (services?.totals.credits ?? 0) > 0
                        ? t("stat.services.sub.credits", {
                            credits: fmtCreditsAmount(services?.totals.credits ?? 0),
                          })
                        : t("stat.services.sub")
                    }
                  />
                </div>

                {/* Balance card (managed deployments only; self-hides otherwise). */}
                <CreditsCard />

                <div className="lc-in-charts lc-in-charts-wide">
                  <ChartCard
                    title={t("chart.creditsOverTime")}
                    control={<ModeToggle cumulative={cumulative} hourly={hourly} onChange={setCumulative} />}
                    empty={credits.credits <= 0}
                    emptyLabel={emptyLabel}
                  >
                    <SpendOverTime
                      daily={series}
                      cumulative={cumulative}
                      hourly={hourly}
                      metric="credits"
                      label={cost.unitNamePlural}
                    />
                  </ChartCard>
                  <ChartCard
                    title={t("chart.services")}
                    height={300}
                    empty={!services || services.by_service.length === 0}
                    emptyLabel={t("empty.services")}
                  >
                    <ServiceCallsBar
                      data={services?.by_service ?? []}
                      labelFor={(s) => (NAMED_SERVICES.has(s) ? t(`service.${s}`) : s)}
                    />
                  </ChartCard>
                </div>

                <div className="lc-in-charts">
                  <ChartCard
                    title={t("chart.creditsByAgent")}
                    height={300}
                    empty={credits.credits <= 0}
                    emptyLabel={emptyLabel}
                  >
                    <SpendByAgentBar
                      data={usage?.by_agent ?? []}
                      metric="credits"
                      label={cost.unitNamePlural}
                    />
                  </ChartCard>
                  {/* "Where credits went": chat vs Cortex vs scheduled work vs
                      highlights etc. — the answer to "I barely chatted, what
                      used my credits?". Ledger-sourced, so it covers the
                      background services the checkpoint views can't split. */}
                  <ChartCard
                    title={t("chart.creditsByOrigin")}
                    height={300}
                    empty={!originRows.some((r) => r.credits > 0)}
                    emptyLabel={emptyLabel}
                  >
                    <OriginBreakdown
                      rows={originRows}
                      metric="credits"
                      fmtValue={fmtCreditsAmount}
                      labelFor={originLabel}
                      callsWord={t("legend.callsCount")}
                    />
                  </ChartCard>
                  {/* "Credits by tier": which brain (Turbo/Pro/Max) burned the
                      window's credits. Operators DO ask this — agents and
                      Cortex are configured per tier, so the split says what a
                      tier change would save. Ledger-sourced (the mined store
                      only knows the gateway's wire names, never the tiers). */}
                  <ChartCard
                    title={t("chart.creditsByTier")}
                    height={300}
                    empty={!tierRows.some((r) => r.credits > 0)}
                    emptyLabel={emptyLabel}
                  >
                    <OriginBreakdown
                      rows={tierRows.map((r) => ({ ...r, origin: r.tier }))}
                      metric="credits"
                      fmtValue={fmtCreditsAmount}
                      labelFor={tierLabel}
                      callsWord={t("legend.callsCount")}
                    />
                  </ChartCard>
                  <ChartCard
                    title={t("chart.servicesByAgent")}
                    height={300}
                    empty={!services || services.by_agent.length === 0}
                    emptyLabel={t("empty.services")}
                  >
                    <ServiceCallsByAgentBar data={services?.by_agent ?? []} />
                  </ChartCard>
                </div>
              </BookSection>
            )}

            {/* ── book 2: BYOK (the operator's own provider bill) ─────────── */}
            {showByok && (
              <BookSection
                icon={<DollarSign size={16} strokeWidth={1.9} />}
                title={t("book.byok.title")}
                desc={t("book.byok.desc")}
              >
                <div className="lc-in-stats">
                  {/* Dollars, always — this book is a provider invoice, and it
                      stays in the currency the provider bills in even when the
                      deployment displays everything else in credits. */}
                  <StatCard
                    label={t("stat.apiCost")}
                    icon={<DollarSign size={16} strokeWidth={1.75} />}
                    value={fmtUsd(byok.usd)}
                    sub={t("stat.apiCost.sub")}
                  />
                  <StatCard
                    label={t("stat.byokCalls")}
                    icon={<Cpu size={16} strokeWidth={1.75} />}
                    value={fmtInt(byok.calls)}
                    sub={t("stat.byokCalls.sub")}
                  />
                </div>

                <div className="lc-in-charts lc-in-charts-wide">
                  <ChartCard
                    title={t("chart.spendOverTime")}
                    control={<ModeToggle cumulative={cumulative} hourly={hourly} onChange={setCumulative} />}
                    empty={byok.usd <= 0}
                    emptyLabel={emptyLabel}
                  >
                    <SpendOverTime daily={series} cumulative={cumulative} hourly={hourly} />
                  </ChartCard>
                  <ChartCard title={t("chart.spendByModel")} empty={byok.usd <= 0} emptyLabel={emptyLabel}>
                    <SpendByModelPie data={usage?.by_model ?? []} />
                  </ChartCard>
                  <ChartCard title={t("chart.spendByAgent")} empty={byok.usd <= 0} emptyLabel={emptyLabel}>
                    <SpendByAgentBar data={usage?.by_agent ?? []} />
                  </ChartCard>
                  {/* Same origin split as the credits book, in this book's
                      currency — background work burns BYOK dollars too. */}
                  <ChartCard
                    title={t("chart.spendByOrigin")}
                    empty={!originRows.some((r) => r.usd > 0)}
                    emptyLabel={emptyLabel}
                  >
                    <OriginBreakdown
                      rows={originRows}
                      metric="usd"
                      fmtValue={fmtUsd}
                      labelFor={originLabel}
                      callsWord={t("legend.callsCount")}
                    />
                  </ChartCard>
                </div>
              </BookSection>
            )}

            {/* ── tool + a2a activity (shared by both books) ──────────────── */}
            <BookSection
              icon={<Wrench size={16} strokeWidth={1.9} />}
              title={t("book.activity.title")}
              desc={t("book.activity.desc")}
            >
              {/* Work volume, in calls rather than tokens: a call is the same
                  unit on either billing book, so it needs no per-book split
                  and no "(all models)" caveat. */}
              <div className="lc-in-charts lc-in-charts-wide">
                <ChartCard
                  title={t("chart.callsOverTime")}
                  control={<ModeToggle cumulative={cumulative} hourly={hourly} onChange={setCumulative} />}
                  empty={!hasUsage}
                  emptyLabel={emptyLabel}
                >
                  <CallsOverTime
                    daily={series}
                    cumulative={cumulative}
                    hourly={hourly}
                    label={callsLabel}
                  />
                </ChartCard>
                <ChartCard title={t("chart.callsByAgent")} empty={!hasUsage} emptyLabel={emptyLabel}>
                  <CallsByAgentBar data={usage?.by_agent ?? []} label={callsLabel} />
                </ChartCard>
              </div>

              <div className="lc-in-charts">
                <ChartCard
                  title={t("chart.toolCallsByAgent")}
                  height={300}
                  empty={!tools || tools.totals.tool_calls === 0}
                  emptyLabel={emptyLabel}
                >
                  <ToolCallsByAgentBar data={tools?.by_agent ?? []} />
                </ChartCard>
                <ChartCard
                  title={t("chart.topTools")}
                  height={300}
                  empty={!tools || tools.by_tool.length === 0}
                  emptyLabel={emptyLabel}
                >
                  <TopToolsBar data={tools?.by_tool ?? []} />
                </ChartCard>
                <ChartCard
                  title={t("chart.topSubagents")}
                  height={300}
                  empty={!tools || tools.by_subagent.length === 0}
                  emptyLabel={t("empty.subagents")}
                >
                  <TopSubagentsBar data={tools?.by_subagent ?? []} />
                </ChartCard>
                <ChartCard
                  title={t("chart.a2aByAgent")}
                  height={300}
                  empty={!a2a || a2a.totals.messages === 0}
                  emptyLabel={emptyLabel}
                >
                  <A2AByAgentBar
                    data={a2a?.by_agent ?? []}
                    labels={{ sent: t("legend.sent"), received: t("legend.received") }}
                  />
                </ChartCard>
                {/* Thumbs on replies (chat feedback), refetched with the
                    overview so Refresh and the window buttons move it too. */}
                <FeedbackCard days={days} refreshKey={data?.generated_at} />
              </div>
            </BookSection>

            {/* ── work journal ───────────────────────────────────────────── */}
            <div style={{ marginTop: 20 }}>
              <JournalFeed />
            </div>
          </>
        )}
      </div>
    </>
  );
}
