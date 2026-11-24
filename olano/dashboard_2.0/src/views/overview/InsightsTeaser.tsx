// Compact Insights summary strip for the Overview page: headline totals + a
// hand-rolled SVG sparkline of recent daily spend, linking to the full
// /insights view. Deliberately uses NO chart library so the Overview page (and
// the main bundle) stay lean — recharts only loads on /insights.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronRight, DollarSign, Gem } from "lucide-react";
import { api } from "../../core/api";
import { useT } from "../../core/i18n";
// Share the exact window, slicing and formatting the full Insights view uses so
// the teaser and the page can never quote different totals. (helpers.ts has no
// chart-lib import, so this doesn't pull recharts into the main bundle.)
import { DEFAULT_WINDOW, bookSlice, fmtInt, fmtUsd, headlineCredits } from "../insights/helpers";
import type { CreditsSummary, UsageRollup } from "../insights/types";
import { billingBooks, fmtCreditsAmount, useCostDisplay } from "../../core/costDisplay";
import { STR } from "./strings";

// The teaser reads the SAME window Insights opens on. Anything else and the
// two headline numbers disagree on the same screen — which is exactly the bug
// this constant exists to make impossible.
const WINDOW = DEFAULT_WINDOW;

interface MiniOverview {
  usage: UsageRollup;
  tools: { totals: { tool_calls: number } };
  services?: { totals: { calls: number } };
  /** Combined local credits for the window (model + service ledgers). */
  credits_summary?: CreditsSummary;
}

// The slice of GET /api/olano/credits the strip needs: the cloud-truth balance.
// Same endpoint + cache window as CreditsCard, so the two can never disagree.
interface CreditsBalance {
  available: boolean;
  balance?: number;
}

