// Shapes returned by the /api/analytics/* endpoints (see olano/analytics).

export interface UsageTotals {
  usd: number;
  /** Olano-managed charge (gateway-authoritative), rendered VERBATIM —
   * never through the USD→credits conversion (two different books). */
  credits?: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
  agents: number;
  models: number;
}

export interface UsageByAgent {
  agent: string;
  usd: number;
  /** Olano-managed charge for this row, rendered verbatim (never converted). */
  credits: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
}

export interface UsageByModel {
  model: string;
  usd: number;
  credits: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
}

/**
 * One billing book's slice of the window. `source` is "olano" (Olano-managed,
 * charged in credits) or "customer" (BYOK, provider-billed USD). The two are
 * different accounting systems and are never summed into each other — which is
 * exactly why the dashboard renders them as separate sections.
 */
export interface UsageBySource {
  source: "olano" | "customer" | string;
  usd: number;
  credits: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
}

/**
 * One work-origin's slice of the window: what KIND of work spent it —
 * "interactive" / "scheduled" / "cognition" / "delegate" / "pulse" /
 * "highlights" / "reports" / "prompt_gen" / "acp" / "peer", with "turns" for
 * ledger rows written before origins existed. Same two-books rule as
 * UsageBySource: usd and credits are never summed into each other.
 */
export interface UsageByOrigin {
  origin: string;
  usd: number;
  credits: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
}

/**
 * One managed chat tier's slice of the window ("turbo" / "pro" / "max" /
 * "auto") — the "which brain burned the credits" split. Ledger-sourced like
 * by_origin; BYOK models and the utility groups (vision/image/…) never appear.
 */
export interface UsageByTier {
  tier: string;
  usd: number;
  credits: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
}

export interface DailyPoint {
  date: string;
  usd: number;
  credits: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
  cum_usd: number;
  cum_credits: number;
  cum_tokens: number;
}

/** One UTC hour bucket ("YYYY-MM-DDTHH") of the rolling last-24h window. */
export interface HourlyPoint {
  hour: string;
  usd: number;
  credits: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
  cum_usd: number;
  cum_credits: number;
  cum_tokens: number;
}

export interface UsageRollup {
  totals: UsageTotals;
  by_agent: UsageByAgent[];
  by_model: UsageByModel[];
  /** Per-billing-book totals. Absent on payloads that predate the split. */
  by_source?: UsageBySource[];
  /** Per-work-origin totals ("where credits went"). Absent on old payloads. */
  by_origin?: UsageByOrigin[];
  /** Per-managed-tier totals (turbo/pro/max/auto). Absent on old payloads. */
  by_tier?: UsageByTier[];
  daily: DailyPoint[];
  /** Present only in the hourly (`?hours=`) window; `daily` is then empty. */
  hourly?: HourlyPoint[];
}

export interface ToolCount {
  tool: string;
  count: number;
}

export interface ToolsByAgent {
  agent: string;
  tool_calls: number;
  tools?: ToolCount[];
}

export interface SubagentCount {
  subagent: string;
  count: number;
}

export interface ToolRollup {
  totals: {
    tool_calls: number;
    subagent_calls: number;
    distinct_tools: number;
    distinct_subagents: number;
    agents: number;
    truncated: boolean;
  };
  by_agent: { agent: string; tool_calls: number }[];
  by_tool: ToolCount[];
  by_subagent: SubagentCount[];
  by_agent_tool: ToolsByAgent[];
}

// ── Services (non-model spend: image / TTS / STT / managed web) ──────────────

/**
 * One service's slice of the window. `calls` always counts; `credits` only
 * carries a figure for the services the cloud prices per call (the managed web
 * search/fetch APIs) — gateway-metered image and speech calls are counted but
 * never priced, which is what `priced_calls` distinguishes.
 */
export interface ServiceCount {
  service: string;
  calls: number;
  credits: number;
  priced_calls: number;
}

export interface ServicePoint {
  date: string;
  calls: number;
  credits: number;
}

