/**
 * The shapes of the runtime API the app reads, as the runtime sends them
 * (olano/runtime/routes/*.py, olano/runtime/auth.py). Only the fields the app
 * uses are listed; everything is optional where an older runtime may omit it.
 */

import type { MediaRef } from "../shared/dashboard.ts";

export interface User {
  id: string;
  email: string;
  username?: string;
  name: string;
  title?: string;
  is_admin: boolean;
  timezone?: string;
}

export interface Me {
  user: User;
  multi_user: boolean;
  agents?: { agent_name: string; role: string }[];
}

export interface Status {
  status: string;
  booting?: boolean;
  setup_complete?: boolean;
  runtime_version?: string | null;
  capabilities?: Record<string, unknown>;
}

export interface Agent {
  name: string;
  display_name?: string;
  title?: string;
  emoji?: string;
  description?: string;
  model?: string;
  running: boolean;
  paused?: boolean;
  /** "running", "starting", "stopped", "error", "configured", "paused" or "blocked". */
  status?: string;
  /** Why it failed to start, when `status` is "error". */
  error?: string;
  blocked?: boolean;
  /** Why it is held back (a missing credential, a plan limit). */
  blocked_reason?: string;
  avatar_url?: string | null;
  color?: string | null;
}

export interface ThreadSummary {
  id: string;
  name?: string;
  title?: string;
  preview?: string;
  origin?: string;
  channel?: string;
  sender?: string;
  message_count?: number;
  created_at?: string;
  updated_at?: string;
  running?: boolean;
  /** On a search result: where the words were found. */
  match?: { role: string; snippet: string };
}

/**
 * One page of an agent's threads, newest first (`GET …/threads?limit=N`), or
 * of a search's hits. `next_before` is the opaque cursor of the page below.
 */
export interface ThreadPage {
  threads: ThreadSummary[];
  has_more: boolean;
  next_before: string | null;
  /** Search only: how many transcripts this request looked at. */
  scanned?: number;
}

/** One persisted row of a thread's transcript. */
export interface ThreadRow {
  role: "user" | "peer_inbound" | "assistant" | "tool_call" | "tool_result" | "system" | string;
  content: string;
  channel?: string;
  sender?: string;
  from?: string;
  tool_call_id?: string;
  name?: string;
  args?: string;
  ts?: string;
  steer?: boolean;
  segments?: { text: string; after: string | null }[];
  media?: MediaRef[];
  /** The turn an assistant row answered (runner._TURN_ID): what a report or vote is keyed on. */
  turn_id?: string;
  /** "magic_link" on the row that reports how a magic link ended, with its fields. */
  event?: string;
  link_id?: string;
  status?: string;
  flow?: string;
  label?: string;
  target_agent?: string;
  apply?: string;
}

export interface RunInfo {
  run_id: string;
  thread_id: string;
  turn_id?: string;
  cursor?: number;
  running: boolean;
}

export interface ThreadDetail {
  id: string;
  messages: ThreadRow[];
  origin?: string;
  channel?: string;
  run?: RunInfo | null;
  /** The model (and reasoning level) the last reply ran on, when stamped. */
  model?: string;
  reasoning_effort?: string;
  /**
   * A paged read (`limit`): the first returned message's position, which is
   * the `before` of the page above it, and whether there is one. Absent from
   * a whole-transcript read, and from a runtime that predates paging.
   */
  start?: number;
  has_more?: boolean;
}

export type CortexKind = "brainstorm" | "idea" | "prototype" | "project";

export interface CortexItem {
  id: string;
  kind: CortexKind;
  title: string;
  status: "active" | "exploring" | "promoted" | "discarded" | string;
  summary?: string;
  tags?: string[];
  created_at?: string;
  updated_at?: string;
  agent?: string;
  preview?: string;
  /** The item's folder, relative to the agent's workspace. */
  path?: string;
  /** The same folder as a full path, which is what the agent opens. */
  abs_path?: string;
  rating?: {
    score?: number;
    rationale?: string;
    recommendation?: string;
    next_step?: string;
  } | null;
  /** Where a promoted item went: the successor's folder, id and stage. */
  links?: { promoted_to?: string; promoted_to_id?: string; promoted_to_kind?: string; [key: string]: unknown } | null;
  /** The item's latest background run: running, or ended within the hour. */
  job?: {
    id: string;
    kind: string;
    status: string;
    title?: string;
    /** For a promotion, `to` is the stage it is heading for. */
    detail?: { to?: string; [key: string]: unknown } | null;
    /** Why a run ended badly. */
    error?: string;
    /** The step the run is on, for a live line under the item. */
    last_step?: { text: string; tool?: string; type?: string } | null;
  } | null;
}

export interface CortexItems {
  agent?: string;
  items: CortexItem[];
  counts?: Partial<Record<CortexKind, number>>;
}

