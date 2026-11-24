// Types for the Agents view. The list cards consume the shared `Agent` type
// from core/types; this file adds the richer shapes returned by
// GET /api/agents/{name} (full agent.json5 + resolved subagents) and the
// catalog endpoints that power the model picker, tool picker, and skills tab.

// ── Full agent config (GET /api/agents/{name}) ──────────────────────────────
// agent.json5 mirrored back, plus the display-only `subagents_resolved` view
// and the transient `_running` flag the server injects.

export interface MemoryConfig {
  markdown?: boolean;
  rag?: boolean;
  self_learning?: boolean;
  self_learning_interval?: number;
  [k: string]: unknown;
}

export interface CustomerServiceConfig {
  enabled?: boolean;
  prompt_extension?: string;
  // Per-user scheduling (Type-2 jobs): end-users may keep their own scheduled
  // tasks, identity-injected and capped. Mirrors CustomerServiceConfig in
  // olano/config/agent_config.py.
  user_scheduling?: boolean;
  max_jobs_per_user?: number;
  min_job_interval_minutes?: number;
  // Consent-based user↔user connections (invite → connect <CODE> → capped,
  // attributed relay) and per-user credentials (each user brings their own
  // token, fail-closed resolution). Mirrors CustomerServiceConfig.
  connections?: boolean;
  max_connections_per_user?: number;
  max_connection_sends_per_day?: number;
  connection_invite_ttl_hours?: number;
  connection_contact_info?: string;
  user_credentials?: boolean;
  customer_credential_names?: string[];
  [k: string]: unknown;
}

// message_policy — what this agent accepts from a sender and how much it says
// back. All fields off by default. Mirrors MessagePolicyConfig in
// olano/config/agent_config.py; the enforcement seam is
// olano/security/reply_policy.py.
export interface MessagePolicyConfig {
  max_input_chars?: number | null;
  on_oversize?: "reject" | "truncate" | "ignore";
  oversize_notice?: string;
  ignore_patterns?: string[];
  ignore_rules?: string[];
  ignore_notice?: string;
  max_output_tokens?: number | null;
  [k: string]: unknown;
}

export interface RateLimitWindows {
  per_minute?: number;
  per_hour?: number;
  per_day?: number;
}

// security.rate_limits — per-agent inbound message caps (opt-in; empty = unlimited),
// plus the pre-existing per-tool call cap. Mirrors backend SecurityConfig.rate_limits.
export interface RateLimitsConfig {
  messages?: RateLimitWindows;
  per_platform?: Record<string, RateLimitWindows>;
  per_tool?: Record<string, number>;
  requests_per_minute?: number;
  [k: string]: unknown;
}

export interface SecurityConfig {
  profile?: string;
  hitl_write_tools?: boolean;
  hitl_shell?: boolean;
  pii_scan_input?: boolean;
  pii_scan_output?: boolean;
  credential_scrub?: boolean;
  rate_limits?: RateLimitsConfig;
  [k: string]: unknown;
}

export interface BackendBlock {
  type?: string;
  access_mode?: string;
  root_dir?: string;
  virtual_mode?: boolean;
  [k: string]: unknown;
}

export interface SubagentEntry {
  name: string;
  ref?: string;
  emoji?: string;
  description?: string;
  system_prompt?: string;
  model?: string;
  tools?: string[];
  skills?: string[];
  from_builtin?: string;
  from_archetype?: string;
  // Display-only, always sent by GET /api/agents/{name} → subagents_resolved:
  // "inline" (hand-written in `subagents`), "archetype" (from_archetype stub),
  // or "builtin" (from `builtin_subagents`, e.g. OpsManager or a ToolkitAgent).
  source?: "inline" | "archetype" | "builtin";
  // Catalog-sourced rows only: the pristine base prompt (before overrides /
  // appended owner instructions) and the raw override set stored in
  // agent.json5 next to the ref — what the overrides editor prefills from.
  system_prompt_base?: string;
  system_prompt_extension?: string | null;
  overrides?: {
    model?: string;
    system_prompt?: string;
    system_prompt_extension?: string;
    tools?: string[];
    extra_tools?: string[];
    disabled_tools?: string[];
  };
  enabled?: boolean;
  backend?: BackendBlock | null;
  [k: string]: unknown;
}

