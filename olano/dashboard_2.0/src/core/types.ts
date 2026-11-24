// Core API types. Hand-written for the slice we consume today; run
// `npm run gen:types` to emit src/core/types.gen.ts from the live FastAPI
// OpenAPI schema and migrate these to the generated source of truth.

// Which dashboard shell a human gets. Its own knob on the user record —
// deliberately NOT derived from is_admin, so an admin can be given the simple
// shell and a member the full dashboard. Absent (older backend, or a row from
// before the column existed) reads as "advanced", which is what everyone had
// before, so an upgrade never changes anyone's screen.
export type UiMode = "advanced" | "simple";
export const DEFAULT_UI_MODE: UiMode = "advanced";

export interface User {
  id: string;
  email: string;
  // Permanent handle: what `human-<username>` is built from across the issue
  // tracker. Minted at account creation and never editable — `name` is the
  // half a person can change.
  username: string;
  name: string;
  // The person's position ("Head of Sales"), shown under their name. Free
  // text, editable, may be empty. Nothing keys off it.
  title?: string;
  is_admin: boolean;
  is_active: boolean;
  can_terminal?: boolean;
  // The RAW stored value (what the admin UI edits). Prefer MeResponse.ui_mode
  // for deciding what to render — that one is resolved server-side.
  ui_mode?: UiMode;
  // The RAW stored flag behind MeResponse.langfuse_links (what Admin -> Users
  // edits). Absent on an older backend.
  langfuse_links?: boolean;
  timezone: string;
  created_at: string;
}

export interface AgentAccessRow {
  agent_name: string;
  role: string;
}

export interface MeResponse {
  user: User;
  agents: AgentAccessRow[];
  multi_user: boolean;
  // The RESOLVED shell for this human, after single-user and unset-column
  // fallbacks. This is what the app forks on. Absent on an older backend →
  // "advanced".
  ui_mode?: UiMode;
  // Whether this human's dashboard shows "Langfuse" links, resolved
  // server-side (per-user flag; always on single-user). Absent → false.
  langfuse_links?: boolean;
  // First-run setup wizard gate: true for admins of a deployed (multi-user)
  // instance until any admin completes or skips the tour. Absent on older
  // backends → treated as false.
  //
  // admin_password_rotated / vault_password_rotated tell the wizard whether
  // a provisioned temporary credential is still in play; the profile step
  // shows only the halves that still have one to replace, and is dropped
  // entirely when neither does (the owner chose both on the way in).
  onboarding?: {
    required: boolean;
    admin_password_rotated?: boolean;
    vault_password_rotated?: boolean;
  };
}

export interface Agent {
  // The handle: folder name, API path segment, `talk_to_agent` target.
  // Permanent. Render `agentLabel(agent)` rather than this.
  name: string;
  // The face: editable, may be empty. Falls back to `name`.
  display_name?: string;
  // The position ("Recruitment Specialist"): the server's resolved `org.title`,
  // shown under the display name. May be empty.
  title?: string;
  emoji?: string;
  model?: string;
  description?: string;
  channels?: string[];
  subagent_count?: number;
  tool_count?: number;
  running?: boolean;
  paused?: boolean;
  status?: string;
  // Why a boot failed (status "error"), from the runtime's registry.
  error?: string;
  // Refused by the deployment (agent cap, feature switched off) and why.
  blocked?: boolean;
  blocked_reason?: string;
  color?: string | null;
  avatar_url?: string | null;
}

export interface ThreadSummary {
  id: string;
  name: string;
  preview: string;
  // Readable label derived server-side (core/thread_labels.py): `title` has the
  // machine directives stripped and names a scheduler run by its task; origin /
  // channel / sender drive the row's provenance line. Optional so an older
  // backend still renders — the UI falls back to `preview`.
  title?: string;
  origin?: string;
  channel?: string;
  sender?: string;
  message_count: number;
  created_at?: string | null;
  updated_at?: string | null;
  // Cached per-thread token/cost (from the threadcost cache; absent until indexed).
  usd?: number | null;
  // Olano-managed spend accumulated by the thread (gateway charge, from the
  // thread state file — a different book from `usd`, never summed into it).
  credits?: number | null;
  total_tokens?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
}

export interface AccessMember extends User {
  role: string;
}

export interface AccessListResponse {
  owner: User | null;
  members: AccessMember[];
}

// GET /api/admin/access-overview — every agent's explicit grants in one call.
// Users are referenced by id against the flat `users` roster so the payload
// stays O(agents + users) instead of repeating a user object per agent.
// `admin_ids` are the *implicit* owners: platform admins own every agent by
// rule (see auth.can_admin_agent) and hold no agent_access row.
export interface AccessOverviewAgent {
  name: string;
  owners: string[];
  users: string[];
}

export interface AccessOverview {
  agents: AccessOverviewAgent[];
  admin_ids: string[];
  users: User[];
}