export interface CortexItemDetail {
  agent: string;
  item: CortexItem;
  detail: string;
  progress?: { done: number; total: number; pct: number } | null;
}

export interface PulseItem {
  id: string;
  lane: "pulse" | "approval" | "ops" | string;
  type?: string;
  priority?: "urgent" | "normal" | "low" | string;
  title?: string;
  content?: string;
  why_now?: string;
  suggested_next_step?: string;
  status: string;
  created_at?: string;
  agent?: string;
  /**
   * What approving does. `restore_backup` marks work the agent already did
   * on its own: approving keeps it, and the card offers Undo instead of a
   * second "do it".
   */
  action?: { type?: string; [key: string]: unknown } | null;
}

export interface HitlPending {
  id: string;
  agent: string;
  action: string;
  description?: string;
  risk?: "low" | "medium" | "high" | string;
  args?: unknown;
  created_at?: string;
  timeout_seconds?: number;
}

export interface Notification {
  id: string;
  kind: string;
  level: "info" | "success" | "warn" | "error";
  title: string;
  body?: string;
  agent?: string | null;
  created_at?: string;
  updated_at?: string;
  read: boolean;
  count?: number;
  dedupe_key?: string | null;
  /** The dashboard page that fixes it (a dashboard route; the app maps it in src/inbox/alertActions.ts). */
  action?: { label?: string; href?: string } | null;
}

export interface NotificationList {
  notifications: Notification[];
  unread: number;
}

export interface PushStatus {
  enabled?: boolean;
  managed?: boolean;
  reason?: string;
}

/** How the deployment counts money (`GET /api/credits`). */
export interface CreditsDisplay {
  /** There is an Olano account behind this deployment that credits are spent from. */
  credits_enabled?: boolean;
  unit_name?: string | null;
  unit_name_plural?: string | null;
  decimals?: number | null;
}

/** The account's credit balance (`GET /api/olano/credits`), read from Olano Cloud. */
export interface CreditsBalance {
  available: boolean;
  balance?: number;
  included_remaining?: number;
  purchased_remaining?: number;
  monthly_included?: number;
  monthly_used?: number;
  period_end?: string | null;
  stale?: boolean;
}

/** Credits each Olano service used over a window (`GET /api/olano/services/usage`). */
export interface ServiceUsage {
  available: boolean;
  window?: { since: string; days: number };
  total_credits?: number;
  services?: { service: string; label: string; calls: number; credits: number }[];
  stale?: boolean;
}

/** A group chat: a named roster of agents and one shared transcript (`/api/groups`). */
export interface GroupSummary {
  id: number;
  name: string;
  /** In speaking order: the first answers a message that names nobody. */
  members: string[];
  message_count: number;
  created_at?: string;
  updated_at?: string;
  /** The account that made it (a user id). */
  created_by?: string;
}

/** One line of a group's transcript. */
export interface GroupMessage {
  id: number;
  group_id: number;
  ts: string;
  /** `human-<username>` for a person, the agent's name for an agent. */
  author: string;
  author_kind: "human" | "agent" | "system";
  /** Who a message was aimed at ("" when it named nobody; a list joined by commas for a person's). */
  addressee: string;
  reply_to?: string;
  body: string;
  /** Written by the person reading (stamped per request). */
  mine?: boolean;
  segments?: { text: string; after: string | null }[];
  media?: MediaRef[];
}

export interface CascadeState {
  running: boolean;
  hops?: number;
  max_hops?: number;
  speaker?: string;
  stopping?: boolean;
  seconds_left?: number;
}

/** One issue on the deployment's tracker (`/api/issues`, olano/issues/store.py). */
export interface Issue {
  id: number;
  title: string;
  body: string;
  /** A principal: `human-<username>` or `ai-<agent>`. */
  author: string;
  assignee: string;
  state: "open" | "closed";
  type: string;
  labels: string[];
  created_at: string;
  updated_at: string;
  closed_at?: string | null;
  /** In lists only. */
  comment_count?: number;
}

/** What happened to an issue, oldest first: comments, assignments, closes and reopens. */
export interface IssueEvent {
  id: number;
  issue_id: number;
  ts: string;
  actor: string;
  kind: string;
  payload: { body?: string; assignee?: string; reason?: string; labels?: string[]; type?: string };
}

/** A file or folder in an agent's workspace (`GET /api/files`). */
export interface WorkspaceEntry {
  name: string;
  /** Relative to the workspace's top. */
  path: string;
  type: "file" | "directory";
  size: number;
  modified: string;
}

export interface FileListing {
  entries: WorkspaceEntry[];
  truncated: boolean;
  total: number;
}

export interface FileSearch {
  results: (WorkspaceEntry & { agent: string })[];
  truncated: boolean;
}