export interface ChannelObject {
  type: string;
  [k: string]: unknown;
}
export type ChannelEntry = string | ChannelObject;

export interface McpServer {
  url: string;
  transport?: string;
  enabled?: boolean;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  disabled_tools?: string[];
  headers?: Record<string, string>;
  auth?: string; // "none" | "oauth"
  oauth_scope?: string;
  oauth_client_id?: string;
  oauth_client_secret?: string;
  [k: string]: unknown;
}

export interface McpToolStatus {
  name: string;
  description?: string;
  enabled?: boolean;
}

export interface McpLogEntry {
  ts?: number;
  level?: string; // "info" | "error" | "debug"
  message: string;
}

export interface McpServerStatus {
  url: string;
  transport?: string;
  enabled?: boolean;
  ok?: boolean | null;
  status?: string; // "connected" | "auth_required" | "error" | "disabled"
  message?: string;
  tools?: McpToolStatus[];
  logs?: McpLogEntry[];
  needs_oauth?: boolean;
  authorized?: boolean;
  // Expiry-aware token classification: "live" (unexpired), "refreshable"
  // (expired but auto-renews on next connect), "expired" (dead — no refresh
  // token; needs re-authorization), "none" (no tokens stored).
  token_state?: "none" | "live" | "refreshable" | "expired";
  token_expires_at?: number | null; // epoch seconds, leeway-adjusted
  auth?: string;
  error_code?: string; // structured failure code, e.g. "dcr_unsupported"
}

// POST /api/agents/{name}/mcp/oauth/start and .../poll/{id} both return this.
export interface McpOAuthSession {
  session_id: string;
  status: string; // starting|awaiting_authorization|exchanging|authorized|error|expired
  authorization_url?: string | null;
  message?: string;
  error?: string | null;
  error_code?: string; // structured failure code, e.g. "dcr_unsupported"
  tool_count?: number;
  logs?: McpLogEntry[];
  url?: string;
  transport?: string;
  redirect_uri?: string;
}

export interface ScheduleEntry {
  cron?: string;
  minute?: string;
  hour?: string;
  day?: string;
  month?: string;
  day_of_week?: string;
  message?: string;
  // Optional label + pause switch + zone, mirroring ScheduleConfig. The editor
  // writes `cron`/`timezone`/`name`/`enabled` and clears the split fields.
  name?: string;
  enabled?: boolean;
  timezone?: string;
  thread_id?: string;
  // Per-job overrides of the agent's model / reasoning effort (the same two
  // per-turn overrides the chat chips set). Absent = the agent's own.
  model?: string;
  reasoning_effort?: string;
  // Multi-user Type-1 fan-out: run once per customer folder — `action` names
  // an allowed_actions script (deterministic, no model turn) or `message`
  // runs one bound agent turn per user. Mirrors ScheduleConfig.
  for_each_customer?: boolean;
  action?: string;
}

// ── Chat slash-command exposure + custom commands (Commands tab) ────────────
export interface CustomCommand {
  name: string;
  prompt: string;
  description?: string;
}
export interface CommandsConfig {
  // canonical built-in name → exposed? (absent ⇒ use the catalog default)
  enabled?: Record<string, boolean>;
  custom?: CustomCommand[];
}

// ── Heartbeat poll settings (HEARTBEAT.md checks) ───────────────────────────
// Null/absent fields inherit the system-wide defaults (enabled, hourly).
export interface HeartbeatConfig {
  enabled?: boolean | null;
  interval_minutes?: number | null;
}

// agent.json5 `gmail_watch` (olano/config/agent_config.py:GmailWatchConfig).
// agent.json5 → observability.langfuse: every field null ⇒ follow Config.
export interface AgentLangfuseConfig {
  enabled?: boolean | null;
  capture?: string | null;
  sample_rate?: number | null;
}
export interface AgentObservabilityConfig {
  langfuse?: AgentLangfuseConfig;
}

export interface GmailWatchConfig {
  enabled?: boolean;
  query?: string;
  poll_minutes?: number;
  prompt?: string;
  max_per_check?: number;
}