function Sparkline({ data }: { data: number[] }) {
  const W = 160;
  const H = 36;
  const P = 2;
  if (data.length < 2 || data.every((d) => d === 0)) {
    return (
      <svg width={W} height={H} aria-hidden>
        <line x1={P} y1={H - P} x2={W - P} y2={H - P} stroke="var(--border)" strokeDasharray="3 3" />
      </svg>
    );
  }
  // Anchor the baseline at 0 (not data-min) so a lone spike reads as a spike,
  // not a full-height ramp.
  const max = Math.max(...data) || 1;
  const stepX = (W - 2 * P) / (data.length - 1);
  const pts = data.map((v, i) => {
    const x = P + i * stepX;
    const y = P + (H - 2 * P) * (1 - v / max);
    return [x, y] as const;
  });
  const line = pts.map(([x, y], i) => `${i === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const area = `${line} L ${pts[pts.length - 1][0].toFixed(1)} ${H - P} L ${pts[0][0].toFixed(1)} ${H - P} Z`;
  const [lx, ly] = pts[pts.length - 1];
  return (
    <svg width={W} height={H} aria-hidden style={{ display: "block" }}>
      <defs>
        <linearGradient id="lc-teaser-spark" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.28" />
          <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#lc-teaser-spark)" />
      <path d={line} fill="none" stroke="var(--accent)" strokeWidth={1.75} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={lx} cy={ly} r={2.4} fill="var(--accent)" />
    </svg>
  );
}

function Metric({ value, label }: { value: string; label: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 18, fontWeight: 700, lineHeight: 1.1, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      <div className="muted" style={{ fontSize: 11 }}>{label}</div>
    </div>
  );
}

export function InsightsTeaser() {
  const t = useT(STR);
  // Subscribed so every cost below re-renders when an admin switches the
  // deployment between dollars and olano credits — and so the strip shows the
  // billing books this deployment actually keeps.
  const cost = useCostDisplay();
  const [data, setData] = useState<MiniOverview | null>(null);
  const [failed, setFailed] = useState(false);
  const [balance, setBalance] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .get<MiniOverview>(`/api/analytics/overview?days=${WINDOW}`)
      .then((d) => alive && setData(d))
      .catch(() => alive && setFailed(true));
    // The balance is the number a fresh box's owner is actually looking for —
    // "Credits used (30d): 0" alone reads as "no credits left" and has sent
    // owners off the olano tiers. Missing/unmanaged just hides the metric.
    api
      .cachedGet<CreditsBalance>("/api/olano/credits", 60_000)
      .then((r) => {
        if (alive && r.available && typeof r.balance === "number") setBalance(r.balance);
      })
      .catch(() => {
        /* unmanaged or unreachable — the metric simply doesn't render */
      });
    return () => {
      alive = false;
    };
  }, []);

  // Stay invisible until we have data — never show an empty/broken strip. A
  // known balance keeps the strip visible even with zero usage: a just-deployed
  // managed box should lead with "N credits left", not with nothing.
  if (failed || !data) return null;
  const { usage, tools, services } = data;
  if (
    usage.totals.usd === 0 &&
    usage.totals.credits === 0 &&
    usage.totals.total_tokens === 0 &&
    !(data.credits_summary?.total ?? 0) &&
    balance === null
  ) {
    return null;
  }

  const books = billingBooks(cost);
  const mixed = books.length > 1;
  const showCredits = books.includes("credits");
  const showByok = books.includes("byok");
  const byok = bookSlice(usage, "customer", mixed);
  // The combined local figure (managed models + services) — the box records
  // every olano-billed call itself, background work included.
  const headline = headlineCredits(data.credits_summary, usage, mixed);
  // The sparkline follows whichever book leads the strip, so its shape matches
  // the number beside it rather than tracking the other book's spend.
  const spark = usage.daily.map((d) => (showCredits ? d.credits : d.usd) ?? 0);

  return (
    <Link
      to="/insights"
      className="card"
      style={{
        marginTop: 14,
        padding: "14px 16px",
        display: "flex",
        alignItems: "center",
        gap: 18,
        flexWrap: "wrap",
        textDecoration: "none",
        color: "inherit",
      }}
    >
      <span
        aria-hidden
        style={{
          display: "inline-flex",
          width: 34,
          height: 34,
          borderRadius: "var(--r-sm)",
          background: "var(--accent-soft)",
          color: "var(--accent)",
          alignItems: "center",
          justifyContent: "center",
          flex: "0 0 auto",
        }}
      >
        {showCredits ? <Gem size={17} strokeWidth={1.9} /> : <DollarSign size={17} strokeWidth={1.9} />}
      </span>
      <div style={{ display: "flex", gap: 22, flexWrap: "wrap", flex: 1, minWidth: 0 }}>
        {/* One headline per book this plan keeps — a credits-only deployment
            never shows a dollar figure (there is no provider bill), and a BYOK
            deployment never shows credits (there is no credit account). The
            credits book leads with what's LEFT (the cloud account balance) and
            only then what the window consumed — the reverse order made a fresh
            box's "0 used" read as an empty account. */}
        {showCredits && balance !== null && (
          <Metric value={fmtCreditsAmount(balance)} label={t("insights.creditsLeft")} />
        )}
        {showCredits && (
          <Metric
            value={fmtCreditsAmount(headline)}
            label={t("insights.credits", { n: String(WINDOW) })}
          />
        )}
        {/* Dollars, not the display unit: this is the same BYOK figure the
            Insights page headlines one click away, and that book is a provider
            invoice that never converts. */}
        {showByok && (
          <Metric value={fmtUsd(byok.usd)} label={t("insights.spend", { n: String(WINDOW) })} />
        )}
        <Metric value={fmtInt(usage.totals.calls)} label={t("insights.modelCalls")} />
        <Metric value={fmtInt(tools.totals.tool_calls)} label={t("insights.tools")} />
        {showCredits && (services?.totals.calls ?? 0) > 0 && (
          <Metric value={fmtInt(services?.totals.calls ?? 0)} label={t("insights.services")} />
        )}
      </div>
      <Sparkline data={spark} />
      <span className="muted row" style={{ fontSize: 13, gap: 2, flex: "0 0 auto" }}>
        {t("insights.viewAll")} <ChevronRight size={14} strokeWidth={1.75} />
      </span>
    </Link>
  );
}
