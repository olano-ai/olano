// View-local types for the Scheduler. These mirror the SchedulerDB rows
// returned by GET /api/scheduler/tasks and /api/scheduler/history/{id} in
// olano/runtime/api.py — keep field names in sync with those handlers.

export interface Task {
  id: string;
  name: string;
  prompt: string;
  schedule: string | null;
  run_at: string | null;
  timezone: string;
  enabled: boolean;
  agent_name: string | null;
  created_at: string;
  last_run: string | null;
  next_run: string | null;
  run_count: number;
  visibility: "private" | "shared";
  created_by: string | null;
  // "db"  → a saved task in the scheduler DB (created here or via the tool).
  // "config" → an agent.json5 `schedule` entry, surfaced + editable here so it
  // is never a hidden job. Absent on older payloads → treat as "db".
  source?: "db" | "config";
  // `provider:model` this task runs on; "" (or absent on older payloads) means
  // the agent's configured model. Both stores carry it.
  model?: string;
  // Reasoning effort for this task's runs, a level the model it runs on
  // accepts; "" (or absent) means the agent's own setting. Both stores carry it.
  reasoning_effort?: string;
}

// History rows are intentionally loose: older runs may carry `timestamp`
// instead of `ran_at`, and either a `result` or an `error`.
export interface HistoryEntry {
  id?: number;
  task_id?: string;
  ran_at?: string | null;
  timestamp?: string | null;
  result?: string | null;
  error?: string | null;
}

// POST /api/scheduler/tasks → {"result": "...", "id": "..."}
export interface CreateResponse {
  result: string;
  id: string;
}

export type Visibility = "private" | "shared";

export interface CreateForm {
  name: string;
  agent_name: string;
  prompt: string;
  schedule: string;
  run_at: string;
  timezone: string;
  visibility: Visibility;
  // "" = the agent's configured model.
  model: string;
  // "" = the agent's own reasoning effort.
  reasoning_effort: string;
}

export interface EditForm {
  name: string;
  prompt: string;
  schedule: string;
  run_at: string;
  timezone: string;
  visibility: Visibility;
  // "" = the agent's configured model.
  model: string;
  // "" = the agent's own reasoning effort.
  reasoning_effort: string;
}

// Shape of the scheduler_update frame broadcast over /api/stream by
// _push_event("scheduler_update", {"action": ..., "id": ...}).
export interface SchedulerEvent {
  type: string;
  action?: "created" | "deleted" | "updated";
  id?: string;
}
