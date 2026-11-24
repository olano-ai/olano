// Cortex/Pulse view data shapes. Field names mirror the FastAPI handlers in
// olano/runtime/api.py exactly (do not rename).

import type { ErrorRemediation } from "../../core/types";

export interface PulseItem {
  id: string;
  source: string;
  lane: string;
  type: string;
  priority: "high" | "normal" | "low" | string;
  priority_score?: number;
  title: string;
  content: string;
  why_now: string;
  suggested_next_step: string;
  references: string[];
  // The card's agent's workspace, absolute — `references` are relative to it.
  // Attached by GET /api/pulse so the chat hand-off can draft full paths.
  workspace_root?: string;
  status: "pending" | "approved" | "rejected" | "delivered" | string;
  created_at: string;
  expires_at?: string | null;
  action: { type: string; [k: string]: unknown } | null;
  metadata: { kind?: string; worker_job_id?: string; [k: string]: unknown };
  agent: string;
  // Synthesised client-side for self-learning failure rows on the Checks tab.
  _kind?: string;
}

export interface PulseListResponse {
  items: PulseItem[];
  lane: string;
  count: number;
}

// /api/pulse/approve response — drives the outcome card.
export interface ApprovePulseResponse {
  ok: boolean;
  message: string;
  action_type: string;
  references: string[];
  kind: string;
  title: string;
  job_id: string;
  agent: string;
}

export interface CognitionPhase {
  id: string;
  label: string;
  feature: string;
  policy: string;
  in_think_cycle: boolean;
  enabled: boolean;
  cadence: string;
  next_run: string | null;
  last_run: string | null;
  last_status: "completed" | "skipped" | "error" | "empty" | null | string;
  last_summary: string;
  last_code: string;
  last_remediation?: ErrorRemediation | null;
}

// Raw per-agent overrides from agent.json5 `cognition` (null ⇒ inherit global).
// Distinct from the resolved effective booleans on CognitionStatusResponse — the
// UI needs these to render a three-state Inherit / On / Off control per agent.
// Every field is three-state: null ⇒ this agent has no opinion and inherits the
// home-wide default. `enabled` included — it used to be a plain boolean, which
// is why the master switch could only ever turn Cortex OFF for an agent.
export interface CognitionOverrides {
  enabled: boolean | null;
  autonomous: boolean | null;
  auto_approve: boolean | null;
  intensity: string | null;
  // Budget for ONE background reasoning run. null ⇒ inherit, same as the
  // booleans — these were plain numbers on the backend, which meant every agent
  // permanently "had an opinion" and the All-agents value could never reach one.
  worker_max_iterations: number | null;
  worker_timeout_seconds: number | null;
  features: Record<string, boolean | null>;
}

// The home-wide values an agent inherits when it has no override — the
// "All agents" column.
export interface CognitionDefaults {
  enabled: boolean;
  autonomous: boolean;
  auto_approve: boolean;
  intensity: string;
  worker_max_iterations: number;
  worker_timeout_seconds: number;
  features: Record<string, boolean>;
}

// What this agent actually runs with, straight from the backend gate functions.
// The UI renders this rather than resolving the two tiers itself — the one
// exception is the optimistic preview between a click and its response.
export interface CognitionResolved {
  enabled: boolean;
  autonomous: boolean;
  auto_approve: boolean;
  intensity: string;
  worker_max_iterations: number;
  worker_timeout_seconds: number;
  features: Record<string, boolean>;
}

// Legal range for the two budget fields, served by the backend so the UI never
// keeps its own copy to drift out of sync with the runtime's clamps.
export interface WorkerBudgetBounds {
  max_iterations_min: number;
  max_iterations_max: number;
  timeout_seconds_min: number;
  timeout_seconds_max: number;
}

export interface CognitionStatusResponse {
  agent: string;
  autonomy: boolean;
  // "Fully Autonomous" — auto-approve cognition outputs (skills, proposals,
  // actionable pulse items) without a human click.
  auto_approve: boolean;
  master_enabled: boolean;
  intensity: "relaxed" | "balanced" | "aggressive" | string;
  intensities: string[];
  features: Record<string, boolean>;
  // Three views of the same switches, so the panel can show the home-wide
  // default, this agent's override and the resolved answer side by side.
  overrides: CognitionOverrides;
  defaults: CognitionDefaults;
  resolved: CognitionResolved;
  worker_budget_bounds: WorkerBudgetBounds;
  deep_runs_today: number;
  deep_run_cap: number;
  deep_runs_remaining: number;
  // Phase ids currently executing (scheduled/background or manual) — for the
  // live "running" indicator.
  running: string[];
  phases: CognitionPhase[];
}