// ── Per-agent Cortex engine overrides (agent.json5 `cognition`) ─────────────
// Mirrors olano/config/agent_config.py CognitionConfig. `enabled` is the
// master per-agent switch; the per-feature/autonomy/auto-approve/intensity
// fields are three-state — null/absent ⇒ inherit the home-wide default.
// Chat self-configuration overrides. Every field is three-state: null/absent ⇒
// inherit the deployment-wide agent_autonomy block (whose fields default ON),
// true/false ⇒ this agent overrides in that direction. Mirrors
// AgentConfig.autonomy / system_settings.AUTONOMY_CAPABILITIES.
export interface AutonomyConfig {
  explore_archetypes?: boolean | null;
  change_model?: boolean | null;
  modify_prompt?: boolean | null;
  restart_self?: boolean | null;
  add_subagents?: boolean | null;
  connect_mcps?: boolean | null;
  connect_platforms?: boolean | null;
  manage_skills?: boolean | null;
  magic_links?: boolean | null;
  external_access?: boolean | null;
}

export interface CognitionConfig {
  enabled?: boolean;
  autonomous?: boolean | null;
  auto_approve?: boolean | null;
  intensity?: string | null;
  // Per-feature overrides (null ⇒ inherit). Names mirror system_settings.FEATURES.
  self_learning?: boolean | null;
  brainstorming?: boolean | null;
  idea_generation?: boolean | null;
  prototyping?: boolean | null;
  skills_optimization?: boolean | null;
  workspace_optimization?: boolean | null;
  memory_optimization?: boolean | null;
  pulse?: boolean | null;
  [k: string]: unknown;
}

export interface AgentVisibility {
  mode: "none" | "all" | "list";
  agents: string[];
}

// A row in the "Olano Internal Tools" catalog (GET /api/agents/{name} →
// internal_tools) — every builder/deepagents-provided tool this agent can
// get without a `tools:` entry, with its live on/off state. Rendered as
// first-class "built-in" cards by ToolBrowser. See
// olano/config/internal_tools.py for the source of truth.
export interface InternalToolRow {
  name: string;
  category: string;
  on: boolean;
  note?: string;
  toggleable?: boolean;
  description?: string;
}

export interface AgentConfig {
  // Permanent handle — folder name, API path segment, talk_to_agent target.
  // Immutable server-side (_IMMUTABLE_AGENT_FIELDS); render agentLabel() instead.
  name: string;
  // Editable display name. Empty falls back to `name`.
  display_name?: string;
  // The position ("Recruitment Specialist"). A READ-ONLY mirror the detail
  // endpoint injects from `org.title` (central organization.yaml first): the
  // server refuses it on PUT /api/agents/{name} — edit it through
  // saveAgentTitle() (PUT …/org) so the central store stays canonical.
  title?: string;
  emoji?: string;
  model?: string;
  // Reasoning effort for models with effort control (from the model's
  // catalog metadata); null/absent → platform default for the model.
  reasoning_effort?: string | null;
  description?: string;
  system_prompt?: string;
  tools?: string[];
  // Explicit opt-outs from the auto-injected universal/ops default tools (e.g.
  // ["talk_to_agent"] to revoke peer messaging).
  disabled_tools?: string[];
  // Which OTHER agents this agent is told exist (prompt awareness).
  agent_visibility?: AgentVisibility;
  commands?: CommandsConfig;
  // Display-only effective toolset (universal + ops defaults + deepagents
  // built-ins) from GET /api/agents/{name}. `tools` holds only extras; this is
  // what the agent actually runs with, used by the chat `/tools` listing.
  tools_resolved?: string[];
  // Full "Olano Internal Tools" catalog, categorized with live on/off state —
  // everything builder-injected (memory/graph/notes/workspace_context) or
  // deepagents-provided (task/write_todos/filesystem primitives), which never
  // appears in `tools` above. From GET /api/agents/{name}.
  internal_tools?: InternalToolRow[];
  skills?: string[];
  // Skill NAMES this agent must not load — the Skills tab's per-skill switches.
  // A denylist, because skills arrive by directory (the shared managed pack),
  // so there is no per-agent allowlist to write instead.
  disabled_skills?: string[];
  subagents?: SubagentEntry[];
  subagents_resolved?: SubagentEntry[];
  channels?: ChannelEntry[];
  mcp_servers?: McpServer[];
  // Inbound exposure: may systems OUTSIDE the deployment reach this agent
  // (MCP tool / A2A card)? `description` is what they see; required when on.
  external_access?: { mcp?: boolean; a2a?: boolean; description?: string };
  schedule?: ScheduleEntry[];
  memory?: MemoryConfig;
  customer_service?: CustomerServiceConfig;
  message_policy?: MessagePolicyConfig;
  // Multi-user action allow-list: action name → script path (absolute, or
  // relative to the workspace / agent dir / a skill root). The only things
  // execute_action can run. Mirrors AgentConfig.allowed_actions.
  allowed_actions?: Record<string, string>;
  heartbeat?: HeartbeatConfig;
  gmail_watch?: GmailWatchConfig;
  observability?: AgentObservabilityConfig;
  cognition?: CognitionConfig;
  autonomy?: AutonomyConfig;
  security?: SecurityConfig;
  monetization?: MonetizationConfig;
  backend?: BackendBlock | string;
  // Access / safety flags (bespoke controls in Overview → Access & Safety).
  human_only?: boolean;
  fleet_access?: boolean;
  hidden?: boolean;
  default_verbosity?: "quiet" | "normal" | "verbose";
  // Runtime limits (bespoke number controls in Overview → Access & Safety).
  iteration_budget?: number;
  truncation_retry_limit?: number;
  tool_error_retry_limit?: number;
  color?: string | null;
  avatar_url?: string | null;
  // Resolved per-pose sprite URLs (GET /api/agents/{name}) once a character has
  // been generated. Map of pose name → image URL.
  sprites?: Record<string, string> | null;
  _running?: boolean;
  [k: string]: unknown;
}

