// Formatters + a stable categorical color palette for the Insights charts.

import type { CreditsSummary, UsageBySource, UsageRollup } from "./types";

// Money formatting lives in core/costDisplay (chat needs it too); re-exported
// here so the Insights call sites keep importing all their formatters from one
// place. One per billing book, each unit-fixed — see that file's note on why
// there is no display-unit conversion left.
export { fmtCreditsAmount, fmtUsd, usdAxisFormatter } from "../../core/costDisplay";

// Axis ticks for an AUTHORITATIVE credits series (the Olano gateway's own
// charge). Takes credits, not dollars — usdAxisFormatter is the other book's
// and the two never share an axis. Compacts the way the tokens axis does and
// drops the unit name (it would not fit; the tooltip carries it).
export function creditsAxisFormatter(max: number): (v: number) => string {
  if (!(max > 0)) return () => "0";
  return (v: number) => {
    if (v === 0) return "0";
    if (max >= 1000) return fmtTokens(Math.round(v));
    if (max >= 1) return v.toFixed(0);
    return v.toFixed(max >= 0.1 ? 2 : 3);
  };
}

// Concrete hexes (not CSS vars) because recharts paints legend/tooltip swatches
// from the literal fill value. These are tuned to sit alongside the Aurora
// design tokens (orange accent, mint success, amber warn, sky info) and stay
// distinguishable in both themes. Assignment is by stable key hash so an
// agent/model keeps its colour across re-renders and views.
export const CHART_COLORS = [
  "#ff6a3d", // orange (accent)
  "#3ddc97", // mint (success)
  "#60a5fa", // sky (info)
  "#e2b341", // amber (warn)
  "#a78bfa", // violet
  "#f472b6", // pink
  "#2dd4bf", // teal
  "#fb923c", // orange
  "#818cf8", // indigo
  "#34d399", // emerald
  "#facc15", // gold
  "#f87171", // red
];

export function colorFor(key: string, index?: number): string {
  if (typeof index === "number") return CHART_COLORS[index % CHART_COLORS.length];
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return CHART_COLORS[h % CHART_COLORS.length];
}

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}K`;
  return String(n);
}

export function fmtInt(n: number): string {
  return n.toLocaleString();
}

// "2026-06-07" → "Jun 7" (no timezone parsing — the date is already a UTC day).
export function fmtDay(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${MONTHS[m - 1]} ${d}`;
}

// Hour-bucket key "2026-06-07T17" (UTC, from the hourly usage series) → "17:00"
// for axis ticks. Accepts any longer ISO timestamp too (slices the hour out).
export function fmtHour(isoHour: string): string {
  const h = isoHour.slice(11, 13);
  return /^\d{2}$/.test(h) ? `${h}:00` : isoHour;
}

// Hour-bucket key → "Jun 7, 17:00" for tooltips/captions, where the day matters.
export function fmtHourLabel(isoHour: string): string {
  const day = fmtDay(isoHour.slice(0, 10));
  const h = isoHour.slice(11, 13);
  return /^\d{2}$/.test(h) ? `${day}, ${h}:00` : day;
}

// Locale-aware label for a journal day: "Wed, Jul 16" (year appended only when
// it differs from the current one). Formats in UTC so the ISO day never shifts.
export function fmtDayLabel(iso: string, locale: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  const sameYear = y === new Date().getUTCFullYear();
  return new Intl.DateTimeFormat(locale, {
    weekday: "short",
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
    timeZone: "UTC",
  }).format(new Date(Date.UTC(y, m - 1, d)));
}

// Shift an ISO day by whole days ("2026-07-17", -1 → "2026-07-16"), in UTC.
export function shiftDay(iso: string, delta: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Date(Date.UTC(y, m - 1, d + delta)).toISOString().slice(0, 10);
}

// Shorten "anthropic:claude-sonnet-4-6" → "claude-sonnet-4-6" for axis labels.
export function shortModel(model: string): string {
  const i = model.indexOf(":");
  return i >= 0 ? model.slice(i + 1) : model;
}