export interface CortexItem {
  id: string;
  // The owning agent — stamped by the API (the per-workspace index never
  // records one). Optional only for an older backend; the fleet-wide list
  // always carries it, and every action on a card routes to it.
  agent?: string;
  kind: "idea" | "brainstorm" | "prototype" | "project" | string;
  slug: string;
  title: string;
  status: "active" | "exploring" | "promoted" | "discarded" | string;
  summary: string;
  // Card-ready description: server-side falls back to the first paragraph of the
  // detail file when `summary` is empty, so cards always have something to show.
  preview?: string;
  tags: string[];
  // Folder relative to the workspace (what the index stores) …
  path: string;
  // … and the same folder absolute, attached by the API for the chat hand-off.
  abs_path?: string;
  detail_file: string;
  source: string;
  created_at: string;
  updated_at: string;
  links: Record<string, string>;
  // Written by the Cortex Rate & Promote phase (or a human via the rating
  // endpoint). Absent on an item nobody has rated yet.
  rating?: CortexRating | null;
  // The item's latest background run (Create project / Explore), stamped by
  // the API: a running one, or one that ended within the hour. Drives the
  // card's "Creating project..." state and its "last attempt failed" line.
  job?: CortexJobRef | null;
}

// ── Cortex jobs (olano/cognition/jobs.py) ─────────────────────────────────────

export type CortexJobStatus = "running" | "done" | "error" | "stopped" | "interrupted";

export interface CortexJobLogEntry {
  ts: string;
  // start | end | end_error | note | step | step_done | step_error | worker |
  // worker_error | tool | tool_result | tool_error | text
  type: string;
  text: string;
  tool?: string;
}

// What a card needs to show a run (GET /api/cortex/items stamps this).
export interface CortexJobRef {
  id: string;
  kind: string;
  status: CortexJobStatus;
  outcome?: string;
  title: string;
  started_at: string;
  ended_at?: string;
  duration_seconds?: number;
  error?: string;
  last_step?: CortexJobLogEntry;
}

export interface CortexJob extends CortexJobRef {
  agent: string;
  target: string;
  // manual | autonomous | approved
  source: string;
  detail: Record<string, unknown>;
  summary: string;
  result: Record<string, unknown>;
  log_count: number;
  log?: CortexJobLogEntry[];
  log_dropped?: number;
}

export interface CortexJobsResponse {
  jobs: CortexJob[];
  running: number;
}

export interface CortexJobStartResponse {
  ok: boolean;
  job: CortexJob;
  already_running: boolean;
}

export interface CortexRating {
  score: number;
  rationale?: string;
  dimensions?: Record<string, number>;
  recommendation?: "promote" | "execute" | "explore" | "keep" | "discard" | string;
  next_step?: string;
  rated_at?: string;
  rated_by?: string;
}

export type CortexKindCounts = Partial<Record<"idea" | "brainstorm" | "prototype" | "project", number>>;

export interface CortexItemsListResponse {
  // "" in the fleet-wide form (`?all_agents=true`), where `agents` lists the
  // agents that answered and `counts_by_agent` splits `counts` per agent so
  // the tab badges can follow the agents filter without a refetch.
  agent: string;
  agents?: string[];
  items: CortexItem[];
  counts: CortexKindCounts;
  counts_by_agent?: Record<string, CortexKindCounts>;
}

export interface CortexItemDetailResponse {
  agent: string;
  item: CortexItem;
  detail: string;
  files: string[];
  progress: { done: number; total: number; pct: number } | null;
}

export interface CortexPromoteResponse {
  ok: boolean;
  project: { id?: string; title?: string; path?: string } & Record<string, unknown>;
  summary: string;
  artifacts: string[];
}

export interface SelfLearningService {
  agent: string;
  distillation_count: number;
  last_run: string | null;
  last_weekly: string | null;
  interval_seconds: number;
  running: boolean;
  failures: Record<string, number>;
  last_failure: string[] | null;
}

export interface SelfLearningStatusResponse {
  services: SelfLearningService[];
  count: number;
}

// /api/cognition/optimize-* responses.
export interface OptimizeChangeFile {
  path: string;
  status: "changed" | "removed" | "unchanged" | string;
  lines_before: number;
  lines_after: number;
  lines_delta: number;
}
export interface OptimizeChanges {
  files: OptimizeChangeFile[];
  files_changed?: number;
  files_removed?: number;
  lines_before?: number;
  lines_after?: number;
  lines_delta?: number;
}
export interface OptimizeResponse {
  ok: boolean;
  agent: string;
  status: string;
  summary: string;
  artifacts: string[];
  backup_job: string | null;
  changes: OptimizeChanges | null;
}

// /api/cognition/actions
export interface CognitionAction {
  ts: string;
  action: string;
  summary: string;
  detail: Record<string, unknown> | null;
}
export interface CognitionActionsResponse {
  agent: string;
  actions: CognitionAction[];
}

// SSE frames from POST /api/cognition/generate (the "Think now" stream).
export type ThinkFrame =
  | { event: "phase_start"; id: string; label?: string; running?: string; index?: number; total?: number }
  | {
      event: "phase_done";
      id: string;
      label?: string;
      count?: number;
      summary?: string;
      status?: "completed" | "empty" | "skipped" | "error" | "disabled" | string;
      backup_job?: string | null;
      code?: string;
      remediation?: ErrorRemediation | null;
    }
  | { event: "all_done"; total_artifacts?: number }
  | { event: "error"; message?: string; code?: string; secret?: string; remediation?: ErrorRemediation | null };

export type TabId =
  | "pulse"
  | "brainstorm"
  | "ideas"
  | "prototypes"
  | "projects"
  | "skills"
  | "improve"
  | "checks";

export type StatusFilter = "pending" | "approved" | "rejected";