// ── Monetization (end-user subscription pricing; the Monetization tab) ──────
// Mirrors olano/config/agent_config.py MonetizationPlan / MonetizationConfig.
// Secrets (STRIPE_API_KEY, STRIPE_WEBHOOK_SECRET) live in the vault, not here.
export interface MonetizationPlan {
  price_cents?: number;
  currency?: string;
  interval?: "month" | "year";
  stripe_price_id?: string;
  stars_amount?: number;
  product_name?: string;
  offer_copy?: string;
  success_url?: string;
  cancel_url?: string;
}

export interface MonetizationConfig {
  enabled?: boolean;
  providers?: ("stripe" | "telegram_stars")[];
  trial_days?: number;
  plan?: MonetizationPlan;
  channels?: string[];
  gate_groups?: boolean;
}

// ── Catalog (model picker, tool picker, skills tab) ─────────────────────────
// Per-model metadata from /api/catalog/providers: reasoning-effort control
// (probed live from langchain model profiles) plus capabilities/pricing/
// context from the synced cloud catalog. Fields are absent for models the
// catalog doesn't know.
export interface ModelMeta {
  reasoning_efforts?: string[];
  default_effort?: string | null;
  display_name?: string | null;
  description?: string | null;
  tier?: string | null; // 'flagship' | 'standard' | 'fast' | 'local'
  recommended?: boolean;
  default_for_provider?: boolean;
  deprecated?: boolean;
  eol_date?: string | null;
  context_window?: number | null;
  max_output_tokens?: number | null;
  capabilities?: {
    vision?: boolean;
    audio_input?: boolean;
    image_gen?: boolean;
    tts?: boolean;
    stt?: boolean;
    reasoning?: boolean;
  };
  // USD per 1M tokens; null/absent = unknown (e.g. subscription-billed Codex).
  pricing?: {
    input?: number | null;
    output?: number | null;
    cache_read?: number | null;
    cache_write?: number | null;
  } | null;
}

export interface ProviderEntry {
  key: string;
  name: string;
  models: string[];
  // Per-model capability metadata (bare model name → meta). A model appears
  // here only when it has something to say (reasoning-effort control and/or
  // cloud-catalog data); `default_effort` null means "unset leaves the
  // model's thinking behavior alone".
  model_meta?: Record<string, ModelMeta>;
  // Availability signals from /api/catalog/providers, used to gate the chat
  // model picker to providers the user has actually authenticated.
  available?: boolean; // provider package installed
  env_var?: string | null; // API-key env var (null = no key needed)
  env_set?: boolean; // key present in env or vault
  // Providers that authenticate by something other than an env key (Codex
  // OAuth, Olano's per-deployment gateway token). `connected` is capability —
  // "usable on this box at all" — and is what `providerUsable()` gates the
  // model pickers on. `status` is live health, so a provider can be
  // connected AND degraded: attached to the control plane but not currently
  // holding a working credential. Rendering degraded as connected is what let
  // a total managed-inference outage sit behind a green tick.
  auth?: {
    connected?: boolean;
    status?: "connected" | "degraded" | "not_connected";
    error?: string | null;
  } | null;
  notes?: string | null;
  docs_url?: string | null; // provider docs (from the cloud catalog)
  console_url?: string | null; // where to create an API key
}
// A `default:<tier>` deployment alias: the picker value, its bare tier id, and
// the concrete "provider:model" the backend currently resolves it to (from
// config.yaml's default_models block, or the box's built-in target).
export interface ModelAliasEntry {
  value: string;
  tier: string;
  target: string | null;
}