// Friendly display name for legends/labels: "anthropic:claude-sonnet-4-6" →
// "Claude Sonnet 4.6", "openai:gpt-5.4" → "GPT-5.4". Falls back to the short
// slug. Keep the raw model string for colour hashing so colours stay stable.
export function prettyModel(model: string): string {
  let s = shortModel(model);
  // claude-sonnet-4-6 → claude-sonnet-4.6 (last "-N" between digits → ".N")
  s = s.replace(/(\d)-(\d)/g, "$1.$2");
  const acronyms: Record<string, string> = { gpt: "GPT", o3: "o3", o4: "o4" };
  return s
    .split("-")
    .map((p) => acronyms[p.toLowerCase()] ?? (/^\d/.test(p) ? p : p.charAt(0).toUpperCase() + p.slice(1)))
    .join(" ");
}

// Truncate a long category label (e.g. agent name) for an axis tick.
export function truncLabel(s: string, max = 12): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

// 1 = the rolling last-24-hours view (hourly resolution, `?hours=24`); the rest
// are trailing calendar-day windows (`?days=N`).
export const WINDOWS = [1, 7, 30, 90] as const;
export type Window = (typeof WINDOWS)[number];

/**
 * The window every fleet-wide usage headline is read over. The Insights page
 * opens on it and the Overview teaser fetches it, so the two can never quote
 * different totals for "spend" — that mismatch is the whole reason this is a
 * shared constant rather than a default in each view.
 */
export const DEFAULT_WINDOW: Window = 30;

/**
 * One billing book's slice of a usage rollup.
 *
 * On a **single-book** deployment the rollup's own totals ARE that book, and
 * they're the better number: totals also carry the checkpoint-mined days that
 * predate the usage ledger, which `by_source` cannot classify into a book.
 * On a **mixed** deployment only `by_source` can separate the two, so it wins
 * there even though it costs that pre-ledger history. Either way the two books
 * are never summed — they are different accounting systems, and a credits
 * figure added to a dollar figure is a number that means nothing.
 *
 * Lives here (not in the view) so the Overview teaser can quote exactly what
 * Insights quotes without importing anything that pulls in recharts.
 */
export function bookSlice(
  usage: UsageRollup | undefined,
  source: "olano" | "customer",
  split: boolean,
): UsageBySource {
  const totals = usage?.totals;
  const fallback: UsageBySource = {
    source,
    usd: totals?.usd ?? 0,
    credits: totals?.credits ?? 0,
    input_tokens: totals?.input_tokens ?? 0,
    output_tokens: totals?.output_tokens ?? 0,
    total_tokens: totals?.total_tokens ?? 0,
    calls: totals?.calls ?? 0,
  };
  if (!split) return fallback;
  const row = (usage?.by_source ?? []).find((r) => r.source === source);
  if (row) return row;
  // Mixed plan, but this book recorded nothing in the window: zeros, not the
  // whole-fleet fallback (which would credit one book with the other's spend).
  return {
    source,
    usd: 0,
    credits: 0,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    calls: 0,
  };
}

/**
 * The credits figure a headline quotes — LOCAL by design. The box records
 * every olano-billed call at call time (all chat tiers, cognition and
 * subagent work, and the priced service ledger: vision/image/speech/web), so
 * the combined `credits_summary.total` is the authoritative usage figure and
 * no cloud fetch sits on the stats path. The cloud stays authoritative only
 * for the billing BALANCE (the credits card / `/api/olano/credits`). Falls
 * back to the usage-ledger slice for payloads that predate the summary.
 * Shared by the Overview teaser and the Insights stat card so the two always
 * quote the same number over the same window.
 */
export function headlineCredits(
  summary: CreditsSummary | undefined,
  usage: UsageRollup | undefined,
  mixed: boolean,
): number {
  const total = summary?.total;
  if (typeof total === "number" && Number.isFinite(total)) return total;
  return bookSlice(usage, "olano", mixed).credits;
}
