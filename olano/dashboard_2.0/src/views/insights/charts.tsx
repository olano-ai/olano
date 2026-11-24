// Themed recharts wrappers. All chart colours come from the Aurora palette in
// helpers.ts (concrete hexes for swatches) while axes/grids use CSS-var strokes
// so they track the active theme. Each chart degrades to an empty-state hint.

import type { ReactNode } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useT } from "../../core/i18n";
import { STR } from "./strings";
import {
  CHART_COLORS,
  colorFor,
  creditsAxisFormatter,
  fmtDay,
  fmtHour,
  fmtHourLabel,
  fmtUsd,
  fmtCreditsAmount,
  prettyModel,
  truncLabel,
  usdAxisFormatter,
} from "./helpers";
import type {
  A2AByAgent,
  DailyPoint,
  ServiceByAgent,
  ServiceCount,
  SubagentCount,
  ToolCount,
  UsageByAgent,
  UsageByModel,
} from "./types";

/**
 * Which billing book a spend chart is drawing.
 *
 * "usd" is the BYOK book — a provider bill, always drawn in dollars, never
 * converted into the deployment's display unit (restating someone else's
 * invoice in credits describes a different transaction). "credits" reads the
 * gateway's own charge and renders it VERBATIM — managed rows carry usd == 0 by
 * design, so converting would re-derive a price from a number that
 * intentionally isn't one. The two books never share an axis.
 */
export type SpendMetric = "usd" | "credits";

const AXIS = { fontSize: 11, fill: "var(--text-3)" } as const;
const GRID = "var(--border)";
// Cap bar thickness so a single-category chart shows a slim bar, not a slab.
const MAX_BAR = 30;

// ── shells ────────────────────────────────────────────────────────────────

export function ChartCard({
  title,
  control,
  empty,
  emptyLabel,
  height = 240,
  children,
}: {
  title: string;
  control?: ReactNode;
  empty?: boolean;
  emptyLabel?: string;
  height?: number;
  children: ReactNode;
}) {
  const t = useT(STR);
  return (
    <div className="card" style={{ padding: 16, display: "flex", flexDirection: "column" }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
        <span style={{ fontWeight: 600, fontSize: 13.5 }}>{title}</span>
        {control}
      </div>
      {empty ? (
        <div
          className="muted"
          style={{
            height,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 12.5,
          }}
        >
          {emptyLabel ?? t("empty.chart")}
        </div>
      ) : (
        <div style={{ width: "100%", height }}>{children}</div>
      )}
    </div>
  );
}

// Custom tooltip: an HTML card themed with tokens, formats each value with a fn.
// Zero-valued series are dropped so hovering an empty day doesn't show "0 / 0";
// if nothing is left, the tooltip is suppressed entirely.
function TipFactory(format: (value: number, name: string) => string) {
  return function ThemedTooltip({
    active,
    payload,
    label,
  }: {
    active?: boolean;
    payload?: { name: string; value: number; color?: string; dataKey?: string }[];
    label?: string | number;
  }) {
    if (!active || !payload || payload.length === 0) return null;
    const rows = payload.filter((p) => typeof p.value === "number" && p.value !== 0);
    if (rows.length === 0) return null;
    return (
      <div
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border-strong)",
          borderRadius: "var(--r-sm)",
          padding: "8px 10px",
          fontSize: 12,
          color: "var(--text)",
          boxShadow: "var(--shadow, 0 4px 16px rgba(0,0,0,.25))",
        }}
      >
        {label !== undefined && (
          <div style={{ fontWeight: 600, marginBottom: 4 }}>
            {typeof label === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}$/.test(label)
              ? fmtHourLabel(label)
              : typeof label === "string" && /^\d{4}-\d{2}-\d{2}$/.test(label)
                ? fmtDay(label)
                : label}
          </div>
        )}
        {rows.map((p, i) => (
          <div key={i} className="row" style={{ gap: 6, justifyContent: "space-between" }}>
            <span className="row" style={{ gap: 6 }}>
              <span
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: 2,
                  background: p.color,
                  display: "inline-block",
                }}
              />
              <span className="muted">{p.name}</span>
            </span>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>{format(p.value, p.name)}</span>
          </div>
        ))}
      </div>
    );
  };
}

const UsdTip = TipFactory((v) => fmtUsd(v));
const CreditsTip = TipFactory((v) => fmtCreditsAmount(v));
const CountTip = TipFactory((v) => String(v));