export interface ProvidersResponse {
  providers: ProviderEntry[];
  // Deployment-default model aliases (default:turbo/pro/max) with their
  // current targets. Present in both full and basic payloads.
  model_aliases?: ModelAliasEntry[];
  // Cloud-catalog provenance: bundled/disk/cloud + the synced revision.
  catalog?: { revision?: number; source?: string | null };
}

// A single required env var for a tool/bundle. `in_env`/`in_vault`/`in_connector`
// say whether the secret is already resolvable; `optional` marks non-blocking ones.
export interface ToolEnvVar {
  name: string;
  optional?: boolean;
  in_env?: boolean;
  in_vault?: boolean;
  /** Supplied live by a connected OAuth connector (resolves above vault/env). */
  in_connector?: boolean;
}
export interface ToolRequirements {
  env_vars?: ToolEnvVar[];
  packages?: string[];
  npm_packages?: string[];
  setup_commands?: string[];
  cli?: string[];
  guidance?: string;
  [k: string]: unknown;
}
export interface ToolEntry {
  name: string;
  description?: string;
  category?: string;
  bundles?: string[];
  requirements?: ToolRequirements;
  disabled?: boolean;
  // Fleet-scoped: acts across the whole deployment (shared logs, other agents'
  // sessions and workspaces, the vault, cross-agent exec) rather than only for
  // the agent holding it. Server-computed from the toolkit deny-list; the
  // picker badges it so granting one is a deliberate act, never a stray click.
  privileged?: boolean;
}
export interface ToolBundle {
  name: string;
  tools: string[];
  requirements?: ToolRequirements;
  // Every member is fleet-scoped (see ToolEntry.privileged) — the toolkit
  // header badges it so "Add all in toolkit" is not the stray click.
  privileged?: boolean;
}
export interface ToolsResponse {
  tools: ToolEntry[];
  bundles?: ToolBundle[];
  env_status?: Record<string, unknown>;
  // Tool names every agent gets auto-injected (universal + document defaults).
  // Unchecking one records the removal in the agent's `disabled_tools`.
  agent_defaults?: string[];
}

// Where a skill lives, which is also what decides whether the dashboard offers
// to edit it. `managed` is the common pack under <home>/skills that every agent
// on the box shares — read-only here, and unreachable from the Files view,
// which is why the skill viewer has its own read endpoints.
export type SkillScope = "managed" | "workspace" | "learned" | "configured";

export interface SkillEntry {
  name?: string;
  description?: string;
  location?: string;
  category?: string;
  // "light" (injected into the parent prompt) or "subagent" (run on demand
  // through delegate_task).
  tier?: string;
  trigger?: string | null;
  // Why the skill breaks the Agent Skills naming rule, if it does. Reported,
  // never enforced — the skill still loads.
  spec_error?: string | null;
  scope?: SkillScope | null;
  editable?: boolean;
  // Workspace-relative folder path — the Files view deep link. Null for a skill
  // that lives outside the workspace.
  workspace_path?: string | null;
  // Whether this agent loads the skill. False when its name is in the agent's
  // `disabled_skills`; the skill stays on disk and stays listed. Always true in
  // the unscoped listing, which has no agent to ask.
  enabled?: boolean;
  // Why the agent never sees this skill regardless of the switch: it declares
  // `requires_tools` and the agent lacks some of them ("needs tools: …").
  // Null when not gated or in the unscoped listing. A switch cannot fix it;
  // granting the tools can.
  gated?: string | null;
  requires_tools?: string[];
}
export interface SkillsResponse {
  skills: SkillEntry[];
  // The agent's raw `disabled_skills` field. May still name a skill that is no
  // longer installed, so the toggle writes this list back rather than rebuilding
  // it from the rows.
  disabled_skills?: string[];
}