/** One agent's non-model service usage. `credits` carries a figure only for the
 *  services the cloud prices per call — see ServiceCount. */
export interface ServiceByAgent {
  agent: string;
  calls: number;
  credits: number;
  priced_calls: number;
}

export interface ServiceRollup {
  totals: {
    calls: number;
    credits: number;
    priced_calls: number;
    services: number;
    agents: number;
  };
  by_service: ServiceCount[];
  by_agent: ServiceByAgent[];
  by_source: { source: string; calls: number; credits: number; priced_calls: number }[];
  daily: ServicePoint[];
  hourly?: { hour: string; calls: number; credits: number }[];
}

export interface A2AEdge {
  from: string;
  to: string;
  count: number;
}

export interface A2AByAgent {
  agent: string;
  sent: number;
  received: number;
}

export interface A2ARecent {
  from: string;
  to: string;
  kind: string;
  message: string;
  ts: string;
}

export interface A2ARollup {
  totals: { messages: number; agents: number; edges: number };
  edges: A2AEdge[];
  by_agent: A2AByAgent[];
  recent: A2ARecent[];
}

/**
 * The combined LOCAL olano-credits figure for the window: managed model spend
 * (usage ledger) + managed service spend (service ledger — vision, image,
 * speech, web). Same unit, same book, computed entirely on the box — the
 * number the dashboard headlines. BYOK dollars never fold in.
 */
export interface CreditsSummary {
  model: number;
  services: number;
  total: number;
}

export interface AnalyticsOverview {
  window_days: number;
  /** Set when the payload is a rolling hourly window (e.g. 24 for "1 day"). */
  window_hours?: number;
  since: string;
  generated_at: string;
  usage: UsageRollup;
  tools: ToolRollup;
  /** Non-model service spend. Absent on payloads that predate the ledger. */
  services?: ServiceRollup;
  a2a: A2ARollup;
  /** Combined local credits for the window (see CreditsSummary). */
  credits_summary?: CreditsSummary;
}

// ── Journal ──────────────────────────────────────────────────────────────────

/**
 * A journal day carries BOTH billing books, exactly like the usage rollup:
 * `usd` is the BYOK estimate (0 for Olano-managed calls — threadcost prices
 * them at zero on purpose) and `credits` is the gateway's authoritative charge
 * for the managed ones, grafted on from the usage ledger. Absent on entries
 * written before the split, hence optional.
 */
export interface JournalAgentRow {
  agent: string;
  usd: number;
  credits?: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
  tool_calls: number;
  top_tools: ToolCount[];
  a2a_sent: number;
  a2a_received: number;
}

export interface JournalModelRow {
  model: string;
  usd: number;
  credits?: number;
  total_tokens: number;
  calls: number;
}

export interface JournalEntry {
  schema_version: number;
  date: string;
  generated_at: string;
  metrics: {
    usd: number;
    credits?: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    calls: number;
    tool_calls: number;
    a2a_messages: number;
    active_agents: string[];
  };
  by_agent: JournalAgentRow[];
  by_model: JournalModelRow[];
  events: {
    cognition_actions: { action: string; summary: string; ts: string }[];
    cortex: { kind: string; title: string; status: string; created: boolean; ts: string }[];
  };
  narrative: string | null;
  narrative_model?: string;
  /** Transient (never persisted): why the last highlights generation failed. */
  narrative_error?: string;
}

export interface JournalList {
  days: string[];
  today: string;
  /** Most recent day with recorded usage; the UI opens on this. */
  latest_active?: string | null;
}

export type ReportPeriod = "weekly" | "monthly";

/** A weekly/monthly rollup over the daily per-agent highlights. Narrative is
 *  scoped server-side to the viewer's agents, exactly like the daily one. */
export interface HighlightReport {
  period: ReportPeriod;
  key: string;
  start: string;
  end: string;
  generated_at: string | null;
  narrative: string | null;
  narrative_model?: string | null;
  narrative_error?: string;
}

export interface ReportList {
  period: ReportPeriod;
  keys: string[];
}