// Per-metric axis/tooltip pair, so a chart picks its book once and every part
// of it agrees. `max` is the dataset maximum in the metric's own unit. Both
// arms are unit-fixed — neither consults the deployment's display mode.
function spendAxis(metric: SpendMetric, max: number) {
  return metric === "credits"
    ? { tick: creditsAxisFormatter(max), Tip: CreditsTip }
    : { tick: usdAxisFormatter(max), Tip: UsdTip };
}

// ── time series ─────────────────────────────────────────────────────────────

export function SpendOverTime({
  daily,
  cumulative,
  hourly = false,
  metric = "usd",
  label,
}: {
  daily: DailyPoint[];
  cumulative: boolean;
  hourly?: boolean;
  metric?: SpendMetric;
  /** Series name, shown in the tooltip row ("spend" / "credits"). */
  label?: string;
}) {
  const t = useT(STR);
  const seriesLabel = label ?? t("legend.spend");
  const key = (
    metric === "credits"
      ? cumulative
        ? "cum_credits"
        : "credits"
      : cumulative
        ? "cum_usd"
        : "usd"
  ) as keyof DailyPoint;
  const max = Math.max(0, ...daily.map((d) => (d[key] as number) ?? 0));
  const { tick, Tip } = spendAxis(metric, max);
  const fmtX = hourly ? fmtHour : fmtDay;
  // Distinct gradient ids per metric: two of these charts render side by side
  // (credits section + BYOK section) and a shared id would let one steal the
  // other's fill.
  const gradId = `spendGrad-${metric}`;
  return (
    <ResponsiveContainer>
      <AreaChart data={daily} margin={{ top: 4, right: 8, left: 4, bottom: 0 }}>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={CHART_COLORS[0]} stopOpacity={0.35} />
            <stop offset="100%" stopColor={CHART_COLORS[0]} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="date" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} tickFormatter={fmtX} minTickGap={24} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={56} tickFormatter={tick} />
        <Tooltip content={<Tip />} />
        <Area
          type="monotone"
          dataKey={key as string}
          name={seriesLabel}
          stroke={CHART_COLORS[0]}
          strokeWidth={2}
          fill={`url(#${gradId})`}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/**
 * Model calls over the window — the work-volume series that replaced the token
 * one. A call is a model round-trip, so a tool-heavy turn contributes several;
 * unlike tokens it needs no unit conversion and reads the same on either
 * billing book, which is why it lives in the shared activity band.
 */
export function CallsOverTime({
  daily,
  cumulative,
  label,
  hourly = false,
}: {
  daily: DailyPoint[];
  cumulative: boolean;
  label: string;
  hourly?: boolean;
}) {
  const key = cumulative ? "cum_calls" : "calls";
  const hasData = daily.some((d) => d.calls > 0);
  const fmtX = hourly ? fmtHour : fmtDay;
  // The payload carries no cumulative-calls column (tokens and cost do), so
  // fold one here rather than widening every rollup for a display-only series.
  let running = 0;
  const rows = daily.map((d) => {
    running += d.calls ?? 0;
    return { ...d, cum_calls: running };
  });
  if (cumulative) {
    return (
      <ResponsiveContainer>
        <AreaChart data={rows} margin={{ top: 4, right: 8, left: 4, bottom: 0 }}>
          <defs>
            <linearGradient id="callsGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={CHART_COLORS[2]} stopOpacity={0.35} />
              <stop offset="100%" stopColor={CHART_COLORS[2]} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="date" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} tickFormatter={fmtX} minTickGap={24} />
          <YAxis tick={AXIS} tickLine={false} axisLine={false} width={48} allowDecimals={false} />
          <Tooltip content={<CountTip />} />
          <Area type="monotone" dataKey={key} name={label} stroke={CHART_COLORS[2]} strokeWidth={2} fill="url(#callsGrad)" />
        </AreaChart>
      </ResponsiveContainer>
    );
  }
  return (
    <ResponsiveContainer>
      <BarChart data={rows} margin={{ top: 4, right: 8, left: 4, bottom: 0 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="date" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} tickFormatter={fmtX} minTickGap={24} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={48} allowDecimals={false} />
        <Tooltip content={<CountTip />} cursor={hasData ? { fill: "var(--surface-2)" } : false} />
        <Bar dataKey={key} name={label} maxBarSize={MAX_BAR} fill={CHART_COLORS[2]} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// ── distributions ────────────────────────────────────────────────────────────

export function SpendByModelPie({
  data,
  metric = "usd",
}: {
  data: UsageByModel[];
  metric?: SpendMetric;
}) {
  const rows = data
    .filter((d) => (d[metric] ?? 0) > 0)
    .map((d) => ({ name: prettyModel(d.model), key: d.model, value: d[metric] ?? 0 }));
  const { Tip } = spendAxis(metric, Math.max(0, ...rows.map((r) => r.value)));
  return (
    <ResponsiveContainer>
      <PieChart>
        <Pie
          data={rows}
          dataKey="value"
          nameKey="name"
          innerRadius="52%"
          outerRadius="80%"
          paddingAngle={rows.length > 1 ? 2 : 0}
          minAngle={12}
          stroke="var(--surface)"
          strokeWidth={2}
        >
          {rows.map((r, i) => (
            <Cell key={r.key} fill={colorFor(r.key, i)} />
          ))}
        </Pie>
        <Tooltip content={<Tip />} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
      </PieChart>
    </ResponsiveContainer>
  );
}

export function SpendByAgentBar({
  data,
  metric = "usd",
  label,
}: {
  data: UsageByAgent[];
  metric?: SpendMetric;
  label?: string;
}) {
  const t = useT(STR);
  const seriesLabel = label ?? t("legend.spend");
  // Rank by the book being drawn: managed agents all tie at $0, so the default
  // (usd-sorted) order is meaningless in the credits section.
  const rows = [...data]
    .sort((a, b) => (b[metric] ?? 0) - (a[metric] ?? 0))
    .slice(0, 12)
    .map((d) => ({ name: d.agent, value: d[metric] ?? 0 }));
  const max = Math.max(0, ...rows.map((r) => r.value));
  const { tick, Tip } = spendAxis(metric, max);
  return (
    <ResponsiveContainer>
      <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} tickFormatter={tick} />
        <YAxis type="category" dataKey="name" tick={AXIS} tickLine={false} axisLine={false} interval={0} width={92} tickFormatter={(v) => truncLabel(String(v), 13)} />
        <Tooltip content={<Tip />} cursor={{ fill: "var(--surface-2)" }} />
        <Bar dataKey="value" name={seriesLabel} maxBarSize={MAX_BAR} radius={[0, 4, 4, 0]}>
          {rows.map((r, i) => (
            <Cell key={r.name} fill={colorFor(r.name, i)} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Model calls per agent — the work-volume ranking that replaced tokens-by-agent. */
export function CallsByAgentBar({ data, label }: { data: UsageByAgent[]; label: string }) {
  const rows = [...data]
    .sort((a, b) => (b.calls ?? 0) - (a.calls ?? 0))
    .slice(0, 12)
    .map((d) => ({ name: d.agent, calls: d.calls ?? 0 }));
  return (
    <ResponsiveContainer>
      <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} allowDecimals={false} />
        <YAxis type="category" dataKey="name" tick={AXIS} tickLine={false} axisLine={false} interval={0} width={92} tickFormatter={(v) => truncLabel(String(v), 13)} />
        <Tooltip content={<CountTip />} cursor={{ fill: "var(--surface-2)" }} />
        <Bar dataKey="calls" name={label} maxBarSize={MAX_BAR} radius={[0, 4, 4, 0]}>
          {rows.map((r, i) => (
            <Cell key={r.name} fill={colorFor(r.name, i)} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function ToolCallsByAgentBar({ data }: { data: { agent: string; tool_calls: number }[] }) {
  const t = useT(STR);
  const rows = data.slice(0, 12).map((d) => ({ name: d.agent, calls: d.tool_calls }));
  return (
    <ResponsiveContainer>
      <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} allowDecimals={false} />
        <YAxis type="category" dataKey="name" tick={AXIS} tickLine={false} axisLine={false} interval={0} width={92} tickFormatter={(v) => truncLabel(String(v), 13)} />
        <Tooltip content={<CountTip />} cursor={{ fill: "var(--surface-2)" }} />
        <Bar dataKey="calls" name={t("legend.toolCalls")} maxBarSize={MAX_BAR} radius={[0, 4, 4, 0]}>
          {rows.map((r, i) => (
            <Cell key={r.name} fill={colorFor(r.name, i)} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// Shared horizontal ranked bar (top tools / top subagents).
function RankedHBar({ rows, color }: { rows: { name: string; count: number }[]; color: string }) {
  const t = useT(STR);
  return (
    <ResponsiveContainer>
      <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} allowDecimals={false} />
        <YAxis type="category" dataKey="name" tick={AXIS} tickLine={false} axisLine={false} interval={0} width={124} tickFormatter={(v) => truncLabel(String(v), 18)} />
        <Tooltip content={<CountTip />} cursor={{ fill: "var(--surface-2)" }} />
        <Bar dataKey="count" name={t("legend.callsCount")} maxBarSize={MAX_BAR} fill={color} radius={[0, 4, 4, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

/**
 * Non-model service calls by kind (image / TTS / STT / web search / web fetch).
 * Counts only — the gateway meters image and speech cloud-side and reports no
 * per-call charge, so a credits axis here would be blank for most rows.
 */
export function ServiceCallsBar({
  data,
  labelFor,
}: {
  data: ServiceCount[];
  labelFor: (service: string) => string;
}) {
  return (
    <RankedHBar
      rows={data.slice(0, 12).map((d) => ({ name: labelFor(d.service), count: d.calls }))}
      color={CHART_COLORS[6]}
    />
  );
}

/**
 * Non-model service calls by agent — who is spending the image/speech/web
 * budget. Counts, for the same reason ServiceCallsBar is counts: the gateway
 * meters image and speech cloud-side and reports no per-call charge, so a
 * credits axis would read 0 for most agents. Ranked here rather than trusting
 * the payload order, since `by_agent` is not guaranteed sorted.
 */
export function ServiceCallsByAgentBar({ data }: { data: ServiceByAgent[] }) {
  const t = useT(STR);
  const rows = [...data]
    .sort((a, b) => (b.calls ?? 0) - (a.calls ?? 0))
    .slice(0, 12)
    .map((d) => ({ name: d.agent, calls: d.calls ?? 0 }));
  return (
    <ResponsiveContainer>
      <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} allowDecimals={false} />
        <YAxis type="category" dataKey="name" tick={AXIS} tickLine={false} axisLine={false} interval={0} width={92} tickFormatter={(v) => truncLabel(String(v), 13)} />
        <Tooltip content={<CountTip />} cursor={{ fill: "var(--surface-2)" }} />
        <Bar dataKey="calls" name={t("legend.serviceCalls")} maxBarSize={MAX_BAR} radius={[0, 4, 4, 0]}>
          {rows.map((r, i) => (
            <Cell key={r.name} fill={colorFor(r.name, i)} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function TopToolsBar({ data }: { data: ToolCount[] }) {
  return <RankedHBar rows={data.slice(0, 12).map((d) => ({ name: d.tool, count: d.count }))} color={CHART_COLORS[1]} />;
}

export function TopSubagentsBar({ data }: { data: SubagentCount[] }) {
  return <RankedHBar rows={data.slice(0, 12).map((d) => ({ name: d.subagent, count: d.count }))} color={CHART_COLORS[4]} />;
}

export function A2AByAgentBar({
  data,
  labels,
}: {
  data: A2AByAgent[];
  labels: { sent: string; received: string };
}) {
  const rows = data.slice(0, 12).map((d) => ({ name: d.agent, sent: d.sent, received: d.received }));
  const rotate = rows.length > 5;
  return (
    <ResponsiveContainer>
      <BarChart data={rows} margin={{ top: 4, right: 8, left: 4, bottom: rotate ? 12 : 0 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="name" tick={AXIS} tickLine={false} axisLine={{ stroke: GRID }} interval={0} tickFormatter={(v) => truncLabel(String(v), 10)} angle={rotate ? -35 : 0} textAnchor={rotate ? "end" : "middle"} height={rotate ? 70 : 24} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} width={36} allowDecimals={false} />
        <Tooltip content={<CountTip />} cursor={{ fill: "var(--surface-2)" }} />
        <Legend verticalAlign="top" wrapperStyle={{ fontSize: 11, paddingBottom: 8 }} />
        <Bar dataKey="sent" name={labels.sent} maxBarSize={22} fill={CHART_COLORS[0]} radius={[3, 3, 0, 0]} />
        <Bar dataKey="received" name={labels.received} maxBarSize={22} fill={CHART_COLORS[6]} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