// ── Skill detail (GET /api/catalog/skills/detail) ───────────────────────────
export interface SkillFileEntry {
  name: string;
  path: string;
  type: "file" | "directory";
  size: number;
  modified: string;
  // Which viewer to use. Absent on directories.
  kind?: "markdown" | "text" | "image" | "binary";
}
export interface SkillFileContent {
  path: string;
  name: string;
  size: number;
  kind: "markdown" | "text" | "image" | "binary";
  mime?: string | null;
  truncated: boolean;
  // Null for images and binaries — their bytes come from the file-raw endpoint.
  content: string | null;
}
export interface SkillDetail {
  skill: SkillEntry & { directory?: string; source_dir?: string };
  files: SkillFileEntry[];
  // The folder hit the walk's entry/depth cap, so the listing is partial.
  truncated: boolean;
  content: SkillFileContent;
}

// ── Connector diagnostics (GET /api/agents/{name}/connectors/health) ────────
export interface ConnectorCheck {
  id?: string;
  ok?: boolean | null;
  message?: string;
}
export interface ConnectorSnapshot {
  live?: boolean;
  bot_user?: string;
  checks?: ConnectorCheck[];
  [k: string]: unknown;
}
export interface ConnectorRow {
  type: string;
  runtime_attached?: boolean;
  snapshot?: ConnectorSnapshot;
}
export interface ConnectorHealth {
  connectors: ConnectorRow[];
  agent_booted: boolean;
  error?: string | null;
}

// Friendly shorthand the Environment dropdown reads/writes.
export type EnvShorthand = "filesystem+shell" | "filesystem" | "read" | "state" | "store";

// Detail tab identifiers.
export type DetailTab =
  | "overview"
  | "external"
  | "browser"
  | "subagents"
  | "tools"
  | "connectors"
  | "mcp"
  | "webhooks"
  | "commands"
  | "monetization"
  | "multiuser"
  | "skills"
  | "org"
  | "pages"
  | "variables"
  | "advanced"
  | "raw";

// ── Agent-scoped vault overrides (GET /api/agents/{name}/vault/secrets) ──────
// The Variables tab: this agent's per-agent override rows plus name-level
// metadata for the common (shared) keys it can override. Values never appear
// in the listing — reveal is a separate call.
export interface AgentVaultSecret {
  name: string;
  agent: string;
  description: string;
  created_at: string | null;
  updated_at: string | null;
  expires_at: string | null;
  last_used: string | null;
}
export interface AgentVaultCommonKey {
  name: string;
  category: string;
  description: string;
}
export interface AgentVaultListResponse {
  unlocked: boolean;
  secrets: AgentVaultSecret[];
  common: AgentVaultCommonKey[];
}

// ── Schema-driven Advanced config editor (GET /api/agents/{name}/config/schema)
// The backend hands us the AgentConfig JSON Schema plus which fields to hide
// (read_only) and which already have a dedicated tab (managed → tab id). The
// Advanced panel renders a control for every remaining field, so new
// AgentConfig fields become editable automatically.
export interface JsonSchemaProp {
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  anyOf?: JsonSchemaProp[];
  allOf?: JsonSchemaProp[];
  items?: JsonSchemaProp;
  $ref?: string;
  properties?: Record<string, JsonSchemaProp>;
  additionalProperties?: boolean | JsonSchemaProp;
  [k: string]: unknown;
}
export interface JsonSchema {
  properties?: Record<string, JsonSchemaProp>;
  $defs?: Record<string, JsonSchemaProp>;
  [k: string]: unknown;
}
export interface AgentConfigSchemaResponse {
  schema: JsonSchema;
  read_only: string[];
  managed: Record<string, string>;
}

// ── Command catalog (GET /api/catalog/commands) ─────────────────────────────
export interface CommandCatalogEntry {
  name: string;
  description: string;
  group: string;
  sensitive: boolean;
  default_exposed: boolean;
  aliases: string[];
}
export interface CommandCatalogResponse {
  groups: { id: string; label: string }[];
  commands: CommandCatalogEntry[];
}

// Lifecycle verbs the start/stop/restart buttons POST.
export type Lifecycle = "start" | "stop" | "restart";