// A tool call the runner has paused on, awaiting approve/reject in plan/manual
// mode. `args` may arrive as an object or a pre-stringified payload.
export interface ApprovalAction {
  name?: string;
  interrupt_id?: string;
  args?: unknown;
  tool_input?: unknown;
  input?: unknown;
  // "low" | "medium" | "high" — risk tier computed server-side.
  risk?: string;
}

// A workspace file a tool saved this turn (screenshot, generated image, TTS
// clip, downloaded video) or one the agent's own reply named (a report it
// wrote, a CSV it read). Built server-side, one per file: `url` streams the
// bytes (a same-origin `/api/files-raw` path, served with the session cookie)
// and is what the chat renders inline; `files_url` deep-links the Files tab;
// `mentions` are the exact tokens in the reply that resolved to this file,
// which the Markdown renderer turns into links (see views/chat/fileLinks.ts).
export interface MediaRef {
  kind: "image" | "audio" | "video" | "document" | "file";
  name: string;
  /** Workspace-relative path. */
  path: string;
  mime: string;
  url: string;
  size?: number;
  /** `/files?path=<path>&agent=<agent>` — the Files tab, file previewed. */
  files_url?: string;
  /** `url` + `&download=1` — served as an attachment. */
  download_url?: string;
  /** Raw tokens in the reply text (relative, absolute or backticked) naming this file. */
  mentions?: string[];
}

// Every frame carries `seq`, monotonic from 1 within its run. It is the cursor
// a reconnecting client attaches with (`GET /api/chat/attach`), which is what
// makes "rejoin the turn I missed" exact rather than approximate.
export type ChatFrame = { seq?: number } & (
  // `turn_id` is the client's handle on this turn: `POST /api/chat/steer` names
  // it so a message typed against THIS reply is never handed to a later one.
  // `run_id` is its handle on the RUN — the detached server-side task that
  // keeps working when this socket dies (olano/core/runs.py).
  | { type: "start"; thread_id: string; agent: string; model: string; mode?: string; turn_id?: string; run_id?: string }
  | { type: "text"; text: string }
  // A message the user steered into the running turn, at the moment the model
  // read it (after the tool calls in flight, before its next step). `id` is
  // the steer id the chip was posted with; `sender` names who typed it.
  | { type: "steer"; id: string; text: string; sender?: string }
  | { type: "subagent_text"; text: string; namespace?: string[]; agents?: string[] }
  | { type: "tool_call"; id: string; name: string; args: string }
  | { type: "tool_result"; id: string; name: string; content: string; media?: MediaRef[] }
  // Files the assistant's own text named, resolved server-side once the text
  // is final — arrives once per turn, before `done`.
  | { type: "media"; role: "assistant"; items: MediaRef[] }
  | { type: "status"; kind: string; message: string }
  | {
      type: "usage";
      input_tokens?: number;
      output_tokens?: number;
      total_tokens?: number;
      // Live context-window fill (last sub-call's prompt size). Distinct from
      // input_tokens, which SUMS every sub-call across the turn (for billing).
      context_tokens?: number;
      usd?: number;
      // Olano-managed turns only: the gateway's authoritative charge for the
      // turn, in olano credits. A separate accounting system from `usd`.
      credits?: number;
      // Model sub-calls this turn made — a turn that ran three tool
      // round-trips reports three. Same definition as the Insights "Model
      // calls" stat, so the chat header and the dashboard agree.
      model_calls?: number;
    }
  | { type: "approval"; actions: ApprovalAction[] }
  | { type: "thread_switch"; old_thread_id: string; new_thread_id: string; reason: string }
  | { type: "error"; message: string; code?: string; remediation?: ErrorRemediation | null }
  // The run buffered past what this client's cursor can be served from — the
  // frames it missed have been evicted. Rendering a gap it cannot see would be
  // worse than refetching, so this asks for the transcript instead.
  | { type: "resync"; run_id: string }
  | { type: "done"; thread_id: string; turn_id?: string; trace_id?: string }
);

// Machine-readable remediation attached to an actionable error frame. `action`
// drives a UI call-to-action (e.g. reconnect a provider, open Providers).
// A remediation with NO `action` renders nothing at all — see ErrorRemediation
// — so a backend rule that wants to be seen in the dashboard must set one, even
// when its `kind` is "none" (kind drives the chat-channel tail, action drives
// the button; they are independent).
export interface ErrorRemediation {
  kind: string; // "reconnect_provider" | "set_api_key" | "top_up_billing" | "retry" | "none"
  summary: string;
  provider?: string | null;
  env_var?: string | null;
  url?: string | null;
  action?: {
    // "reconnect_provider" | "open_url" | "set_api_key" | "new_thread" | …
    type: string;
    provider?: string;
    auth_type?: string;
    env_var?: string | null;
    url?: string;
    label?: string;
  } | null;
}
