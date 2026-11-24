import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { api, ApiError } from "../../core/api";
import { useAuth } from "../../core/auth";
import { useT } from "../../core/i18n";
import { BrandWordmark } from "../../ui/BrandMark";
import { useToast } from "../../ui/toast";
import { ThemePicker } from "../../ui/ThemePicker";
import { useCatalog, modelOptions, modelValueLabel, type ModelOption } from "../agents/catalog";
import { useModelOptionText } from "../agents/ModelOptions";
import { Toggle, KvRow } from "../agents/Toggle";
import { Modal } from "../agents/Modal";
import { useUnsavedChanges } from "../../core/unsavedChanges";
import { MagicLinksSection } from "../connections/MagicLinksSection";
import { ExternalAccessAdminPanel } from "../external/ExternalAccessAdminPanel";
import { refreshCostDisplay } from "../../core/costDisplay";
import { refreshCapabilities } from "../../core/capabilities";
import type { Agent } from "../../core/types";
import type { CatalogChannelsResponse } from "../agents/connectorCatalog";
import { STR } from "./strings";
import { settingAnchor } from "../../core/settingAnchor";
import { Section } from "./Section";
import { DiskEncryptionCard } from "./DiskEncryptionCard";
import { VoicePreviewButton } from "./VoicePreviewButton";
import {
  BrowserConfigSection,
  browserFormFrom,
  browserFormValid,
  browserPayload,
  type BrowserCfg,
  type BrowserForm,
  type BrowserStatusCfg,
} from "../browser/BrowserConfigSection";
import {
  ObservabilityConfigSection,
  observabilityFormFrom,
  observabilityFormValid,
  observabilityPayload,
  type ObservabilityCfg,
  type ObservabilityForm,
} from "../observability/ObservabilityConfigSection";

// Per-window inbound message caps (int | undefined ⇒ unlimited for that window).
interface RlWindows {
  per_minute?: number;
  per_hour?: number;
  per_day?: number;
}
interface RateLimits {
  messages: RlWindows;
  per_platform: Record<string, RlWindows>;
}

// One direction of the speech block (speech.stt / speech.tts in config.yaml).
interface SpeechSvc {
  provider: string; // disabled | olano | openai | groq | qwen | custom
  model: string;
  url: string;
  api_key_name: string;
  voice: string;
  audio_format: string; // TTS only; "" = per-provider auto
}
const EMPTY_SPEECH: SpeechSvc = {
  provider: "disabled",
  model: "",
  url: "",
  api_key_name: "",
  voice: "",
  audio_format: "",
};
// Containers offerable for speech.tts.audio_format. Only Ogg/Opus renders as a
// native voice bubble on the chat platforms, which is why "" (auto) picks it
// wherever the provider supports it — see olano/speech.py.
const TTS_AUDIO_FORMATS = ["opus", "mp3", "wav", "flac", "aac", "m4a", "pcm"];

// Per-provider model/voice options for the Config → Speech dropdowns, served by
// GET /api/system/config. A provider missing from a section (custom, or qwen
// under stt) has no curated options → the UI renders a free-text input instead.
interface SpeechCatalogEntry {
  models: string[];
  voices?: string[];
}
interface SpeechCatalog {
  stt: Record<string, SpeechCatalogEntry>;
  tts: Record<string, SpeechCatalogEntry>;
}
// One source choice for the Config → "Media & search" dropdowns, computed
// server-side by olano.media_catalog.media_catalog() (GET /api/system/config).
// `value` is exactly what the resolver reads back: "olano" (the managed
// service), a "provider:model" BYOK pin, or a web-search sentinel
// (tavily/disabled). `olano` flags the managed option so the UI can group it
// separately. The list already reflects live entitlement + key presence, so the
// UI renders it verbatim — it never re-derives availability.
interface MediaOption {
  value: string;
  label: string;
  olano?: boolean;
}
interface MediaCatalog {
  vision: MediaOption[];
  image: MediaOption[];
  web_search: MediaOption[];
}
// Disk monitor block (config.yaml → disk_monitor) + the action catalog the
// API sends alongside it (name/description/safe flag/host availability).
interface DiskCleanupCfg {
  enabled: boolean;
  actions: string[];
  cooldown_minutes: number;
  force_interval_days: number;
  journald_vacuum_size: string;
}
interface DiskMonitorCfg {
  enabled: boolean;
  path: string;
  check_interval_minutes: number;
  warn_percent: number;
  cleanup_percent: number;
  cleanup: DiskCleanupCfg;
}
interface DiskActionInfo {
  name: string;
  description: string;
  safe: boolean;
  selected: boolean;
  available: boolean;
  reason: string | null;
}

// MCP connection upkeep (config.yaml → mcp_health) — the background sweep that
// renews stored OAuth grants before they expire and reconnects MCP servers that
// dropped out. Applies live; the loop re-reads the block every tick.
interface McpHealthCfg {
  enabled: boolean;
  check_interval_minutes: number;
  refresh_tokens: boolean;
  refresh_window_minutes: number;
  auto_reconnect: boolean;
}
// How much conversation two agents carry into each talk_to_agent exchange
// (config.yaml → peer_threads). Read at every peer call; applies live.
interface PeerThreadsCfg {
  rotate: boolean;
  max_age_days: number;
  max_turns: number;
}
// The checkpoint history sweep (config.yaml → checkpoint_gc). The loop re-reads
// the block on every pass; Postgres only.
interface CheckpointGcCfg {
  enabled: boolean;
  interval_hours: number;
  boot_delay_minutes: number;
  keep_min: number;
  max_minutes: number;
}
// The Olano mobile app's layout (config.yaml → mobile_app). Phones read it from
// GET /api/mobile/config at launch and on every return to the foreground.
interface MobileAppCfg {
  cortex_tab: boolean;
  inbox: { enabled: boolean; approvals: boolean; alerts: boolean; issues: boolean };
  settings_tab: { files: boolean; credits_usage: boolean; transcribe_on_phone: boolean };
  transcribe_on_phone_default: boolean;
}
// Deployed pages (config.yaml → pages): the page supervisor that builds and
// runs pages published with pages_deploy, and the proxy in front of their
// servers. Re-read by the supervisor every few seconds, so it applies live.
interface PagesCfg {
  services_enabled: boolean;
  max_services_per_agent: number;
  health_interval_seconds: number;
  request_timeout_seconds: number;
  max_request_body_mb: number;
  max_concurrent_requests: number;
}
// Context-window policy (config.yaml → context_window): what a compaction
// keeps and when it fires, plus the MCP result offload cap. Read at agent
// build, so a change reaches an agent on its next restart / reload; each
// agent.json5 may override any field.
interface ContextWindowCfg {
  keep_fraction: number;
  keep_tokens: number;
  trigger_fraction: number;
  trigger_tokens: number;
  mcp_result_token_limit: number;
}
// External access (config.yaml → external_access): the inbound MCP endpoint,
// the per-agent A2A endpoints and Olano's own OAuth authorization server, all
// for callers OUTSIDE the deployment. Both surfaces default off.
interface ExternalAccessCfg {
  mcp: { enabled: boolean };
  a2a: { enabled: boolean };
  require_https: boolean;
  token_ttl_days: number;
  a2a_task_retention_days: number;
  allowed_hosts: string[];
  allowed_origins: string[];
  oauth: {
    enabled: boolean;
    dynamic_registration: boolean;
    access_token_minutes: number;
    refresh_token_days: number;
  };
  jwt: { issuer: string; jwks_url: string; audience: string; required_scopes: string[] };
}
// Resource ceilings for the Docker sandbox each agent's shell runs in
// (config.yaml → sandbox). `memory`/`cpus` are the writable fields; null means
// automatic (a fraction of the host). Everything else is read-only context the
// card needs to explain itself: how big the box is, what the limits actually
// resolve to and which layer decided each, and whether an environment variable
// is overriding the field on screen.
interface SandboxCfg {
  memory: string | null;
  cpus: number | null;
  host: { ncpu: number | null; mem_bytes: number | null; source: string };
  effective: {
    memory: string;
    cpus: number;
    memory_source: string;
    cpus_source: string;
  };
  env_overrides: { memory: boolean; cpus: boolean };
}

// Compact read-only summary of the last sweep (GET /api/diagnostics/mcp), shown
// above the knobs so the card answers "is anything broken right now?" and not
// only "what would happen if it were".
interface McpDiagnosticsServer {
  connected: boolean;
  enabled: boolean;
}
interface McpDiagnosticsAgent {
  servers: McpDiagnosticsServer[];
}
interface McpDiagnostics {
  checked_at: number | null;
  agents: Record<string, McpDiagnosticsAgent>;
}

// Olano model-gateway enrollment (GET /api/diagnostics/gateway). Read-only.
//
// This card exists because every other way to see this state was a shell
// command — `olano models gateway` — on a managed box where the operator has
// no shell. A customer whose olano: models were failing had no way at all to
// find out why, which turned a config problem into a support conversation.
interface GatewayDiagnostics {
  state: string; // enrolled | unenrolled | unattached
  gateway_url: string;
  consecutive_failures: number;
  last_error: string | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  retry_in_seconds: number | null;
  env_key_revoked?: boolean;
  unenrolled_model_builds?: number;
  remediation: string | null;
  // Which managed groups the gateway actually serves, from the cached probe.
  // Holding a key is not the same as the gateway routing what we ask it for: a
  // box entitled to managed vision whose gateway serves no `olano-vision` group
  // fails every image_describe call while looking perfectly healthy here.
  served?: {
    ok: boolean;
    served: string[];
    expected: string[];
    missing: string[];
    error: string | null;
    checked_at: string;
  } | null;
}

// Magic links (config.yaml → magic_links; legacy key `handshake`) — the
// short-lived, PIN-protected pages agents hand to chat-only users. `available`
// and `unavailable_reason` are read-only: they fold in the feature switch AND
// whether this deployment has a browser-reachable public origin at all, so a
// deployment can be `enabled` yet still unable to mint a link.
// Owner-login sub-block (magic_links.owner_login): the flow that signs the
// owner into the dashboard from a chat magic link. session_hours is clamped
// 1–720 server-side.
interface OwnerLoginCfg {
  enabled: boolean;
  auto_bind: boolean;
  session_hours: number;
}
interface MagicLinksCfg {
  enabled: boolean;
  ttl_minutes: number;
  pin: string; // required | off
  max_pin_attempts: number;
  allow_groups: boolean;
  owner_login: OwnerLoginCfg;
  available: boolean;
  unavailable_reason: string;
}

// The block under whichever key this server emits. A runtime older than the
// rename sends only `handshake`, so reading both keeps the form correct
// against a dashboard that outran its backend. Saves always write
// `magic_links`.
const magicLinksCfg = (c: SystemConfig | null): MagicLinksCfg | undefined =>
  c ? (c.magic_links ?? c.handshake) : undefined;

// Mirror of GET /api/system/config — the global config.yaml settings.
// Cost display unit (config.yaml → credits.display) + the cloud-published
// olano-credit rate the dashboard converts with. Only `display` is writable —
// the rate arrives from the catalog sync, so `source` tells the UI whether the
// cloud actually confirmed it or the bundled launch rate is standing in.
interface CreditsCfg {
  display: string; // "usd" | "credits"
  // Managed deployments: the unit is signed cloud policy — render read-only.
  display_locked?: boolean;
  credits_per_usd: number;
  unit_name: string;
  unit_name_plural: string;
  decimals: number;
  source: string; // cloud | disk | cache | bundled
  revision: number;
}

interface SystemConfig {
  default_agent: string;
  preferred_model: string | null;
  runtime_api_port: number;
  dashboard_port: number;
  multi_user_enabled: boolean;
  highlights_all_agents: boolean;
  playground_enabled: boolean;
  // Member bot creation (config.yaml self_serve_agents:) — whether non-admin
  // members may create their own agent, how many they may own, and which
  // archetypes a self-serve create may use (empty = any).
  self_serve_agents: { enabled: boolean; max_per_user: number; allowed_archetypes: string[] };
  // Chat self-configuration fleet defaults (config.yaml agent_autonomy:) —
  // what agents may change about themselves from a conversation. Per-agent
  // Autonomy settings override these per capability.
  agent_autonomy: Record<string, boolean>;
  // Multi-bot group threads (config.yaml groups:). `enabled` draws the whole
  // group surface in the simple shell; `autonomous` is the separate switch
  // that lets bots answer each other, bounded by max_hops + the timeout.
  groups?: {
    enabled: boolean;
    min_members: number;
    max_members: number;
    autonomous: boolean;
    max_hops: number;
    cascade_timeout_seconds: number;
  };
  extra_bind_roots: string[];
  archetype_dirs: string[];
  // Deployment-wide outbound-destination lists for the agents' SSRF-gated
  // tools (config.yaml → egress_allow / egress_deny). Bare hosts; empty means
  // every public destination. Absent on a backend older than the field.
  egress_allow?: string[];
  egress_deny?: string[];
  fast_model: string;
  deep_model: string | null;
  // Per alias tier: the operator's explicit target ("" = inherit), what the
  // alias currently resolves to, and this box's built-in fallback.
  default_models?: Record<string, { value: string; resolved: string; builtin: string }>;
  rate_limits: RateLimits;
  speech: { stt: SpeechSvc; tts: SpeechSvc };
  speech_providers: string[];
  speech_catalog?: SpeechCatalog;
  // The customer's per-feature source pins ("" = auto). vision/image hold
  // "olano" or a "provider:model"; web_search holds "" | olano | tavily |
  // disabled. `media_catalog` is the live option list for each.
  media: { vision: string; image: string };
  web_search: { provider: string };
  media_catalog?: MediaCatalog;
  disk_monitor: DiskMonitorCfg;
  disk_cleanup_actions: DiskActionInfo[];
  usage_budgets?: UsageBudgetsCfg;
  magic_links: MagicLinksCfg;
  /** Legacy alias still emitted by the API; read only as a fallback. */
  handshake?: MagicLinksCfg;
  mcp_health: McpHealthCfg;
  peer_threads?: PeerThreadsCfg;
  checkpoint_gc?: CheckpointGcCfg;
  mobile_app?: MobileAppCfg;
  pages?: PagesCfg;
  browser?: BrowserCfg;
  browser_status?: BrowserStatusCfg;
  observability?: ObservabilityCfg;
  context_window?: ContextWindowCfg;
  external_access?: ExternalAccessCfg;
  sandbox: SandboxCfg;
  credits: CreditsCfg;
  cost_display_modes: string[];
  config_path: string;
  restart_required: string[];
}

// Usage budgets (config.yaml → usage_budgets), over the deployment's signed
// ceilings. Each row is one budget as the runtime enforces it: `limit` is what
// bites, `deployment_limit` is what the plan said, `source` says which won, and
// `override` is what this box stored — null (inherit), a number, or the string
// "unlimited". Those three are genuinely different states and the form keeps
// them apart. `overridable` is false for the two cost budgets: they bound
// olano's own upstream bill and are signed, not local.
interface UsageBudgetRow {
  key: string;
  label: string;
  unit: "tokens" | "micro_usd";
  window: string;
  limit: number | null;
  deployment_limit: number | null;
  source: "deployment" | "config";
  overridable: boolean;
  override: number | "unlimited" | null;
  current: number | null;
}
interface UsageBudgetsCfg {
  enforced: boolean;
  posture: string;
  alerts_enabled: boolean;
  alert_percents: number[];
  default_alert_percents: number[];
  limits: UsageBudgetRow[];
}

// Windows held as strings for the inputs; "" = unset ⇒ unlimited.
type RlWinForm = { per_minute: string; per_hour: string; per_day: string };
const EMPTY_WIN: RlWinForm = { per_minute: "", per_hour: "", per_day: "" };
const RL_WINDOW_KEYS: Array<keyof RlWinForm> = ["per_minute", "per_hour", "per_day"];

// Editable mirror — numbers and lists are held as strings for the inputs and
// coerced back on save (ports → int, textareas → string[]).
interface Form extends BrowserForm, ObservabilityForm {
  default_agent: string;
  preferred_model: string;
  fast_model: string;
  deep_model: string;
  dfm_turbo: string;
  dfm_pro: string;
  dfm_max: string;
  runtime_api_port: string;
  dashboard_port: string;
  multi_user_enabled: boolean;
  highlights_all_agents: boolean;
  playground_enabled: boolean;
  ssa_enabled: boolean;
  ssa_max: string;
  ssa_archetypes: string;
  agent_autonomy: Record<string, boolean>;
  grp_enabled: boolean;
  grp_min: string;
  grp_max: string;
  grp_autonomous: boolean;
  grp_hops: string;
  grp_timeout: string;
  extra_bind_roots: string;
  archetype_dirs: string;
  eg_allow: string;
  eg_deny: string;
  rl_messages: RlWinForm;
  rl_platform: Record<string, RlWinForm>;
  speech_stt: SpeechSvc;
  speech_tts: SpeechSvc;
  media_vision: string;
  media_image: string;
  web_search_provider: string;
  dm_enabled: boolean;
  dm_path: string;
  dm_interval: string;
  dm_warn: string;
  dm_cleanup_pct: string;
  dm_cleanup_enabled: boolean;
  dm_actions: Record<string, boolean>;
  dm_cooldown: string;
  dm_force_days: string;
  dm_vacuum: string;
  ml_enabled: boolean;
  ml_ttl: string;
  ml_pin: string;
  ml_attempts: string;
  ml_allow_groups: boolean;
  ml_ol_enabled: boolean;
  ml_ol_auto_bind: boolean;
  ml_ol_session_hours: string;
  mh_enabled: boolean;
  mh_interval: string;
  mh_refresh_tokens: boolean;
  mh_window: string;
  mh_auto_reconnect: boolean;
  pt_rotate: boolean;
  pt_days: string;
  pt_turns: string;
  gc_enabled: boolean;
  gc_interval: string;
  gc_keep_min: string;
  ma_cortex: boolean;
  ma_inbox: boolean;
  ma_inbox_approvals: boolean;
  ma_inbox_alerts: boolean;
  ma_inbox_issues: boolean;
  ma_files: boolean;
  ma_credits: boolean;
  ma_transcribe_row: boolean;
  ma_transcribe_default: boolean;
  pg_enabled: boolean;
  pg_max_services: string;
  pg_health: string;
  pg_timeout: string;
  pg_body: string;
  pg_concurrency: string;
  cw_keep_fraction: string;
  cw_keep_tokens: string;
  cw_trigger_fraction: string;
  cw_trigger_tokens: string;
  cw_mcp_limit: string;
  ea_mcp: boolean;
  ea_a2a: boolean;
  ea_require_https: boolean;
  ea_ttl: string;
  ea_a2a_retention: string;
  ea_oauth_enabled: boolean;
  ea_oauth_dcr: boolean;
  ea_oauth_access_minutes: string;
  ea_oauth_refresh_days: string;
  ea_jwt_issuer: string;
  ea_jwt_jwks: string;
  ea_jwt_audience: string;
  // "" = automatic (a fraction of the host), matching the API's null.
  sb_memory: string;
  sb_cpus: string;
}

const splitLines = (s: string): string[] =>
  s.split("\n").map((l) => l.trim()).filter(Boolean);

const winToForm = (w?: RlWindows): RlWinForm => ({
  per_minute: w?.per_minute != null ? String(w.per_minute) : "",
  per_hour: w?.per_hour != null ? String(w.per_hour) : "",
  per_day: w?.per_day != null ? String(w.per_day) : "",
});

// Coerce a form window map to ints, omitting empty / non-positive entries.
function winFromForm(f: RlWinForm): RlWindows {
  const out: RlWindows = {};
  for (const key of RL_WINDOW_KEYS) {
    const raw = f[key].trim();
    const n = parseInt(raw, 10);
    if (raw !== "" && Number.isFinite(n) && n > 0) out[key] = n;
  }
  return out;
}

function toForm(c: SystemConfig): Form {
  return {
    default_agent: c.default_agent,
    preferred_model: c.preferred_model ?? "",
    fast_model: c.fast_model,
    deep_model: c.deep_model ?? "",
    dfm_turbo: c.default_models?.turbo?.value ?? "",
    dfm_pro: c.default_models?.pro?.value ?? "",
    dfm_max: c.default_models?.max?.value ?? "",
    runtime_api_port: String(c.runtime_api_port),
    dashboard_port: String(c.dashboard_port),
    multi_user_enabled: c.multi_user_enabled,
    highlights_all_agents: c.highlights_all_agents,
    playground_enabled: c.playground_enabled,
    // Fallbacks match the backend defaults (on, 3, any) so a payload from an
    // older runtime that predates the block renders truthfully.
    ssa_enabled: c.self_serve_agents?.enabled ?? true,
    ssa_max: String(c.self_serve_agents?.max_per_user ?? 3),
    ssa_archetypes: (c.self_serve_agents?.allowed_archetypes ?? []).join("\n"),
    // Whatever capabilities the backend reports (the server owns the list);
    // an older runtime without the block renders no rows rather than lying.
    agent_autonomy: { ...(c.agent_autonomy ?? {}) },
    // Fallbacks match the current backend defaults — surface on, cascade on.
    // They only ever apply to a payload from a runtime that predates the
    // block, which cannot cascade at all; the form still has to show
    // something, and today's default is the least surprising thing to show.
    grp_enabled: c.groups?.enabled ?? true,
    grp_min: String(c.groups?.min_members ?? 2),
    grp_max: String(c.groups?.max_members ?? 6),
    grp_autonomous: c.groups?.autonomous ?? true,
    grp_hops: String(c.groups?.max_hops ?? 3),
    grp_timeout: String(c.groups?.cascade_timeout_seconds ?? 180),
    extra_bind_roots: c.extra_bind_roots.join("\n"),
    archetype_dirs: c.archetype_dirs.join("\n"),
    eg_allow: (c.egress_allow ?? []).join("\n"),
    eg_deny: (c.egress_deny ?? []).join("\n"),
    rl_messages: winToForm(c.rate_limits?.messages),
    rl_platform: Object.fromEntries(
      Object.entries(c.rate_limits?.per_platform || {}).map(([k, v]) => [k, winToForm(v)]),
    ),
    speech_stt: { ...EMPTY_SPEECH, ...(c.speech?.stt ?? {}) },
    speech_tts: { ...EMPTY_SPEECH, ...(c.speech?.tts ?? {}) },
    media_vision: c.media?.vision ?? "",
    media_image: c.media?.image ?? "",
    web_search_provider: c.web_search?.provider ?? "",
    dm_enabled: c.disk_monitor?.enabled ?? true,
    dm_path: c.disk_monitor?.path ?? "/",
    dm_interval: String(c.disk_monitor?.check_interval_minutes ?? 30),
    dm_warn: String(c.disk_monitor?.warn_percent ?? 80),
    dm_cleanup_pct: String(c.disk_monitor?.cleanup_percent ?? 85),
    dm_cleanup_enabled: c.disk_monitor?.cleanup?.enabled ?? true,
    dm_actions: Object.fromEntries(
      (c.disk_cleanup_actions ?? []).map((a) => [
        a.name,
        (c.disk_monitor?.cleanup?.actions ?? []).includes(a.name),
      ]),
    ),
    dm_cooldown: String(c.disk_monitor?.cleanup?.cooldown_minutes ?? 360),
    dm_force_days: String(c.disk_monitor?.cleanup?.force_interval_days ?? 0),
    dm_vacuum: c.disk_monitor?.cleanup?.journald_vacuum_size ?? "200M",
    ml_enabled: magicLinksCfg(c)?.enabled ?? true,
    ml_ttl: String(magicLinksCfg(c)?.ttl_minutes ?? 15),
    // Anything but an explicit "off" means PIN-protected — same bias as the
    // backend's validator, so a malformed config never reads as the laxer one.
    ml_pin: magicLinksCfg(c)?.pin === "off" ? "off" : "required",
    ml_attempts: String(magicLinksCfg(c)?.max_pin_attempts ?? 5),
    ml_allow_groups: magicLinksCfg(c)?.allow_groups ?? false,
    ml_ol_enabled: magicLinksCfg(c)?.owner_login?.enabled ?? false,
    ml_ol_auto_bind: magicLinksCfg(c)?.owner_login?.auto_bind ?? false,
    ml_ol_session_hours: String(magicLinksCfg(c)?.owner_login?.session_hours ?? 24),
    ...browserFormFrom(c.browser),
    ...observabilityFormFrom(c.observability),
    mh_enabled: c.mcp_health?.enabled ?? true,
    mh_interval: String(c.mcp_health?.check_interval_minutes ?? 10),
    mh_refresh_tokens: c.mcp_health?.refresh_tokens ?? true,
    mh_window: String(c.mcp_health?.refresh_window_minutes ?? 20),
    mh_auto_reconnect: c.mcp_health?.auto_reconnect ?? true,
    pt_rotate: c.peer_threads?.rotate ?? true,
    pt_days: String(c.peer_threads?.max_age_days ?? 3),
    pt_turns: String(c.peer_threads?.max_turns ?? 200),
    gc_enabled: c.checkpoint_gc?.enabled ?? true,
    gc_interval: String(c.checkpoint_gc?.interval_hours ?? 6),
    gc_keep_min: String(c.checkpoint_gc?.keep_min ?? 3),
    ma_cortex: c.mobile_app?.cortex_tab ?? true,
    ma_inbox: c.mobile_app?.inbox?.enabled ?? true,
    ma_inbox_approvals: c.mobile_app?.inbox?.approvals ?? true,
    ma_inbox_alerts: c.mobile_app?.inbox?.alerts ?? true,
    ma_inbox_issues: c.mobile_app?.inbox?.issues ?? true,
    ma_files: c.mobile_app?.settings_tab?.files ?? true,
    ma_credits: c.mobile_app?.settings_tab?.credits_usage ?? true,
    ma_transcribe_row: c.mobile_app?.settings_tab?.transcribe_on_phone ?? true,
    ma_transcribe_default: c.mobile_app?.transcribe_on_phone_default ?? false,
    pg_enabled: c.pages?.services_enabled ?? true,
    pg_max_services: String(c.pages?.max_services_per_agent ?? 5),
    pg_health: String(c.pages?.health_interval_seconds ?? 30),
    pg_timeout: String(c.pages?.request_timeout_seconds ?? 120),
    pg_body: String(c.pages?.max_request_body_mb ?? 25),
    pg_concurrency: String(c.pages?.max_concurrent_requests ?? 64),
    cw_keep_fraction: String(c.context_window?.keep_fraction ?? 0.35),
    cw_keep_tokens: String(c.context_window?.keep_tokens ?? 40000),
    cw_trigger_fraction: String(c.context_window?.trigger_fraction ?? 0.85),
    cw_trigger_tokens: String(c.context_window?.trigger_tokens ?? 170000),
    cw_mcp_limit: String(c.context_window?.mcp_result_token_limit ?? 5000),
    ea_mcp: c.external_access?.mcp?.enabled ?? false,
    ea_a2a: c.external_access?.a2a?.enabled ?? false,
    ea_require_https: c.external_access?.require_https ?? true,
    ea_ttl: String(c.external_access?.token_ttl_days ?? 90),
    ea_a2a_retention: String(c.external_access?.a2a_task_retention_days ?? 7),
    ea_oauth_enabled: c.external_access?.oauth?.enabled ?? true,
    ea_oauth_dcr: c.external_access?.oauth?.dynamic_registration ?? true,
    ea_oauth_access_minutes: String(c.external_access?.oauth?.access_token_minutes ?? 60),
    ea_oauth_refresh_days: String(c.external_access?.oauth?.refresh_token_days ?? 30),
    ea_jwt_issuer: c.external_access?.jwt?.issuer ?? "",
    ea_jwt_jwks: c.external_access?.jwt?.jwks_url ?? "",
    ea_jwt_audience: c.external_access?.jwt?.audience ?? "",
    sb_memory: c.sandbox?.memory ?? "",
    sb_cpus: c.sandbox?.cpus != null ? String(c.sandbox.cpus) : "",
  };
}

function validPort(s: string): boolean {
  const n = Number(s);
  return Number.isInteger(n) && n >= 1 && n <= 65535;
}

// A rate-limit window input is valid when empty (unset) or a positive integer.
const validWindow = (s: string): boolean => {
  const t = s.trim();
  return t === "" || (/^\d+$/.test(t) && parseInt(t, 10) > 0);
};
function rateLimitsValid(form: Form): boolean {
  const all = [
    ...RL_WINDOW_KEYS.map((k) => form.rl_messages[k]),
    ...Object.values(form.rl_platform).flatMap((w) => RL_WINDOW_KEYS.map((k) => w[k])),
  ];
  return all.every(validWindow);
}

// The custom provider needs an endpoint URL; everything else is optional.
const speechValid = (form: Form): boolean =>
  [form.speech_stt, form.speech_tts].every(
    (s) => s.provider !== "custom" || s.url.trim() !== "",
  );

// Disk monitor validation — mirrors the server's PUT checks so the Save button
// disables (with red borders) instead of round-tripping a 400.
const intIn = (s: string, lo: number, hi: number): boolean => {
  const t = s.trim();
  if (!/^\d+$/.test(t)) return false;
  const n = parseInt(t, 10);
  return n >= lo && n <= hi;
};
const validVacuum = (s: string): boolean => /^\d+[KMGT]?$/.test(s.trim());
const diskThresholdsOrdered = (form: Form): boolean =>
  !intIn(form.dm_warn, 50, 99) ||
  !intIn(form.dm_cleanup_pct, 50, 99) ||
  parseInt(form.dm_cleanup_pct, 10) >= parseInt(form.dm_warn, 10);
// Roster bounds and the two cascade limits, mirroring GroupThreadSettings'
// validators. The min<=max rule is the one worth catching in the UI: the
// server resolves an inverted pair by raising max, which is a reasonable
// repair for a hand-edit and a confusing one for someone watching the field
// they just typed in change by itself.
function groupsValid(form: Form): boolean {
  return (
    intIn(form.grp_min, 2, 6) &&
    intIn(form.grp_max, 2, 6) &&
    parseInt(form.grp_max, 10) >= parseInt(form.grp_min, 10) &&
    intIn(form.grp_hops, 1, 10) &&
    intIn(form.grp_timeout, 10, 1800)
  );
}

function diskValid(form: Form): boolean {
  return (
    form.dm_path.trim() !== "" &&
    intIn(form.dm_interval, 5, 1440) &&
    intIn(form.dm_warn, 50, 99) &&
    intIn(form.dm_cleanup_pct, 50, 99) &&
    parseInt(form.dm_cleanup_pct, 10) >= parseInt(form.dm_warn, 10) &&
    intIn(form.dm_cooldown, 30, 10080) &&
    intIn(form.dm_force_days, 0, 90) &&
    validVacuum(form.dm_vacuum)
  );
}

// Magic-link validation — mirrors MagicLinkSettings' clamps (ttl 2…1440,
// attempts 1…10, owner-login session 1…720) so the Save button disables
// instead of silently persisting a coerced value the user didn't type.
const magicLinksValid = (form: Form): boolean =>
  intIn(form.ml_ttl, 2, 1440) &&
  intIn(form.ml_attempts, 1, 10) &&
  intIn(form.ml_ol_session_hours, 1, 720);

// MCP health validation — mirrors McpHealthSettings' clamps plus the server's
// ordering rule. A refresh window shorter than the tick would let a token be
// first *noticed* after it already lapsed, which is the gap the sweep exists to
// close, so the PUT rejects it and the Save button disables here first.
const mcpWindowOrdered = (form: Form): boolean =>
  !intIn(form.mh_interval, 1, 1440) ||
  !intIn(form.mh_window, 1, 720) ||
  parseInt(form.mh_window, 10) >= parseInt(form.mh_interval, 10);
function eaIntIn(raw: string, lo: number, hi: number): boolean {
  const n = parseInt(raw, 10);
  return /^\d+$/.test(raw.trim()) && n >= lo && n <= hi;
}

function externalAccessValid(f: Form): boolean {
  const jwtHalf = (f.ea_jwt_issuer.trim() === "") !== (f.ea_jwt_jwks.trim() === "");
  return (
    eaIntIn(f.ea_ttl, 0, 3650) &&
    eaIntIn(f.ea_a2a_retention, 1, 3650) &&
    eaIntIn(f.ea_oauth_access_minutes, 5, 1440) &&
    eaIntIn(f.ea_oauth_refresh_days, 1, 365) &&
    !jwtHalf
  );
}

// Agent-to-agent conversation bounds: mirrors PeerThreadSettings' clamps.
const peerThreadsValid = (form: Form): boolean =>
  intIn(form.pt_days, 1, 365) && intIn(form.pt_turns, 10, 100000);
// Checkpoint history sweep: mirrors CheckpointGcSettings' clamps.
const checkpointGcValid = (form: Form): boolean =>
  intIn(form.gc_interval, 1, 168) && intIn(form.gc_keep_min, 1, 1000);

// Deployed pages: mirrors PagesSettings' clamps (and the PUT's own check).
const pagesValid = (form: Form): boolean =>
  intIn(form.pg_max_services, 1, 50) &&
  intIn(form.pg_health, 5, 3600) &&
  intIn(form.pg_timeout, 5, 3600) &&
  intIn(form.pg_body, 1, 1024) &&
  intIn(form.pg_concurrency, 1, 1024);

const mcpHealthValid = (form: Form): boolean =>
  intIn(form.mh_interval, 1, 1440) && intIn(form.mh_window, 1, 720) && mcpWindowOrdered(form);

// Context-window policy — mirrors the backend validators (fractions 0.05–0.8
// keep / 0.3–0.98 trigger, token floors, and keep strictly below trigger on
// both axes, since a keep at or above its trigger compacts nothing and then
// compacts again on the next turn).
const fracIn = (s: string, lo: number, hi: number): boolean => {
  const t = s.trim();
  if (!/^\d*\.?\d+$/.test(t)) return false;
  const n = parseFloat(t);
  return n >= lo && n <= hi;
};
const cwKeepFractionOk = (f: Form): boolean => fracIn(f.cw_keep_fraction, 0.05, 0.8);
const cwTriggerFractionOk = (f: Form): boolean => fracIn(f.cw_trigger_fraction, 0.3, 0.98);
const cwKeepTokensOk = (f: Form): boolean => intIn(f.cw_keep_tokens, 2000, 2000000);
const cwTriggerTokensOk = (f: Form): boolean => intIn(f.cw_trigger_tokens, 8000, 4000000);
const cwMcpLimitOk = (f: Form): boolean => intIn(f.cw_mcp_limit, 0, 200000);
const cwFractionsOrdered = (f: Form): boolean =>
  !cwKeepFractionOk(f) ||
  !cwTriggerFractionOk(f) ||
  parseFloat(f.cw_keep_fraction) < parseFloat(f.cw_trigger_fraction);
const cwTokensOrdered = (f: Form): boolean =>
  !cwKeepTokensOk(f) ||
  !cwTriggerTokensOk(f) ||
  parseInt(f.cw_keep_tokens, 10) < parseInt(f.cw_trigger_tokens, 10);
const contextWindowValid = (f: Form): boolean =>
  cwKeepFractionOk(f) &&
  cwTriggerFractionOk(f) &&
  cwKeepTokensOk(f) &&
  cwTriggerTokensOk(f) &&
  cwMcpLimitOk(f) &&
  cwFractionsOrdered(f) &&
  cwTokensOrdered(f);

// Sandbox limit validation — mirrors the backend validators exactly (docker's
// own size grammar and its 6 MiB / 0.1-core floors), so the Save button
// disables here instead of the PUT 400ing. "" is always valid: it means
// automatic, and clearing a field is how an operator goes back to it.
const SANDBOX_MEMORY_RE = /^\d+[bkmgBKMG]?$/;
const validSandboxMemory = (raw: string): boolean => {
  const text = raw.trim().toLowerCase();
  if (!text) return true;
  if (!SANDBOX_MEMORY_RE.test(text)) return false;
  const unit = text.slice(-1);
  const mult: Record<string, number> = { b: 1, k: 1024, m: 1024 ** 2, g: 1024 ** 3 };
  const bytes = unit in mult ? parseInt(text, 10) * mult[unit] : parseInt(text, 10);
  return bytes >= 6 * 1024 ** 2;
};
const validSandboxCpus = (raw: string): boolean => {
  const text = raw.trim();
  if (!text) return true;
  const cpus = Number(text);
  return Number.isFinite(cpus) && cpus >= 0.1 && cpus <= 64;
};
const sandboxValid = (form: Form): boolean =>
  validSandboxMemory(form.sb_memory) && validSandboxCpus(form.sb_cpus);

// ── small layout helpers (local — AdminView's are not exported) ───────────────
// `Section`, the card wrapper every block on this page uses, lives in
// ./Section.tsx so the self-contained cards kept in their own files
// (DiskEncryptionCard) can share it without importing this module.

// ── Cloud catalogs (GET /api/catalog/status + POST /api/catalog/refresh) ────
// Read-only status per synced catalog plus the one action an operator needs
// without SSH: force a sync now (e.g. right after enabling a new official
// connector in the admin console, instead of waiting for the hourly poll).

interface CatalogStatusEntry {
  revision?: number;
  source?: string;
  fetched_at?: number | null;
  official_count?: number;
  [key: string]: unknown;
}

interface CatalogStatusResponse {
  enabled: boolean;
  manifest_poll_minutes: number;
  catalogs: Record<string, CatalogStatusEntry>;
}

interface CatalogRefreshReport {
  catalogs?: Record<string, { status?: string }>;
}

function CatalogSyncCard() {
  const t = useT(STR);
  const toast = useToast();
  const [status, setStatus] = useState<CatalogStatusResponse | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const load = useCallback(() => {
    api
      .get<CatalogStatusResponse>("/api/catalog/status")
      .then((s) => {
        setStatus(s);
        setLoadFailed(false);
      })
      .catch(() => setLoadFailed(true));
  }, []);

  useEffect(load, [load]);

  async function syncNow() {
    setSyncing(true);
    try {
      const report = await api.post<CatalogRefreshReport>("/api/catalog/refresh", {});
      const entries = Object.values(report.catalogs ?? {});
      const updated = entries.filter(
        (e) => e.status === "updated" || e.status === "rebuilt",
      ).length;
      toast.push(
        t("catalogs.toast.synced", {
          updated: String(updated),
          total: String(entries.length),
        }),
      );
      load();
    } catch (err) {
      toast.push(
        t("catalogs.toast.failed", {
          detail: err instanceof ApiError ? err.detail : String(err),
        }),
        "error",
      );
    } finally {
      setSyncing(false);
    }
  }

  return (
    <Section
      settingId="config.catalogs"
      title={t("section.catalogs")}
      hint={t("section.catalogs.hint")}
      right={
        <button
          className="btn"
          style={{ whiteSpace: "nowrap" }}
          onClick={() => void syncNow()}
          disabled={syncing}
        >
          {syncing ? t("catalogs.syncing") : t("catalogs.syncNow")}
        </button>
      }
    >
      {loadFailed ? (
        <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
          {t("catalogs.loadFailed")}
        </p>
      ) : status === null ? (
        <div className="spinner" />
      ) : (
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          {Object.entries(status.catalogs).map(([name, entry]) => (
            <span
              key={name}
              className="badge"
              style={{ fontFamily: "var(--font-mono)", fontSize: 11, padding: "3px 8px" }}
              title={
                entry.fetched_at
                  ? t("catalogs.fetchedAt", {
                      when: new Date(Number(entry.fetched_at) * 1000).toLocaleString(),
                    })
                  : t("catalogs.neverFetched")
              }
            >
              {/* Catalog ids are runtime data — deliberately untranslated. */}
              {name}
              {typeof entry.revision === "number" ? ` r${entry.revision}` : ""}
              {entry.source ? ` · ${entry.source}` : ""}
              {typeof entry.official_count === "number" ? ` · ${entry.official_count}` : ""}
            </span>
          ))}
        </div>
      )}
    </Section>
  );
}

// ── Usage budgets (config.yaml → usage_budgets) ────────────────────────────
// Self-contained like CatalogSyncCard: it owns its edit state and PUTs only
// its own key, because it is the one card whose fields are per-row rather than
// per-field and the page-wide form has no shape for that.
//
// This card exists because these ceilings used to be invisible. The only place
// they rendered was an admin card left unmounted on the grounds that plan
// limits were "practically unlimited" — so when one did bite, the first anyone
// heard of it was an agent refusing every turn, with no way to see the number
// or move it.

function fmtBudget(value: number | null, unit: string): string {
  if (value === null) return "∞";
  return unit === "micro_usd" ? `$${(value / 1_000_000).toLocaleString()}` : value.toLocaleString();
}

function budgetPct(row: UsageBudgetRow): number | null {
  if (row.limit === null || row.limit === 0 || row.current === null) return null;
  return Math.min(100, Math.floor((row.current / row.limit) * 100));
}

function UsageBudgetsCard({ cfg, onSaved }: { cfg: UsageBudgetsCfg; onSaved: () => void }) {
  const t = useT(STR);
  const toast = useToast();
  // Draft per overridable row, as typed: "" = inherit the deployment ceiling,
  // "unlimited" = no ceiling, digits = that ceiling.
  const initial = useCallback(
    () =>
      Object.fromEntries(
        cfg.limits
          .filter((r) => r.overridable)
          .map((r) => [r.key, r.override === null ? "" : String(r.override)]),
      ),
    [cfg.limits],
  );
  const [draft, setDraft] = useState<Record<string, string>>(initial);
  const [alerts, setAlerts] = useState(cfg.alerts_enabled);
  const [percents, setPercents] = useState(cfg.alert_percents.join(", "));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(initial());
    setAlerts(cfg.alerts_enabled);
    setPercents(cfg.alert_percents.join(", "));
  }, [initial, cfg.alerts_enabled, cfg.alert_percents]);

  const validEntry = (v: string) =>
    v.trim() === "" || v.trim().toLowerCase() === "unlimited" || /^\d[\d_,]*$/.test(v.trim());
  const parsedPercents = percents
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const percentsValid = parsedPercents.every((p) => /^\d+$/.test(p) && +p >= 1 && +p <= 99);
  const rowsValid = Object.values(draft).every(validEntry);

  const dirty =
    JSON.stringify(draft) !== JSON.stringify(initial()) ||
    alerts !== cfg.alerts_enabled ||
    percents !== cfg.alert_percents.join(", ");

  async function save() {
    setSaving(true);
    const budgets: Record<string, unknown> = {
      alerts_enabled: alerts,
      alert_percents: parsedPercents.map(Number),
    };
    for (const [key, raw] of Object.entries(draft)) {
      const v = raw.trim();
      // "" clears the override; the backend keeps inherit / number / unlimited
      // apart on exactly these three shapes.
      budgets[key] = v === "" ? null : /^\d[\d_,]*$/.test(v) ? Number(v.replace(/[_,]/g, "")) : v;
    }
    try {
      await api.put<SystemConfig>("/api/system/config", { usage_budgets: budgets });
      toast.push(t("toast.saved"));
      onSaved();
    } catch (err) {
      toast.push(err instanceof ApiError ? err.detail : t("toast.saveFailed"), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Section
      settingId="config.budgets"
      title={t("section.budgets")}
      hint={t("section.budgets.hint")}
      right={
        dirty ? (
          <button className="btn primary" disabled={saving || !rowsValid || !percentsValid} onClick={() => void save()}>
            {saving ? t("btn.saving") : t("btn.save")}
          </button>
        ) : undefined
      }
    >
      {!cfg.enforced && (
        <p className="muted" style={{ margin: "0 0 12px", fontSize: 12.5 }}>
          {t("budgets.unmanaged")}
        </p>
      )}
      {/* A wrapping flex list rather than a table: the limit input is this
          card's whole point, and in a three-column table it lands off-screen
          on a phone behind a horizontal scroll. Here it simply wraps onto its
          own line. */}
      <div>
        {cfg.limits.map((row) => {
          const pct = budgetPct(row);
          return (
            <div
              key={row.key}
              className="row"
              style={{
                justifyContent: "space-between",
                alignItems: "center",
                gap: 12,
                flexWrap: "wrap",
                padding: "10px 0",
                borderTop: "1px solid var(--border)",
              }}
            >
              <div style={{ flex: "1 1 220px", minWidth: 0 }}>
                <div style={{ fontSize: 13 }}>{row.label}</div>
                <div className="muted" style={{ fontSize: 11.5 }}>
                  {t(`budgets.window.${row.window}`)}
                  {row.source === "config" &&
                    ` · ${t("budgets.overridden", { plan: fmtBudget(row.deployment_limit, row.unit) })}`}
                </div>
              </div>
              <div className="row" style={{ gap: 10, alignItems: "center", flex: "0 1 auto" }}>
                <span style={{ whiteSpace: "nowrap", fontSize: 12.5 }}>
                  <span className="muted">{t("budgets.col.used")} </span>
                  <span style={pct !== null && pct >= 90 ? { color: "var(--warn)" } : undefined}>
                    {row.current === null ? "—" : fmtBudget(row.current, row.unit)}
                  </span>
                  {pct !== null && (
                    <span className="muted" style={{ marginLeft: 5 }}>
                      ({pct}%)
                    </span>
                  )}
                </span>
                {row.overridable ? (
                  <input
                    className="input"
                    style={{
                      width: 190,
                      ...(validEntry(draft[row.key] ?? "") ? {} : { borderColor: "var(--danger)" }),
                    }}
                    value={draft[row.key] ?? ""}
                    aria-label={row.label}
                    placeholder={t("budgets.placeholder", { plan: fmtBudget(row.deployment_limit, row.unit) })}
                    onChange={(e) => setDraft((d) => ({ ...d, [row.key]: e.target.value }))}
                  />
                ) : (
                  <span className="muted" style={{ fontSize: 12.5, whiteSpace: "nowrap" }}>
                    {fmtBudget(row.limit, row.unit)} · {t("budgets.signed")}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
      <p className="muted" style={{ margin: "10px 0 0", fontSize: 12.5 }}>
        {t("budgets.help")}
      </p>

      <div style={{ marginTop: 14, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
        <KvRow
          settingId="config.budgets.alerts"
          label={t("field.budgetAlerts")}
          hint={t("field.budgetAlerts.hint")}
        >
          <Toggle checked={alerts} onChange={setAlerts} />
        </KvRow>
        <Field
          settingId="config.budgets.percents"
          label={t("field.budgetPercents")}
          hint={t("field.budgetPercents.hint")}
          style={{ marginTop: 12, maxWidth: 420 }}
        >
          <input
            className="input"
            value={percents}
            disabled={!alerts}
            onChange={(e) => setPercents(e.target.value)}
            style={percentsValid ? undefined : { borderColor: "var(--danger)" }}
          />
        </Field>
      </div>
    </Section>
  );
}

function Field({
  label,
  hint,
  children,
  style,
  settingId,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  style?: CSSProperties;
  settingId?: string;
}) {
  return (
    <div className="admin-field" style={style} {...settingAnchor(settingId)}>
      <label className="admin-field-label">
        {label}
        {hint && <span className="admin-field-hint">{hint}</span>}
      </label>
      {children}
    </div>
  );
}

// Model picker mirroring the agent-edit dropdown: catalog "provider:model"
// options, a saved value outside the catalog kept as its own option, plus an
// explicit blank option since blank is meaningful here (default / inherit).
// Olano tiers render with their one-line description (useModelOptionText), the
// same copy the agent-edit dropdowns show.
function ModelSelect({
  value,
  blankLabel,
  models,
  onChange,
}: {
  value: string;
  blankLabel: string;
  models: ModelOption[];
  onChange: (v: string) => void;
}) {
  const text = useModelOptionText();
  return (
    <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{blankLabel}</option>
      {value && !models.some((m) => m.value === value) && (
        <option value={value}>{modelValueLabel(value)}</option>
      )}
      {models.map((m) => (
        <option key={m.value} value={m.value}>
          {text(m)}
        </option>
      ))}
    </select>
  );
}

// Source picker for a media feature (vision / image). Options come pre-labeled
// from media_catalog(); we only split the managed "Olano" option from the BYOK
// ones into two optgroups (mirroring the agent-edit dropdown's Olano / "Your
// providers" grouping) and prepend an explicit blank = Auto. A saved value the
// catalog no longer offers (a rotated-out key, a legacy env pin) is kept as its
// own option so selecting it isn't silently lost.
function MediaSelect({
  value,
  blankLabel,
  olanoGroupLabel,
  byokGroupLabel,
  options,
  onChange,
}: {
  value: string;
  blankLabel: string;
  olanoGroupLabel: string;
  byokGroupLabel: string;
  options: MediaOption[];
  onChange: (v: string) => void;
}) {
  const olanoOpts = options.filter((o) => o.olano);
  const byokOpts = options.filter((o) => !o.olano);
  const known = value === "" || options.some((o) => o.value === value);
  return (
    <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{blankLabel}</option>
      {!known && <option value={value}>{value}</option>}
      {olanoOpts.length > 0 && (
        <optgroup label={olanoGroupLabel}>
          {olanoOpts.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </optgroup>
      )}
      {byokOpts.length > 0 && (
        <optgroup label={byokGroupLabel}>
          {byokOpts.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </optgroup>
      )}
    </select>
  );
}

export function ConfigView() {
  const t = useT(STR);
  const toast = useToast();
  const { user } = useAuth();
  const cat = useCatalog();
  const [config, setConfig] = useState<SystemConfig | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [saving, setSaving] = useState(false);
  const [showRlPlatforms, setShowRlPlatforms] = useState(false);
  const [platformList, setPlatformList] = useState<string[]>([]);
  const [mcpDiag, setMcpDiag] = useState<McpDiagnostics | null>(null);
  const [gwDiag, setGwDiag] = useState<GatewayDiagnostics | null>(null);

  const load = useCallback(() => {
    api
      .get<SystemConfig>("/api/system/config")
      .then((c) => {
        setConfig(c);
        setForm(toForm(c));
      })
      .catch((err) =>
        toast.push(
          err instanceof ApiError && err.status === 403 ? t("toast.notAdmin") : t("toast.loadFailed"),
          "error",
        ),
      );
    api.cachedGet<Agent[]>("/api/agents").then(setAgents).catch(() => setAgents([]));
    // Platform namespace for per-platform overrides: bare enum values (strip the
    // `platform:` catalog prefix) + the dashboard/REST scope.
    api
      .get<CatalogChannelsResponse>("/api/catalog/channels")
      .then((r) =>
        setPlatformList(
          Array.from(
            new Set([
              ...r.channels.map((c) => c.type.replace(/^platform:/, "").toLowerCase()),
              "dashboard",
            ]),
          ).sort(),
        ),
      )
      .catch(() => setPlatformList(["dashboard"]));
    // Last MCP health sweep — informational only, and absent on a runtime that
    // hasn't ticked yet, so a failure just leaves the summary line out.
    api
      .get<McpDiagnostics>("/api/diagnostics/mcp")
      .then(setMcpDiag)
      .catch(() => setMcpDiag(null));
    // Gateway enrollment state. Absent on an unmanaged install (no cloud
    // channel at all), so a failure just hides the card rather than erroring.
    api
      .get<GatewayDiagnostics>("/api/diagnostics/gateway")
      .then(setGwDiag)
      .catch(() => setGwDiag(null));
  }, [toast, t]);
  useEffect(load, [load]);

  const set = useCallback(<K extends keyof Form>(key: K, value: Form[K]) => {
    setForm((f) => (f ? { ...f, [key]: value } : f));
  }, []);

  // Rate-limit setters (nested Form state).
  const setRlMessage = useCallback((key: keyof RlWinForm, value: string) => {
    setForm((f) => (f ? { ...f, rl_messages: { ...f.rl_messages, [key]: value } } : f));
  }, []);
  const setRlPlatform = useCallback((plat: string, key: keyof RlWinForm, value: string) => {
    setForm((f) =>
      f
        ? {
            ...f,
            rl_platform: {
              ...f.rl_platform,
              [plat]: { ...(f.rl_platform[plat] ?? EMPTY_WIN), [key]: value },
            },
          }
        : f,
    );
  }, []);
  const setSpeech = useCallback(
    (which: "speech_stt" | "speech_tts", key: keyof SpeechSvc, value: string) => {
      setForm((f) => {
        if (!f) return f;
        // Changing the provider invalidates every provider-specific refinement:
        // model ids and voices differ per provider, and a leftover custom
        // endpoint URL / api_key_name would otherwise leak onto the next
        // provider (the backend now honors url for built-ins too). Reset to a
        // clean slate so the new provider starts on its own defaults.
        const next =
          key === "provider"
            ? { ...EMPTY_SPEECH, provider: value }
            : { ...f[which], [key]: value };
        return { ...f, [which]: next };
      });
    },
    [],
  );
  const setDiskAction = useCallback((name: string, checked: boolean) => {
    setForm((f) => (f ? { ...f, dm_actions: { ...f.dm_actions, [name]: checked } } : f));
  }, []);
  const addRlPlatform = useCallback((plat: string) => {
    setForm((f) =>
      f && plat && !f.rl_platform[plat]
        ? { ...f, rl_platform: { ...f.rl_platform, [plat]: { ...EMPTY_WIN } } }
        : f,
    );
  }, []);
  const removeRlPlatform = useCallback((plat: string) => {
    setForm((f) => {
      if (!f) return f;
      const next = { ...f.rl_platform };
      delete next[plat];
      return { ...f, rl_platform: next };
    });
  }, []);

  const dirty = useMemo(
    () => !!config && !!form && JSON.stringify(form) !== JSON.stringify(toForm(config)),
    [config, form],
  );

  // One-line readout of the last MCP health sweep. Null (no line rendered) when
  // the sweep hasn't run yet, is switched off, or no agent has MCP servers —
  // "0 servers" would read as a problem where there is simply nothing to watch.
  const mcpSummary = useMemo(() => {
    if (!mcpDiag?.checked_at) return null;
    const servers = Object.values(mcpDiag.agents ?? {}).flatMap((a) => a.servers ?? []);
    const watched = servers.filter((s) => s.enabled);
    if (watched.length === 0) return null;
    const connected = watched.filter((s) => s.connected).length;
    const minutes = Math.max(0, Math.round(Date.now() / 1000 - mcpDiag.checked_at) / 60);
    return t("mcpHealth.lastCheck", {
      minutes: String(Math.round(minutes)),
      connected: String(connected),
      total: String(watched.length),
    });
  }, [mcpDiag, t]);

  // One-line readout of what the sandbox limits currently come out as. The
  // fields below are usually empty (automatic), so without this the card would
  // show two blanks and no way to tell what agents are actually running with.
  const sandboxSummary = useMemo(() => {
    const sb = config?.sandbox;
    if (!sb?.effective) return null;
    const gb = sb.host?.mem_bytes ? sb.host.mem_bytes / 1024 ** 3 : null;
    return t("sandbox.summary", {
      hostCpus: sb.host?.ncpu != null ? String(sb.host.ncpu) : "?",
      hostMem: gb != null ? `${gb.toFixed(1)} GB` : "?",
      cpus: String(sb.effective.cpus),
      memory: sb.effective.memory,
      source: sb.effective.memory_source,
    });
  }, [config, t]);

  const portsValid = !!form && validPort(form.runtime_api_port) && validPort(form.dashboard_port);
  // Mirrors the server's 1..100 clamp so the Save button disables (with a red
  // border) instead of silently persisting a clamped-away value.
  const ssaValid = !!form && intIn(form.ssa_max, 1, 100);
  // Mirrors the server's clamps (roster 2..6, hops 1..10, timeout 10..1800)
  // and its min<=max rule, so an impossible roster disables Save with a red
  // border rather than being silently corrected on the way to disk.
  const grpValid = !!form && groupsValid(form);
  const rlValid = !!form && rateLimitsValid(form);
  const spValid = !!form && speechValid(form);
  const dkValid = !!form && diskValid(form);
  const mlValid = !!form && magicLinksValid(form);
  const mhValid = !!form && mcpHealthValid(form);
  const pgValid = !!form && pagesValid(form);
  const ptValid = !!form && peerThreadsValid(form);
  const gcValid = !!form && checkpointGcValid(form);
  const brValid = !!form && browserFormValid(form);
  const obValid = !!form && observabilityFormValid(form);
  const cwValid = !!form && contextWindowValid(form);
  const eaValid = !!form && externalAccessValid(form);
  const sbValid = !!form && sandboxValid(form);
  // Every per-section validity flag, without the dirty/saving gates: the leave
  // dialog needs to know whether "Save and leave" is possible at all, so its
  // body can say "fix the red fields" rather than offer a button that does
  // nothing.
  const formValid =
    portsValid &&
    ssaValid &&
    grpValid &&
    rlValid &&
    spValid &&
    dkValid &&
    mlValid &&
    mhValid &&
    ptValid &&
    gcValid &&
    pgValid &&
    brValid &&
    obValid &&
    cwValid &&
    eaValid &&
    sbValid;
  const canSave = dirty && formValid && !saving;

  // Leaving with unsaved edits. The shell asks (core/navGuard) before any
  // in-app link or palette jump; the dialog's three buttons resolve the
  // promise. `beforeunload` covers close/refresh with the browser's own
  // prompt, and one idle nudge per dirty episode catches the person who
  // changed something and then assumed it took. See core/unsavedChanges.ts.
  const [leave, setLeave] = useState<{ resolve: (ok: boolean) => void } | null>(null);
  useUnsavedChanges({
    dirty,
    onLeave: () => new Promise<boolean>((resolve) => setLeave({ resolve })),
    onIdle: () => toast.push(t("unsaved.nudge")),
    editSignal: form,
  });

  async function save() {
    if (!form || !config || !canSave) return;
    setSaving(true);
    const payload = {
      default_agent: form.default_agent,
      preferred_model: form.preferred_model.trim() || null,
      fast_model: form.fast_model.trim(),
      deep_model: form.deep_model.trim() || null,
      // "" = inherit: the PUT resets that tier to the box's built-in target.
      default_models: {
        turbo: form.dfm_turbo.trim(),
        pro: form.dfm_pro.trim(),
        max: form.dfm_max.trim(),
      },
      runtime_api_port: Number(form.runtime_api_port),
      dashboard_port: Number(form.dashboard_port),
      multi_user_enabled: form.multi_user_enabled,
      highlights_all_agents: form.highlights_all_agents,
      playground_enabled: form.playground_enabled,
      self_serve_agents: {
        enabled: form.ssa_enabled,
        max_per_user: parseInt(form.ssa_max, 10),
        allowed_archetypes: splitLines(form.ssa_archetypes),
      },
      agent_autonomy: form.agent_autonomy,
      groups: {
        enabled: form.grp_enabled,
        min_members: parseInt(form.grp_min, 10),
        max_members: parseInt(form.grp_max, 10),
        autonomous: form.grp_autonomous,
        max_hops: parseInt(form.grp_hops, 10),
        cascade_timeout_seconds: parseInt(form.grp_timeout, 10),
      },
      extra_bind_roots: splitLines(form.extra_bind_roots),
      archetype_dirs: splitLines(form.archetype_dirs),
      // The PUT normalizes each line to a bare host and 400s on anything
      // that is not one, so what lands in config.yaml is what was typed.
      egress_allow: splitLines(form.eg_allow),
      egress_deny: splitLines(form.eg_deny),
      rate_limits: {
        messages: winFromForm(form.rl_messages),
        per_platform: Object.fromEntries(
          Object.entries(form.rl_platform)
            .map(([k, v]) => [k, winFromForm(v)] as const)
            .filter(([, v]) => Object.keys(v).length > 0),
        ),
      },
      speech: { stt: form.speech_stt, tts: form.speech_tts },
      // Per-feature source pins. "" (auto) round-trips as "" — the backend
      // treats it as "use the runtime's smart chain". "olano" and
      // "provider:model" are deliberate selections; the PUT does NOT run the
      // brain-rejection guard on these (the managed service IS the choice here).
      media: { vision: form.media_vision, image: form.media_image },
      web_search: { provider: form.web_search_provider },
      disk_monitor: {
        enabled: form.dm_enabled,
        path: form.dm_path.trim(),
        check_interval_minutes: parseInt(form.dm_interval, 10),
        warn_percent: parseInt(form.dm_warn, 10),
        cleanup_percent: parseInt(form.dm_cleanup_pct, 10),
        cleanup: {
          enabled: form.dm_cleanup_enabled,
          actions: Object.entries(form.dm_actions)
            .filter(([, on]) => on)
            .map(([name]) => name),
          cooldown_minutes: parseInt(form.dm_cooldown, 10),
          force_interval_days: parseInt(form.dm_force_days, 10),
          journald_vacuum_size: form.dm_vacuum.trim(),
        },
      },
      // `available` / `unavailable_reason` are derived on the GET; the PUT
      // rejects nothing but they are not ours to send.
      magic_links: {
        enabled: form.ml_enabled,
        ttl_minutes: parseInt(form.ml_ttl, 10),
        pin: form.ml_pin,
        max_pin_attempts: parseInt(form.ml_attempts, 10),
        allow_groups: form.ml_allow_groups,
        // Deep-merged server-side; session_hours is clamped to 1–720 there.
        owner_login: {
          enabled: form.ml_ol_enabled,
          auto_bind: form.ml_ol_auto_bind,
          session_hours: parseInt(form.ml_ol_session_hours, 10),
        },
      },
      mcp_health: {
        enabled: form.mh_enabled,
        check_interval_minutes: parseInt(form.mh_interval, 10),
        refresh_tokens: form.mh_refresh_tokens,
        refresh_window_minutes: parseInt(form.mh_window, 10),
        auto_reconnect: form.mh_auto_reconnect,
      },
      // Sent only by a runtime that reported the block: an older one would
      // 400 on a key it does not know.
      ...(config.peer_threads
        ? {
            peer_threads: {
              rotate: form.pt_rotate,
              max_age_days: parseInt(form.pt_days, 10),
              max_turns: parseInt(form.pt_turns, 10),
            },
          }
        : {}),
      ...(config.checkpoint_gc
        ? {
            checkpoint_gc: {
              enabled: form.gc_enabled,
              interval_hours: parseInt(form.gc_interval, 10),
              keep_min: parseInt(form.gc_keep_min, 10),
            },
          }
        : {}),
      ...(config.mobile_app
        ? {
            mobile_app: {
              cortex_tab: form.ma_cortex,
              inbox: {
                enabled: form.ma_inbox,
                approvals: form.ma_inbox_approvals,
                alerts: form.ma_inbox_alerts,
                issues: form.ma_inbox_issues,
              },
              settings_tab: {
                files: form.ma_files,
                credits_usage: form.ma_credits,
                transcribe_on_phone: form.ma_transcribe_row,
              },
              transcribe_on_phone_default: form.ma_transcribe_default,
            },
          }
        : {}),
      pages: {
        services_enabled: form.pg_enabled,
        max_services_per_agent: parseInt(form.pg_max_services, 10),
        health_interval_seconds: parseInt(form.pg_health, 10),
        request_timeout_seconds: parseInt(form.pg_timeout, 10),
        max_request_body_mb: parseInt(form.pg_body, 10),
        max_concurrent_requests: parseInt(form.pg_concurrency, 10),
      },
      browser: browserPayload(form),
      observability: observabilityPayload(form),
      context_window: {
        keep_fraction: parseFloat(form.cw_keep_fraction),
        keep_tokens: parseInt(form.cw_keep_tokens, 10),
        trigger_fraction: parseFloat(form.cw_trigger_fraction),
        trigger_tokens: parseInt(form.cw_trigger_tokens, 10),
        mcp_result_token_limit: parseInt(form.cw_mcp_limit, 10),
      },
      external_access: {
        mcp: { enabled: form.ea_mcp },
        a2a: { enabled: form.ea_a2a },
        require_https: form.ea_require_https,
        token_ttl_days: parseInt(form.ea_ttl, 10),
        a2a_task_retention_days: parseInt(form.ea_a2a_retention, 10),
        oauth: {
          enabled: form.ea_oauth_enabled,
          dynamic_registration: form.ea_oauth_dcr,
          access_token_minutes: parseInt(form.ea_oauth_access_minutes, 10),
          refresh_token_days: parseInt(form.ea_oauth_refresh_days, 10),
        },
        jwt: {
          issuer: form.ea_jwt_issuer.trim(),
          jwks_url: form.ea_jwt_jwks.trim(),
          audience: form.ea_jwt_audience.trim(),
        },
      },
      // "" clears the field back to automatic sizing. `host`/`effective`/
      // `env_overrides` are derived on the GET and dropped by the PUT, so they
      // are not ours to send.
      sandbox: {
        memory: form.sb_memory.trim() || null,
        cpus: form.sb_cpus.trim() ? Number(form.sb_cpus) : null,
      },
      // `credits` is deliberately absent: the cost-display unit is deployment
      // policy (cloud plan, or LIONCLAW_COST_DISPLAY on a self-hosted box), not
      // an operator preference, so Config no longer offers it and must not send
      // it. The PUT still accepts the key for API clients — see
      // olano/config/system_settings.py.
    };
    // Did a restart-only field change? Drives which success toast we show.
    const restartChanged =
      payload.runtime_api_port !== config.runtime_api_port ||
      payload.dashboard_port !== config.dashboard_port ||
      payload.multi_user_enabled !== config.multi_user_enabled;
    try {
      const next = await api.put<SystemConfig>("/api/system/config", payload);
      setConfig(next);
      setForm(toForm(next));
      // Every other view formats money off a module snapshot of /api/credits;
      // without this they keep rendering the old unit until a full reload.
      refreshCostDisplay();
      // The Playground toggle drives a capability the nav and its route read,
      // so re-fetch them: the tab should appear (or go) on save, not on reload.
      refreshCapabilities();
      toast.push(restartChanged ? t("toast.savedRestart") : t("toast.saved"));
      // The leave dialog's "Save and leave" awaits this: it must know a real
      // success from a toast it cannot see. `finally` still runs first.
      return true;
    } catch (err) {
      toast.push(
        err instanceof ApiError ? err.detail : t("toast.saveFailed"),
        "error",
      );
    } finally {
      setSaving(false);
    }
    return false;
  }

  const restartBadge = (
    <span className="badge" style={{ background: "var(--warn-soft)", color: "var(--warn)" }} title={t("badge.restart.title")}>
      {t("badge.restart")}
    </span>
  );

  // Default-agent options: the live agent list, plus the saved value if it's not
  // among them (so a renamed/removed agent still shows rather than silently reset).
  const agentOptions = useMemo(() => {
    const names = agents.map((a) => a.name);
    if (form && form.default_agent && !names.includes(form.default_agent)) {
      return [form.default_agent, ...names];
    }
    return names;
  }, [agents, form]);

  const models = useMemo(() => modelOptions(cat.providers), [cat.providers]);
  // The Default-models card's option list: concrete models only — the aliases
  // themselves must not be offered as alias targets (no default:* chains).
  const concreteModels = useMemo(() => models.filter((m) => !m.alias), [models]);

  return (
    <>
      {/* Save lives in the topbar, which is `position: sticky` app-wide, so it
          stays reachable however far down this long page someone scrolls — it
          used to sit in the header row below and scroll away with it. The
          pill says the page is dirty from anywhere on it; the buttons are the
          same two the header had. */}
      <div className="topbar">
        <BrandWordmark />
        <div className="row" style={{ gap: 8, marginLeft: "auto" }} data-testid="config-actions">
          {dirty && (
            <span className="pill" data-testid="config-unsaved" style={{ fontSize: 12 }}>
              {t("unsaved.pill")}
            </span>
          )}
          {dirty && (
            <button className="btn" disabled={saving} onClick={() => config && setForm(toForm(config))}>
              {t("btn.reset")}
            </button>
          )}
          <button className="btn primary" disabled={!canSave} onClick={() => void save()}>
            {saving ? t("btn.saving") : t("btn.save")}
          </button>
        </div>
      </div>
      {/* Leaving with unsaved edits (an in-app link, or the palette): three
          ways out, same shape as the agent page's restart reminder. "Save and
          leave" is disabled while a field is invalid — the PUT would refuse it
          — and the body says so; closing (X / Esc / backdrop) stays. */}
      {leave && (
        <Modal
          title={t("unsaved.title")}
          width={440}
          onClose={() => {
            leave.resolve(false);
            setLeave(null);
          }}
          footer={
            <>
              <button
                className="btn"
                disabled={saving}
                data-testid="unsaved-leave"
                onClick={() => {
                  leave.resolve(true);
                  setLeave(null);
                }}
              >
                {t("unsaved.leave")}
              </button>
              <button
                className="btn primary"
                disabled={!canSave}
                autoFocus
                data-testid="unsaved-save-leave"
                onClick={async () => {
                  const ok = await save();
                  // A failed save keeps the person here with the error toast
                  // showing, rather than leaving with their edits lost.
                  if (ok) {
                    leave.resolve(true);
                    setLeave(null);
                  }
                }}
              >
                {saving ? t("btn.saving") : t("unsaved.saveLeave")}
              </button>
            </>
          }
        >
          <p style={{ margin: 0 }}>{formValid ? t("unsaved.body") : t("unsaved.bodyInvalid")}</p>
        </Modal>
      )}
      <div className="page" style={{ maxWidth: 920 }}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
          <div>
            <h1>{t("page.title")}</h1>
            <p className="muted" style={{ margin: "2px 0 0" }}>
              {t("page.subtitle")}
            </p>
            {config && (
              <p className="muted" style={{ margin: "4px 0 0", fontSize: 12, fontFamily: "var(--mono, monospace)" }}>
                {t("configPath", { path: config.config_path })}
              </p>
            )}
          </div>
        </div>

        {!form ? (
          <div className="spinner" style={{ marginTop: 40 }} />
        ) : (
          <>
            <Section settingId="config.general" title={t("section.general")} hint={t("section.general.hint")}>
              <div className="admin-form-row" style={{ gap: 16 }}>
                <Field
                  settingId="config.general.defaultAgent"
                  label={t("field.defaultAgent")}
                  hint={t("field.defaultAgent.hint")}
                  style={{ flex: "1 1 220px" }}
                >
                  <select
                    className="input"
                    value={form.default_agent}
                    onChange={(e) => set("default_agent", e.target.value)}
                  >
                    {agentOptions.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field
                  settingId="config.general.preferredModel"
                  label={t("field.preferredModel")}
                  hint={t("field.preferredModel.hint")}
                  style={{ flex: "2 1 320px" }}
                >
                  <ModelSelect
                    value={form.preferred_model}
                    blankLabel={t("model.builtinDefault")}
                    models={models}
                    onChange={(v) => set("preferred_model", v)}
                  />
                </Field>
              </div>
              <Field
                settingId="config.general.theme"
                label={t("field.theme")}
                hint={t("field.theme.hint")}
                style={{ marginTop: 16 }}
              >
                <div
                  style={{
                    maxWidth: 280,
                    background: "var(--surface-2)",
                    border: "1px solid var(--border)",
                    borderRadius: "var(--r-md)",
                    padding: 6,
                  }}
                >
                  <ThemePicker />
                </div>
              </Field>
            </Section>

            {/* No "Cost display" section here by design. Which unit this
                deployment shows money in is deployment policy — the cloud plan
                on a managed box, LIONCLAW_COST_DISPLAY on a self-hosted one —
                not something an operator picks per install, and the credit rate
                beside it is read-only catalog data. Surfacing a control the
                customer can't meaningfully act on only invited the question it
                couldn't answer. The setting itself is untouched: GET/PUT
                /api/system/config still carry `credits.display`, and
                core/costDisplay still reads the resolved unit from
                /api/credits, so every money figure keeps rendering in it. */}

            {/* Above Cognition deliberately: these three targets are what the
                `default:*` aliases resolve to, so they govern the brain of every
                agent on the box — including the cognition models below when
                those are left on an alias. The broader setting reads first. */}
            <Section
              settingId="config.defaultModels"
              title={t("section.defaultModels")}
              hint={t("section.defaultModels.hint")}
            >
              <div className="admin-form-row" style={{ gap: 16 }}>
                {(
                  [
                    ["dfm_turbo", "turbo", t("field.defaultTurbo"), t("field.defaultTurbo.hint")],
                    ["dfm_pro", "pro", t("field.defaultPro"), t("field.defaultPro.hint")],
                    ["dfm_max", "max", t("field.defaultMax"), t("field.defaultMax.hint")],
                  ] as const
                ).map(([field, tier, label, hint]) => {
                  const builtin = config?.default_models?.[tier]?.builtin;
                  return (
                    <Field
                      key={field}
                      settingId={`config.defaultModels.${tier}`}
                      label={label}
                      hint={hint}
                      style={{ flex: "1 1 280px" }}
                    >
                      <ModelSelect
                        value={form[field]}
                        blankLabel={
                          builtin
                            ? t("model.builtinTarget", { model: modelValueLabel(builtin) })
                            : t("model.builtinDefault")
                        }
                        // A tier target must be a concrete model — pointing an
                        // alias at another alias is rejected by the backend.
                        models={concreteModels}
                        onChange={(v) => set(field, v)}
                      />
                    </Field>
                  );
                })}
              </div>
            </Section>

            <Section
              settingId="config.cognition"
              title={t("section.cognition")}
              hint={t("section.cognition.hint")}
            >
              <div className="admin-form-row" style={{ gap: 16 }}>
                <Field
                  settingId="config.cognition.fastModel"
                  label={t("field.fastModel")}
                  hint={t("field.fastModel.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <ModelSelect
                    value={form.fast_model}
                    blankLabel={t("model.builtinDefault")}
                    models={models}
                    onChange={(v) => set("fast_model", v)}
                  />
                </Field>
                <Field
                  settingId="config.cognition.deepModel"
                  label={t("field.deepModel")}
                  hint={t("field.deepModel.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <ModelSelect
                    value={form.deep_model}
                    blankLabel={t("model.inheritAgent")}
                    models={models}
                    onChange={(v) => set("deep_model", v)}
                  />
                </Field>
              </div>
            </Section>

            <Section settingId="config.speech" title={t("section.speech")} hint={t("section.speech.hint")}>
              {(
                [
                  ["speech_stt", form.speech_stt, t("field.speechStt"), t("field.speechStt.hint")],
                  ["speech_tts", form.speech_tts, t("field.speechTts"), t("field.speechTts.hint")],
                ] as const
              ).map(([which, svc, label, hint]) => {
                // Curated model/voice options for this direction + provider drive
                // dropdowns; a provider with none (custom) falls back to free text.
                const direction = which === "speech_tts" ? "tts" : "stt";
                const catEntry = config?.speech_catalog?.[direction]?.[svc.provider];
                const modelOpts = catEntry?.models ?? [];
                const voiceOpts = catEntry?.voices ?? [];
                return (
                <div key={which} style={{ marginTop: which === "speech_tts" ? 16 : 0 }}>
                  <div className="admin-form-row" style={{ gap: 16 }}>
                    <Field
                      settingId={`config.speech.${direction}`}
                      label={label}
                      hint={hint}
                      style={{ flex: "1 1 200px" }}
                    >
                      <select
                        className="input"
                        value={svc.provider}
                        onChange={(e) => setSpeech(which, "provider", e.target.value)}
                      >
                        {(config?.speech_providers ?? ["disabled", "olano", "openai", "groq", "qwen", "custom"]).map(
                          (p) => (
                            <option key={p} value={p}>
                              {t(`speech.provider.${p}`)}
                            </option>
                          ),
                        )}
                      </select>
                    </Field>
                    {svc.provider !== "disabled" && (
                      <Field
                        settingId={`config.speech.${direction}.model`}
                        label={t("field.speechModel")}
                        hint={t("field.speechModel.hint")}
                        style={{ flex: "1 1 220px" }}
                      >
                        {modelOpts.length > 0 ? (
                          <ModelSelect
                            value={svc.model}
                            blankLabel={t("placeholder.providerDefault")}
                            models={modelOpts.map((m) => ({ value: m, label: m }))}
                            onChange={(v) => setSpeech(which, "model", v)}
                          />
                        ) : (
                          <input
                            className="input"
                            placeholder={t("placeholder.providerDefault")}
                            value={svc.model}
                            onChange={(e) => setSpeech(which, "model", e.target.value)}
                          />
                        )}
                      </Field>
                    )}
                    {svc.provider !== "disabled" && which === "speech_tts" && (
                      <Field
                        settingId="config.speech.tts.voice"
                        label={t("field.speechVoice")}
                        hint={t("field.speechVoice.hint")}
                        style={{ flex: "1 1 200px" }}
                      >
                        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                          <div style={{ flex: "1 1 auto", minWidth: 0, display: "flex", flexDirection: "column" }}>
                            {voiceOpts.length > 0 ? (
                              <ModelSelect
                                value={svc.voice}
                                blankLabel={t("placeholder.providerDefault")}
                                models={voiceOpts.map((v) => ({ value: v, label: v }))}
                                onChange={(v) => setSpeech(which, "voice", v)}
                              />
                            ) : (
                              <input
                                className="input"
                                placeholder={t("placeholder.providerDefault")}
                                value={svc.voice}
                                onChange={(e) => setSpeech(which, "voice", e.target.value)}
                              />
                            )}
                          </div>
                          <VoicePreviewButton
                            settings={{
                              provider: svc.provider,
                              model: svc.model,
                              voice: svc.voice,
                              url: svc.url,
                              api_key_name: svc.api_key_name,
                            }}
                            text={t("speech.preview.sample")}
                            labels={{
                              play: t("speech.preview.play"),
                              loading: t("speech.preview.loading"),
                              stop: t("speech.preview.stop"),
                              failed: t("speech.preview.failed"),
                            }}
                          />
                        </div>
                      </Field>
                    )}
                    {svc.provider !== "disabled" && which === "speech_tts" && (
                      <Field
                        settingId="config.speech.tts.format"
                        label={t("field.speechFormat")}
                        hint={t("field.speechFormat.hint")}
                        style={{ flex: "1 1 160px" }}
                      >
                        <select
                          className="input"
                          value={svc.audio_format}
                          onChange={(e) => setSpeech(which, "audio_format", e.target.value)}
                        >
                          <option value="">{t("speech.format.auto")}</option>
                          {TTS_AUDIO_FORMATS.map((f) => (
                            <option key={f} value={f}>
                              {t(`speech.format.${f}`)}
                            </option>
                          ))}
                        </select>
                      </Field>
                    )}
                  </div>
                  {svc.provider === "custom" && (
                    <div className="admin-form-row" style={{ gap: 16, marginTop: 10 }}>
                      <Field
                        settingId={`config.speech.${direction}.url`}
                        label={t("field.speechUrl")}
                        hint={t("field.speechUrl.hint")}
                        style={{ flex: "2 1 320px" }}
                      >
                        <input
                          className="input"
                          placeholder={
                            which === "speech_stt"
                              ? "http://localhost:8000/v1/audio/transcriptions"
                              : "http://localhost:8000/v1/audio/speech"
                          }
                          value={svc.url}
                          onChange={(e) => setSpeech(which, "url", e.target.value)}
                        />
                      </Field>
                      <Field
                        settingId={`config.speech.${direction}.keyName`}
                        label={t("field.speechKeyName")}
                        hint={t("field.speechKeyName.hint")}
                        style={{ flex: "1 1 220px" }}
                      >
                        <input
                          className="input"
                          placeholder="LIONCLAW_SPEECH_API_KEY"
                          value={svc.api_key_name}
                          onChange={(e) => setSpeech(which, "api_key_name", e.target.value)}
                        />
                      </Field>
                    </div>
                  )}
                </div>
                );
              })}
            </Section>

            <Section settingId="config.media" title={t("section.media")} hint={t("section.media.hint")}>
              <div className="admin-form-row" style={{ gap: 16 }}>
                <Field
                  settingId="config.media.vision"
                  label={t("field.mediaVision")}
                  hint={t("field.mediaVision.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <MediaSelect
                    value={form.media_vision}
                    blankLabel={t("media.auto")}
                    olanoGroupLabel={t("media.group.olano")}
                    byokGroupLabel={t("media.group.byok")}
                    options={config?.media_catalog?.vision ?? []}
                    onChange={(v) => set("media_vision", v)}
                  />
                </Field>
                <Field
                  settingId="config.media.image"
                  label={t("field.mediaImage")}
                  hint={t("field.mediaImage.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <MediaSelect
                    value={form.media_image}
                    blankLabel={t("media.auto")}
                    olanoGroupLabel={t("media.group.olano")}
                    byokGroupLabel={t("media.group.byok")}
                    options={config?.media_catalog?.image ?? []}
                    onChange={(v) => set("media_image", v)}
                  />
                </Field>
              </div>
              <div className="admin-form-row" style={{ gap: 16, marginTop: 16 }}>
                <Field
                  settingId="config.media.webSearch"
                  label={t("field.webSearch")}
                  hint={t("field.webSearch.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <select
                    className="input"
                    value={form.web_search_provider}
                    onChange={(e) => set("web_search_provider", e.target.value)}
                  >
                    <option value="">{t("media.auto")}</option>
                    {(config?.media_catalog?.web_search ?? []).some(
                      (o) => o.value === form.web_search_provider,
                    ) === false &&
                      form.web_search_provider !== "" && (
                        <option value={form.web_search_provider}>{form.web_search_provider}</option>
                      )}
                    {(config?.media_catalog?.web_search ?? []).map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
            </Section>

            <Section
              settingId="config.network"
              title={t("section.network")}
              hint={t("section.network.hint")}
              right={restartBadge}
            >
              <div className="admin-form-row" style={{ gap: 16 }}>
                <Field
                  settingId="config.network.runtimeApiPort"
                  label={t("field.runtimeApiPort")}
                  style={{ flex: "1 1 160px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={65535}
                    value={form.runtime_api_port}
                    onChange={(e) => set("runtime_api_port", e.target.value)}
                    style={!validPort(form.runtime_api_port) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
                <Field
                  settingId="config.network.dashboardPort"
                  label={t("field.dashboardPort")}
                  style={{ flex: "1 1 160px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={65535}
                    value={form.dashboard_port}
                    onChange={(e) => set("dashboard_port", e.target.value)}
                    style={!validPort(form.dashboard_port) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
              </div>
              <div style={{ marginTop: 6 }}>
                <KvRow
                  settingId="config.network.multiUser"
                  label={t("field.multiUser")}
                  hint={t("field.multiUser.hint")}
                >
                  <Toggle
                    checked={form.multi_user_enabled}
                    onChange={(v) => set("multi_user_enabled", v)}
                  />
                </KvRow>
                <KvRow
                  settingId="config.network.highlightsAll"
                  label={t("field.highlightsAll")}
                  hint={t("field.highlightsAll.hint")}
                >
                  <Toggle
                    checked={form.highlights_all_agents}
                    onChange={(v) => set("highlights_all_agents", v)}
                  />
                </KvRow>
                <KvRow
                  settingId="config.network.playground"
                  label={t("field.playground")}
                  hint={t("field.playground.hint")}
                >
                  <Toggle
                    checked={form.playground_enabled}
                    onChange={(v) => set("playground_enabled", v)}
                  />
                </KvRow>
              </div>
            </Section>

            <Section
              settingId="config.selfServe"
              title={t("section.selfServe")}
              hint={t("section.selfServe.hint")}
            >
              <div style={{ marginTop: 6 }}>
                <KvRow
                  settingId="config.selfServe.enabled"
                  label={t("field.ssaEnabled")}
                  hint={t("field.ssaEnabled.hint")}
                >
                  <Toggle checked={form.ssa_enabled} onChange={(v) => set("ssa_enabled", v)} />
                </KvRow>
              </div>
              <div className="admin-form-row" style={{ gap: 16, marginTop: 10 }}>
                <Field
                  settingId="config.selfServe.max"
                  label={t("field.ssaMax")}
                  hint={t("field.ssaMax.hint")}
                  style={{ flex: "1 1 280px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={100}
                    value={form.ssa_max}
                    onChange={(e) => set("ssa_max", e.target.value)}
                    style={!intIn(form.ssa_max, 1, 100) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
                <Field
                  settingId="config.selfServe.archetypes"
                  label={t("field.ssaArchetypes")}
                  hint={t("field.ssaArchetypes.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <textarea
                    className="input"
                    rows={3}
                    placeholder={t("field.ssaArchetypes.placeholder")}
                    value={form.ssa_archetypes}
                    onChange={(e) => set("ssa_archetypes", e.target.value)}
                  />
                </Field>
              </div>
            </Section>

            <Section settingId="config.groups" title={t("section.groups")} hint={t("section.groups.hint")}>
              <div style={{ marginTop: 6 }}>
                <KvRow
                  settingId="config.groups.enabled"
                  label={t("field.grpEnabled")}
                  hint={t("field.grpEnabled.hint")}
                >
                  <Toggle checked={form.grp_enabled} onChange={(v) => set("grp_enabled", v)} />
                </KvRow>
              </div>
              <div className="admin-form-row" style={{ gap: 16, marginTop: 10 }}>
                <Field
                  settingId="config.groups.min"
                  label={t("field.grpMin")}
                  hint={t("field.grpMin.hint")}
                  style={{ flex: "1 1 260px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={2}
                    max={6}
                    value={form.grp_min}
                    onChange={(e) => set("grp_min", e.target.value)}
                    style={
                      !intIn(form.grp_min, 2, 6) ? { borderColor: "var(--danger)" } : undefined
                    }
                  />
                </Field>
                <Field
                  settingId="config.groups.max"
                  label={t("field.grpMax")}
                  hint={t("field.grpMax.hint")}
                  style={{ flex: "1 1 260px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={2}
                    max={6}
                    value={form.grp_max}
                    onChange={(e) => set("grp_max", e.target.value)}
                    style={
                      !intIn(form.grp_max, 2, 6) ||
                      parseInt(form.grp_max, 10) < parseInt(form.grp_min, 10)
                        ? { borderColor: "var(--danger)" }
                        : undefined
                    }
                  />
                </Field>
              </div>

              {/* The cascade is a second, independent switch, on by default
                  like the one above: that one adds a surface, this one adds
                  model calls nobody asked for turn by turn. Its two budgets
                  only bind while it is on, so they are disabled rather than
                  hidden — someone deciding whether to turn it back on needs
                  to see what would bound it. */}
              <div style={{ marginTop: 14 }}>
                <KvRow
                  settingId="config.groups.autonomous"
                  label={t("field.grpAutonomous")}
                  hint={t("field.grpAutonomous.hint")}
                >
                  <Toggle
                    checked={form.grp_autonomous}
                    onChange={(v) => set("grp_autonomous", v)}
                  />
                </KvRow>
              </div>
              <div className="admin-form-row" style={{ gap: 16, marginTop: 10 }}>
                <Field
                  settingId="config.groups.hops"
                  label={t("field.grpHops")}
                  hint={t("field.grpHops.hint")}
                  style={{ flex: "1 1 260px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={10}
                    disabled={!form.grp_autonomous}
                    value={form.grp_hops}
                    onChange={(e) => set("grp_hops", e.target.value)}
                    style={
                      !intIn(form.grp_hops, 1, 10) ? { borderColor: "var(--danger)" } : undefined
                    }
                  />
                </Field>
                <Field
                  settingId="config.groups.timeout"
                  label={t("field.grpTimeout")}
                  hint={t("field.grpTimeout.hint")}
                  style={{ flex: "1 1 260px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={10}
                    max={1800}
                    disabled={!form.grp_autonomous}
                    value={form.grp_timeout}
                    onChange={(e) => set("grp_timeout", e.target.value)}
                    style={
                      !intIn(form.grp_timeout, 10, 1800)
                        ? { borderColor: "var(--danger)" }
                        : undefined
                    }
                  />
                </Field>
              </div>
            </Section>

            {Object.keys(form.agent_autonomy).length > 0 && (
              <Section
                settingId="config.autonomy"
                title={t("section.autonomy")}
                hint={t("section.autonomy.hint")}
              >
                <div style={{ marginTop: 6 }}>
                  {/* Rows come from the backend's own capability list, so a
                      runtime that grows a capability shows it without a
                      dashboard release — and an older runtime shows nothing
                      instead of dead switches. */}
                  {Object.keys(form.agent_autonomy)
                    .sort()
                    .map((cap) => (
                      <KvRow
                        key={cap}
                        label={t(`field.aut.${cap}`)}
                        hint={t(`field.aut.${cap}.hint`)}
                      >
                        <Toggle
                          checked={form.agent_autonomy[cap]}
                          onChange={(v) =>
                            set("agent_autonomy", { ...form.agent_autonomy, [cap]: v })
                          }
                        />
                      </KvRow>
                    ))}
                </div>
              </Section>
            )}

            <Section
              settingId="config.rateLimits"
              title={t("section.rateLimits")}
              hint={t("section.rateLimits.hint")}
            >
              <div className="admin-form-row" style={{ gap: 16 }}>
                <Field
                  settingId="config.rateLimits.perMinute"
                  label={t("field.rlPerMinute")}
                  style={{ flex: "1 1 150px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    placeholder={t("field.rlUnlimited")}
                    value={form.rl_messages.per_minute}
                    onChange={(e) => setRlMessage("per_minute", e.target.value)}
                    style={!validWindow(form.rl_messages.per_minute) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
                <Field
                  settingId="config.rateLimits.perHour"
                  label={t("field.rlPerHour")}
                  style={{ flex: "1 1 150px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    placeholder={t("field.rlUnlimited")}
                    value={form.rl_messages.per_hour}
                    onChange={(e) => setRlMessage("per_hour", e.target.value)}
                    style={!validWindow(form.rl_messages.per_hour) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
                <Field
                  settingId="config.rateLimits.perDay"
                  label={t("field.rlPerDay")}
                  style={{ flex: "1 1 150px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    placeholder={t("field.rlUnlimited")}
                    value={form.rl_messages.per_day}
                    onChange={(e) => setRlMessage("per_day", e.target.value)}
                    style={!validWindow(form.rl_messages.per_day) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
              </div>

              {/* Per-platform overrides — advanced, collapsed by default. */}
              <div style={{ marginTop: 14, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
                <button
                  className="btn-ghost"
                  onClick={() => setShowRlPlatforms((v) => !v)}
                  style={{ fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 6 }}
                >
                  <span style={{ color: "var(--text-3)" }}>{showRlPlatforms ? "▾" : "▸"}</span>
                  {t("field.rlPerPlatform")}
                </button>
                {showRlPlatforms && (
                  <div style={{ marginTop: 10 }}>
                    <p className="muted" style={{ fontSize: 12, margin: "0 0 10px" }}>
                      {t("field.rlPerPlatform.hint")}
                    </p>
                    {Object.keys(form.rl_platform)
                      .sort()
                      .map((p) => (
                        <div
                          key={p}
                          style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 0", flexWrap: "wrap" }}
                        >
                          <span style={{ fontFamily: "var(--mono, monospace)", fontSize: 13, minWidth: 96 }}>{p}</span>
                          {RL_WINDOW_KEYS.map((key) => (
                            <input
                              key={key}
                              className="input"
                              type="number"
                              min={1}
                              placeholder={t(`field.rlPh.${key}` as Parameters<typeof t>[0])}
                              value={form.rl_platform[p][key]}
                              onChange={(e) => setRlPlatform(p, key, e.target.value)}
                              style={{
                                width: 78,
                                ...(!validWindow(form.rl_platform[p][key]) ? { borderColor: "var(--danger)" } : {}),
                              }}
                            />
                          ))}
                          <button
                            className="btn-ghost"
                            onClick={() => removeRlPlatform(p)}
                            title={t("field.rlRemove")}
                            style={{ color: "var(--text-3)" }}
                          >
                            ✕
                          </button>
                        </div>
                      ))}
                    {(() => {
                      const addable = platformList.filter((p) => !form.rl_platform[p]);
                      return addable.length > 0 ? (
                        <select
                          className="input"
                          value=""
                          onChange={(e) => e.target.value && addRlPlatform(e.target.value)}
                          style={{ marginTop: 8, width: 220 }}
                        >
                          <option value="">{t("field.rlAddPlatform")}</option>
                          {addable.map((p) => (
                            <option key={p} value={p}>
                              {p}
                            </option>
                          ))}
                        </select>
                      ) : null;
                    })()}
                  </div>
                )}
              </div>
            </Section>

            {/* Mint + manage sign-in links (channel setup, prompt editing,
                account connections) — the primary action, kept above the
                low-level enable/TTL/PIN settings below. Self-hides for
                non-admins (its API is admin-only). */}
            <MagicLinksSection agents={agents} />

            <Section
              settingId="config.magicLinks"
              title={t("section.magicLinks")}
              hint={t("section.magicLinks.hint")}
            >
              <KvRow
                settingId="config.magicLinks.enabled"
                label={t("field.mlEnabled")}
                hint={t("field.mlEnabled.hint")}
              >
                <Toggle checked={form.ml_enabled} onChange={(v) => set("ml_enabled", v)} />
              </KvRow>

              {/* Saved state, not the toggle above: a deployment can be enabled
                  and still unable to mint (no browser-reachable public origin). */}
              {config && !magicLinksCfg(config)?.available && magicLinksCfg(config)?.unavailable_reason && (
                <p
                  style={{
                    margin: "10px 0 0",
                    padding: "9px 11px",
                    borderRadius: "var(--r-sm)",
                    background: "var(--warn-soft)",
                    border: "1px solid var(--warn)",
                    color: "var(--text-2)",
                    fontSize: 12.5,
                    lineHeight: 1.5,
                  }}
                >
                  {magicLinksCfg(config)?.unavailable_reason}
                </p>
              )}

              <div className="admin-form-row" style={{ gap: 16, marginTop: 12 }}>
                <Field
                  settingId="config.magicLinks.ttl"
                  label={t("field.mlTtl")}
                  hint={t("field.mlTtl.hint")}
                  style={{ flex: "1 1 150px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={2}
                    max={1440}
                    value={form.ml_ttl}
                    onChange={(e) => set("ml_ttl", e.target.value)}
                    style={!intIn(form.ml_ttl, 2, 1440) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
                <Field
                  settingId="config.magicLinks.pin"
                  label={t("field.mlPin")}
                  hint={t("field.mlPin.hint")}
                  style={{ flex: "1 1 180px" }}
                >
                  <select className="input" value={form.ml_pin} onChange={(e) => set("ml_pin", e.target.value)}>
                    <option value="required">{t("mlPin.required")}</option>
                    <option value="off">{t("mlPin.off")}</option>
                  </select>
                </Field>
                {/* Meaningless with no PIN to get wrong — dimmed rather than
                    removed so the row doesn't reflow on every toggle. */}
                <Field
                  settingId="config.magicLinks.attempts"
                  label={t("field.mlAttempts")}
                  hint={t("field.mlAttempts.hint")}
                  style={{ flex: "1 1 150px", opacity: form.ml_pin === "off" ? 0.55 : 1 }}
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={10}
                    disabled={form.ml_pin === "off"}
                    value={form.ml_attempts}
                    onChange={(e) => set("ml_attempts", e.target.value)}
                    style={!intIn(form.ml_attempts, 1, 10) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
              </div>

              <div style={{ marginTop: 6 }}>
                <KvRow
                  settingId="config.magicLinks.groups"
                  label={t("field.mlGroups")}
                  hint={t("field.mlGroups.hint")}
                >
                  <Toggle
                    checked={form.ml_allow_groups}
                    onChange={(v) => set("ml_allow_groups", v)}
                  />
                </KvRow>
              </div>

              {/* Dashboard access — the owner-login flow (magic_links.owner_login). */}
              <div
                style={{ marginTop: 14, borderTop: "1px solid var(--border)", paddingTop: 12 }}
                {...settingAnchor("config.ownerLogin")}
              >
                <h3 style={{ margin: 0, fontSize: 13.5, fontWeight: 600 }}>{t("section.ownerLogin")}</h3>
                <p className="muted" style={{ margin: "3px 0 6px", fontSize: 12.5, lineHeight: 1.5 }}>
                  {t("section.ownerLogin.hint")}
                </p>
                <KvRow
                  settingId="config.ownerLogin.enabled"
                  label={t("field.olEnabled")}
                  hint={t("field.olEnabled.hint")}
                >
                  <Toggle checked={form.ml_ol_enabled} onChange={(v) => set("ml_ol_enabled", v)} />
                </KvRow>
                <KvRow
                  settingId="config.ownerLogin.autoBind"
                  label={t("field.olAutoBind")}
                  hint={t("field.olAutoBind.hint")}
                >
                  <Toggle checked={form.ml_ol_auto_bind} onChange={(v) => set("ml_ol_auto_bind", v)} />
                </KvRow>
                <div className="admin-form-row" style={{ gap: 16, marginTop: 12 }}>
                  <Field
                    settingId="config.ownerLogin.sessionHours"
                    label={t("field.olSessionHours")}
                    hint={t("field.olSessionHours.hint")}
                    style={{ flex: "1 1 220px" }}
                  >
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={720}
                      value={form.ml_ol_session_hours}
                      onChange={(e) => set("ml_ol_session_hours", e.target.value)}
                      style={!intIn(form.ml_ol_session_hours, 1, 720) ? { borderColor: "var(--danger)" } : undefined}
                    />
                  </Field>
                </div>
              </div>
            </Section>

            {gwDiag && gwDiag.state !== "unattached" && (
              <Section
                settingId="config.gateway"
                title={t("section.gateway")}
                hint={t("section.gateway.hint")}
              >
                <KvRow
                  settingId="config.gateway.state"
                  label={t("field.gwState")}
                  hint={t("field.gwState.hint")}
                >
                  <span
                    className="badge"
                    style={{
                      background:
                        gwDiag.state === "enrolled" && !gwDiag.served?.missing?.length
                          ? "var(--success-soft)"
                          : "var(--warn-soft)",
                      color:
                        gwDiag.state === "enrolled" && !gwDiag.served?.missing?.length
                          ? "var(--success)"
                          : "var(--warn)",
                    }}
                  >
                    {gwDiag.state === "enrolled" && !gwDiag.served?.missing?.length
                      ? t("gw.ok")
                      : t("gw.degraded")}
                  </span>
                </KvRow>
                {/* An enrolled box with an unrouted group reads as fully healthy
                    on every other signal here — this row is the only place that
                    contradiction becomes visible. */}
                {!!gwDiag.served?.missing?.length && (
                  <p style={{ fontSize: 12.5, margin: "10px 0 0", color: "var(--warn)" }}>
                    {t("gw.unrouted", { groups: gwDiag.served.missing.join(", ") })}
                  </p>
                )}
                {gwDiag.state !== "enrolled" && (
                  <p style={{ fontSize: 12.5, margin: "10px 0 0", color: "var(--warn)" }}>
                    {t("gw.explain")}
                  </p>
                )}
                {gwDiag.last_error && (
                  <p
                    className="muted"
                    style={{
                      fontSize: 12,
                      margin: "8px 0 0",
                      fontFamily: "var(--font-mono)",
                      wordBreak: "break-word",
                    }}
                  >
                    {t("gw.lastError", { error: gwDiag.last_error })}
                  </p>
                )}
                {gwDiag.retry_in_seconds != null && (
                  <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
                    {t("gw.retry", {
                      seconds: String(Math.round(gwDiag.retry_in_seconds)),
                      attempts: String(gwDiag.consecutive_failures),
                    })}
                  </p>
                )}
                {gwDiag.env_key_revoked && (
                  <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
                    {t("gw.envRevoked")}
                  </p>
                )}
                {!!gwDiag.unenrolled_model_builds && gwDiag.state !== "enrolled" && (
                  <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
                    {t("gw.sentinelBuilds", { n: String(gwDiag.unenrolled_model_builds) })}
                  </p>
                )}
                {gwDiag.last_success_at && (
                  <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
                    {t("gw.lastSuccess", {
                      when: new Date(gwDiag.last_success_at).toLocaleString(),
                    })}
                  </p>
                )}
              </Section>
            )}

            {config?.usage_budgets && (
              <UsageBudgetsCard cfg={config.usage_budgets} onSaved={load} />
            )}

            {/* Renders nothing on a box without a sealed data volume (custody
                mode "none": every legacy and self-hosted install). */}
            <DiskEncryptionCard />

            {config?.peer_threads && (
              <Section
                settingId="config.peerThreads"
                title={t("section.peerThreads")}
                hint={t("section.peerThreads.hint")}
              >
                <KvRow
                  settingId="config.peerThreads.rotate"
                  label={t("field.ptRotate")}
                  hint={t("field.ptRotate.hint")}
                >
                  <Toggle checked={form.pt_rotate} onChange={(v) => set("pt_rotate", v)} />
                </KvRow>
                <div
                  className="admin-form-row"
                  style={{ gap: 16, marginTop: 12, opacity: form.pt_rotate ? 1 : 0.55 }}
                >
                  <Field
                    settingId="config.peerThreads.days"
                    label={t("field.ptDays")}
                    hint={t("field.ptDays.hint")}
                    style={{ flex: "1 1 220px" }}
                  >
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={365}
                      value={form.pt_days}
                      onChange={(e) => set("pt_days", e.target.value)}
                      style={
                        !intIn(form.pt_days, 1, 365)
                          ? { borderColor: "var(--danger)" }
                          : undefined
                      }
                    />
                  </Field>
                  <Field
                    settingId="config.peerThreads.turns"
                    label={t("field.ptTurns")}
                    hint={t("field.ptTurns.hint")}
                    style={{ flex: "1 1 220px" }}
                  >
                    <input
                      className="input"
                      type="number"
                      min={10}
                      max={100000}
                      value={form.pt_turns}
                      onChange={(e) => set("pt_turns", e.target.value)}
                      style={
                        !intIn(form.pt_turns, 10, 100000)
                          ? { borderColor: "var(--danger)" }
                          : undefined
                      }
                    />
                  </Field>
                </div>
              </Section>
            )}

            {config?.checkpoint_gc && (
              <Section
                settingId="config.checkpointGc"
                title={t("section.checkpointGc")}
                hint={t("section.checkpointGc.hint")}
              >
                <KvRow
                  settingId="config.checkpointGc.enabled"
                  label={t("field.gcEnabled")}
                  hint={t("field.gcEnabled.hint")}
                >
                  <Toggle checked={form.gc_enabled} onChange={(v) => set("gc_enabled", v)} />
                </KvRow>
                <div
                  className="admin-form-row"
                  style={{ gap: 16, marginTop: 12, opacity: form.gc_enabled ? 1 : 0.55 }}
                >
                  <Field
                    settingId="config.checkpointGc.interval"
                    label={t("field.gcInterval")}
                    hint={t("field.gcInterval.hint")}
                    style={{ flex: "1 1 220px" }}
                  >
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={168}
                      value={form.gc_interval}
                      onChange={(e) => set("gc_interval", e.target.value)}
                      style={
                        !intIn(form.gc_interval, 1, 168)
                          ? { borderColor: "var(--danger)" }
                          : undefined
                      }
                    />
                  </Field>
                  <Field
                    settingId="config.checkpointGc.keepMin"
                    label={t("field.gcKeepMin")}
                    hint={t("field.gcKeepMin.hint")}
                    style={{ flex: "1 1 220px" }}
                  >
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={1000}
                      value={form.gc_keep_min}
                      onChange={(e) => set("gc_keep_min", e.target.value)}
                      style={
                        !intIn(form.gc_keep_min, 1, 1000)
                          ? { borderColor: "var(--danger)" }
                          : undefined
                      }
                    />
                  </Field>
                </div>
              </Section>
            )}

            <Section
              settingId="config.mcpHealth"
              title={t("section.mcpHealth")}
              hint={t("section.mcpHealth.hint")}
            >
              {mcpSummary && (
                <p className="muted" style={{ fontSize: 12.5, margin: "0 0 12px" }}>
                  {mcpSummary}
                </p>
              )}
              <KvRow
                settingId="config.mcpHealth.enabled"
                label={t("field.mhEnabled")}
                hint={t("field.mhEnabled.hint")}
              >
                <Toggle checked={form.mh_enabled} onChange={(v) => set("mh_enabled", v)} />
              </KvRow>
              <div style={{ marginTop: 12, opacity: form.mh_enabled ? 1 : 0.55 }}>
                <KvRow
                  settingId="config.mcpHealth.refreshTokens"
                  label={t("field.mhRefreshTokens")}
                  hint={t("field.mhRefreshTokens.hint")}
                >
                  <Toggle
                    checked={form.mh_refresh_tokens}
                    onChange={(v) => set("mh_refresh_tokens", v)}
                  />
                </KvRow>
                <KvRow
                  settingId="config.mcpHealth.autoReconnect"
                  label={t("field.mhAutoReconnect")}
                  hint={t("field.mhAutoReconnect.hint")}
                >
                  <Toggle
                    checked={form.mh_auto_reconnect}
                    onChange={(v) => set("mh_auto_reconnect", v)}
                  />
                </KvRow>
                <div className="admin-form-row" style={{ gap: 16, marginTop: 12 }}>
                  <Field
                    settingId="config.mcpHealth.interval"
                    label={t("field.mhInterval")}
                    hint={t("field.mhInterval.hint")}
                    style={{ flex: "1 1 220px" }}
                  >
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={1440}
                      value={form.mh_interval}
                      onChange={(e) => set("mh_interval", e.target.value)}
                      style={
                        !intIn(form.mh_interval, 1, 1440) || !mcpWindowOrdered(form)
                          ? { borderColor: "var(--danger)" }
                          : undefined
                      }
                    />
                  </Field>
                  <Field
                    settingId="config.mcpHealth.window"
                    label={t("field.mhWindow")}
                    hint={t("field.mhWindow.hint")}
                    style={{ flex: "1 1 220px" }}
                  >
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={720}
                      value={form.mh_window}
                      onChange={(e) => set("mh_window", e.target.value)}
                      style={
                        !intIn(form.mh_window, 1, 720) || !mcpWindowOrdered(form)
                          ? { borderColor: "var(--danger)" }
                          : undefined
                      }
                    />
                  </Field>
                </div>
                {!mcpWindowOrdered(form) && (
                  <p style={{ color: "var(--danger)", fontSize: 12, margin: "6px 0 0" }}>
                    {t("field.mhWindowOrder")}
                  </p>
                )}
              </div>
            </Section>

            {config?.mobile_app && (
              <Section
                settingId="config.mobileApp"
                title={t("section.mobileApp")}
                hint={t("section.mobileApp.hint")}
              >
                <KvRow
                  settingId="config.mobileApp.cortex"
                  label={t("field.maCortex")}
                  hint={t("field.maCortex.hint")}
                >
                  <Toggle checked={form.ma_cortex} onChange={(v) => set("ma_cortex", v)} />
                </KvRow>
                <KvRow
                  settingId="config.mobileApp.inbox"
                  label={t("field.maInbox")}
                  hint={t("field.maInbox.hint")}
                >
                  <Toggle checked={form.ma_inbox} onChange={(v) => set("ma_inbox", v)} />
                </KvRow>
                <div style={{ marginLeft: 16, opacity: form.ma_inbox ? 1 : 0.55 }}>
                  {(
                    [
                      ["ma_inbox_approvals", "config.mobileApp.inboxApprovals", "field.maInboxApprovals"],
                      ["ma_inbox_alerts", "config.mobileApp.inboxAlerts", "field.maInboxAlerts"],
                      ["ma_inbox_issues", "config.mobileApp.inboxIssues", "field.maInboxIssues"],
                    ] as const
                  ).map(([key, id, label]) => (
                    <KvRow key={key} settingId={id} label={t(label)} hint={t(`${label}.hint`)}>
                      <Toggle
                        checked={form[key]}
                        disabled={!form.ma_inbox}
                        onChange={(v) => set(key, v)}
                      />
                    </KvRow>
                  ))}
                </div>
                <KvRow
                  settingId="config.mobileApp.files"
                  label={t("field.maFiles")}
                  hint={t("field.maFiles.hint")}
                >
                  <Toggle checked={form.ma_files} onChange={(v) => set("ma_files", v)} />
                </KvRow>
                <KvRow
                  settingId="config.mobileApp.credits"
                  label={t("field.maCredits")}
                  hint={t("field.maCredits.hint")}
                >
                  <Toggle checked={form.ma_credits} onChange={(v) => set("ma_credits", v)} />
                </KvRow>
                <KvRow
                  settingId="config.mobileApp.transcribeRow"
                  label={t("field.maTranscribeRow")}
                  hint={t("field.maTranscribeRow.hint")}
                >
                  <Toggle
                    checked={form.ma_transcribe_row}
                    onChange={(v) => set("ma_transcribe_row", v)}
                  />
                </KvRow>
                <KvRow
                  settingId="config.mobileApp.transcribeDefault"
                  label={t("field.maTranscribeDefault")}
                  hint={t("field.maTranscribeDefault.hint")}
                >
                  <Toggle
                    checked={form.ma_transcribe_default}
                    onChange={(v) => set("ma_transcribe_default", v)}
                  />
                </KvRow>
              </Section>
            )}

            <Section
              settingId="config.pages"
              title={t("section.pages")}
              hint={t("section.pages.hint")}
            >
              <KvRow
                settingId="config.pages.enabled"
                label={t("field.pgEnabled")}
                hint={t("field.pgEnabled.hint")}
              >
                <Toggle checked={form.pg_enabled} onChange={(v) => set("pg_enabled", v)} />
              </KvRow>
              <div style={{ marginTop: 12, opacity: form.pg_enabled ? 1 : 0.55 }}>
                <div className="admin-form-row" style={{ gap: 16 }}>
                  {(
                    [
                      ["pg_max_services", "config.pages.maxServices", "field.pgMaxServices", 1, 50],
                      ["pg_health", "config.pages.health", "field.pgHealth", 5, 3600],
                      ["pg_timeout", "config.pages.timeout", "field.pgTimeout", 5, 3600],
                      ["pg_body", "config.pages.body", "field.pgBody", 1, 1024],
                      ["pg_concurrency", "config.pages.concurrency", "field.pgConcurrency", 1, 1024],
                    ] as const
                  ).map(([key, settingId, label, lo, hi]) => (
                    <Field
                      key={key}
                      settingId={settingId}
                      label={t(label)}
                      hint={t(`${label}.hint`)}
                      style={{ flex: "1 1 220px" }}
                    >
                      <input
                        className="input"
                        type="number"
                        min={lo}
                        max={hi}
                        value={form[key]}
                        onChange={(e) => set(key, e.target.value)}
                        style={
                          !intIn(form[key], lo, hi) ? { borderColor: "var(--danger)" } : undefined
                        }
                      />
                    </Field>
                  ))}
                </div>
              </div>
            </Section>

            <BrowserConfigSection
              form={form}
              set={set}
              status={config?.browser_status}
              isAdmin={!!user?.is_admin}
            />

            <ObservabilityConfigSection form={form} set={(key, value) => set(key, value as never)} />

            <Section
              settingId="config.contextWindow"
              title={t("section.contextWindow")}
              hint={t("section.contextWindow.hint")}
            >
              <div className="admin-form-row">
                <Field
                  settingId="config.contextWindow.keepFraction"
                  label={t("field.cwKeepFraction")}
                  hint={t("field.cwKeepFraction.hint")}
                  style={{ flex: "1 1 220px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={0.05}
                    max={0.8}
                    step={0.05}
                    value={form.cw_keep_fraction}
                    onChange={(e) => set("cw_keep_fraction", e.target.value)}
                    style={
                      !cwKeepFractionOk(form) || !cwFractionsOrdered(form)
                        ? { borderColor: "var(--danger)" }
                        : undefined
                    }
                  />
                </Field>
                <Field
                  settingId="config.contextWindow.triggerFraction"
                  label={t("field.cwTriggerFraction")}
                  hint={t("field.cwTriggerFraction.hint")}
                  style={{ flex: "1 1 220px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={0.3}
                    max={0.98}
                    step={0.05}
                    value={form.cw_trigger_fraction}
                    onChange={(e) => set("cw_trigger_fraction", e.target.value)}
                    style={
                      !cwTriggerFractionOk(form) || !cwFractionsOrdered(form)
                        ? { borderColor: "var(--danger)" }
                        : undefined
                    }
                  />
                </Field>
              </div>
              {!cwFractionsOrdered(form) && (
                <p style={{ color: "var(--danger)", fontSize: 12, margin: "6px 0 0" }}>
                  {t("field.cwFractionOrder")}
                </p>
              )}
              <div className="admin-form-row" style={{ marginTop: 10 }}>
                <Field
                  settingId="config.contextWindow.keepTokens"
                  label={t("field.cwKeepTokens")}
                  hint={t("field.cwKeepTokens.hint")}
                  style={{ flex: "1 1 220px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={2000}
                    max={2000000}
                    step={1000}
                    value={form.cw_keep_tokens}
                    onChange={(e) => set("cw_keep_tokens", e.target.value)}
                    style={
                      !cwKeepTokensOk(form) || !cwTokensOrdered(form)
                        ? { borderColor: "var(--danger)" }
                        : undefined
                    }
                  />
                </Field>
                <Field
                  settingId="config.contextWindow.triggerTokens"
                  label={t("field.cwTriggerTokens")}
                  hint={t("field.cwTriggerTokens.hint")}
                  style={{ flex: "1 1 220px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={8000}
                    max={4000000}
                    step={1000}
                    value={form.cw_trigger_tokens}
                    onChange={(e) => set("cw_trigger_tokens", e.target.value)}
                    style={
                      !cwTriggerTokensOk(form) || !cwTokensOrdered(form)
                        ? { borderColor: "var(--danger)" }
                        : undefined
                    }
                  />
                </Field>
              </div>
              {!cwTokensOrdered(form) && (
                <p style={{ color: "var(--danger)", fontSize: 12, margin: "6px 0 0" }}>
                  {t("field.cwTokenOrder")}
                </p>
              )}
              <div className="admin-form-row" style={{ marginTop: 10 }}>
                <Field
                  settingId="config.contextWindow.mcpLimit"
                  label={t("field.cwMcpLimit")}
                  hint={t("field.cwMcpLimit.hint")}
                  style={{ flex: "1 1 220px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={0}
                    max={200000}
                    step={500}
                    value={form.cw_mcp_limit}
                    onChange={(e) => set("cw_mcp_limit", e.target.value)}
                    style={!cwMcpLimitOk(form) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
              </div>
            </Section>

            <Section
              settingId="config.externalAccess"
              title={t("section.externalAccess")}
              hint={t("section.externalAccess.hint")}
            >
              <p className="muted" style={{ fontSize: 12.5, margin: "0 0 12px" }}>
                {t("section.externalAccess.direction")}
              </p>
              <KvRow settingId="config.externalAccess.mcp" label={t("field.ea.mcp")} hint={t("field.ea.mcp.hint")}>
                <Toggle checked={form.ea_mcp} onChange={(v) => set("ea_mcp", v)} />
              </KvRow>
              <KvRow settingId="config.externalAccess.a2a" label={t("field.ea.a2a")} hint={t("field.ea.a2a.hint")}>
                <Toggle checked={form.ea_a2a} onChange={(v) => set("ea_a2a", v)} />
              </KvRow>
              <KvRow
                settingId="config.externalAccess.https"
                label={t("field.ea.requireHttps")}
                hint={t("field.ea.requireHttps.hint")}
              >
                <Toggle checked={form.ea_require_https} onChange={(v) => set("ea_require_https", v)} />
              </KvRow>
              <div className="admin-form-row" style={{ marginTop: 10 }}>
                <Field settingId="config.externalAccess.ttl" label={t("field.ea.ttl")} hint={t("field.ea.ttl.hint")}>
                  <input
                    className="input"
                    type="number"
                    min={0}
                    max={3650}
                    value={form.ea_ttl}
                    onChange={(e) => set("ea_ttl", e.target.value)}
                    style={{ borderColor: eaIntIn(form.ea_ttl, 0, 3650) ? undefined : "var(--danger)" }}
                  />
                </Field>
                <Field
                  settingId="config.externalAccess.a2aRetention"
                  label={t("field.ea.a2aRetention")}
                  hint={t("field.ea.a2aRetention.hint")}
                >
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={3650}
                    value={form.ea_a2a_retention}
                    onChange={(e) => set("ea_a2a_retention", e.target.value)}
                    style={{
                      borderColor: eaIntIn(form.ea_a2a_retention, 1, 3650) ? undefined : "var(--danger)",
                    }}
                  />
                </Field>
              </div>
              <h4 style={{ margin: "14px 0 4px" }}>{t("field.ea.oauth")}</h4>
              <p className="muted" style={{ fontSize: 12, lineHeight: 1.45, margin: "0 0 8px" }}>
                {t("field.ea.oauth.hint")}
              </p>
              <KvRow
                settingId="config.externalAccess.oauth"
                label={t("field.ea.oauthEnabled")}
                hint={t("field.ea.oauthEnabled.hint")}
              >
                <Toggle checked={form.ea_oauth_enabled} onChange={(v) => set("ea_oauth_enabled", v)} />
              </KvRow>
              <div style={{ opacity: form.ea_oauth_enabled ? 1 : 0.55 }}>
                <KvRow
                  settingId="config.externalAccess.dcr"
                  label={t("field.ea.oauthDcr")}
                  hint={t("field.ea.oauthDcr.hint")}
                >
                  <Toggle checked={form.ea_oauth_dcr} onChange={(v) => set("ea_oauth_dcr", v)} />
                </KvRow>
                <div className="admin-form-row" style={{ marginTop: 10 }}>
                  <Field
                    settingId="config.externalAccess.accessMinutes"
                    label={t("field.ea.oauthAccess")}
                    hint={t("field.ea.oauthAccess.hint")}
                  >
                    <input
                      className="input"
                      type="number"
                      min={5}
                      max={1440}
                      value={form.ea_oauth_access_minutes}
                      onChange={(e) => set("ea_oauth_access_minutes", e.target.value)}
                      style={{
                        borderColor: eaIntIn(form.ea_oauth_access_minutes, 5, 1440) ? undefined : "var(--danger)",
                      }}
                    />
                  </Field>
                  <Field
                    settingId="config.externalAccess.refreshDays"
                    label={t("field.ea.oauthRefresh")}
                    hint={t("field.ea.oauthRefresh.hint")}
                  >
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={365}
                      value={form.ea_oauth_refresh_days}
                      onChange={(e) => set("ea_oauth_refresh_days", e.target.value)}
                      style={{
                        borderColor: eaIntIn(form.ea_oauth_refresh_days, 1, 365) ? undefined : "var(--danger)",
                      }}
                    />
                  </Field>
                </div>
              </div>
              <h4 style={{ margin: "14px 0 4px" }}>{t("field.ea.jwt")}</h4>
              <p className="muted" style={{ fontSize: 12, lineHeight: 1.45, margin: "0 0 8px" }}>
                {t("field.ea.jwt.hint")}
              </p>
              <div className="admin-form-row">
                <Field settingId="config.externalAccess.jwtIssuer" label={t("field.ea.jwtIssuer")} hint={t("field.ea.jwtIssuer.hint")}>
                  <input
                    className="input"
                    value={form.ea_jwt_issuer}
                    placeholder="https://your-tenant.auth0.com/"
                    onChange={(e) => set("ea_jwt_issuer", e.target.value)}
                  />
                </Field>
                <Field settingId="config.externalAccess.jwtJwks" label={t("field.ea.jwtJwks")} hint={t("field.ea.jwtJwks.hint")}>
                  <input
                    className="input"
                    value={form.ea_jwt_jwks}
                    placeholder="https://your-tenant.auth0.com/.well-known/jwks.json"
                    onChange={(e) => set("ea_jwt_jwks", e.target.value)}
                  />
                </Field>
              </div>
              <div className="admin-form-row">
                <Field settingId="config.externalAccess.jwtAudience" label={t("field.ea.jwtAudience")} hint={t("field.ea.jwtAudience.hint")}>
                  <input
                    className="input"
                    value={form.ea_jwt_audience}
                    onChange={(e) => set("ea_jwt_audience", e.target.value)}
                  />
                </Field>
              </div>
              {(form.ea_jwt_issuer.trim() === "") !== (form.ea_jwt_jwks.trim() === "") && (
                <p style={{ color: "var(--danger)", fontSize: 12.5 }}>{t("field.ea.jwt.half")}</p>
              )}
              <ExternalAccessAdminPanel />
            </Section>

            <Section settingId="config.sandbox" title={t("section.sandbox")} hint={t("section.sandbox.hint")}>
              {sandboxSummary && (
                <p className="muted" style={{ fontSize: 12.5, margin: "0 0 12px" }}>
                  {sandboxSummary}
                </p>
              )}
              {(config?.sandbox?.env_overrides?.memory ||
                config?.sandbox?.env_overrides?.cpus) && (
                <p style={{ color: "var(--warn)", fontSize: 12, margin: "0 0 12px" }}>
                  {t("sandbox.envOverride")}
                </p>
              )}
              <div className="admin-form-row" style={{ gap: 16 }}>
                <Field
                  settingId="config.sandbox.memory"
                  label={t("field.sbMemory")}
                  hint={t("field.sbMemory.hint")}
                  style={{ flex: "1 1 260px" }}
                >
                  <input
                    className="input"
                    type="text"
                    placeholder={t("field.sbMemory.placeholder")}
                    value={form.sb_memory}
                    onChange={(e) => set("sb_memory", e.target.value)}
                    style={
                      validSandboxMemory(form.sb_memory)
                        ? undefined
                        : { borderColor: "var(--danger)" }
                    }
                  />
                </Field>
                <Field
                  settingId="config.sandbox.cpus"
                  label={t("field.sbCpus")}
                  hint={t("field.sbCpus.hint")}
                  style={{ flex: "1 1 260px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={0.1}
                    max={64}
                    step={0.1}
                    placeholder={t("field.sbCpus.placeholder")}
                    value={form.sb_cpus}
                    onChange={(e) => set("sb_cpus", e.target.value)}
                    style={
                      validSandboxCpus(form.sb_cpus) ? undefined : { borderColor: "var(--danger)" }
                    }
                  />
                </Field>
              </div>
            </Section>

            <Section settingId="config.egress" title={t("section.egress")} hint={t("section.egress.hint")}>
              <div className="admin-form-row" style={{ gap: 16 }}>
                <Field
                  settingId="config.egress.allow"
                  label={t("field.egAllow")}
                  hint={t("field.egAllow.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <textarea
                    className="input"
                    rows={4}
                    placeholder={t("placeholder.egDomains")}
                    value={form.eg_allow}
                    onChange={(e) => set("eg_allow", e.target.value)}
                    spellCheck={false}
                    style={{ resize: "vertical", fontFamily: "var(--mono, monospace)", fontSize: 12.5 }}
                  />
                </Field>
                <Field
                  settingId="config.egress.deny"
                  label={t("field.egDeny")}
                  hint={t("field.egDeny.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <textarea
                    className="input"
                    rows={4}
                    placeholder={t("placeholder.egDomains")}
                    value={form.eg_deny}
                    onChange={(e) => set("eg_deny", e.target.value)}
                    spellCheck={false}
                    style={{ resize: "vertical", fontFamily: "var(--mono, monospace)", fontSize: 12.5 }}
                  />
                </Field>
              </div>
            </Section>

            <CatalogSyncCard />

            <Section settingId="config.disk" title={t("section.disk")} hint={t("section.disk.hint")}>
              <KvRow
                settingId="config.disk.enabled"
                label={t("field.dmEnabled")}
                hint={t("field.dmEnabled.hint")}
              >
                <Toggle checked={form.dm_enabled} onChange={(v) => set("dm_enabled", v)} />
              </KvRow>
              <div className="admin-form-row" style={{ gap: 16, marginTop: 12 }}>
                <Field
                  settingId="config.disk.path"
                  label={t("field.dmPath")}
                  hint={t("field.dmPath.hint")}
                  style={{ flex: "1 1 150px" }}
                >
                  <input
                    className="input"
                    value={form.dm_path}
                    onChange={(e) => set("dm_path", e.target.value)}
                    style={form.dm_path.trim() === "" ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
                <Field
                  settingId="config.disk.interval"
                  label={t("field.dmInterval")}
                  hint={t("field.dmInterval.hint")}
                  style={{ flex: "1 1 140px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={5}
                    max={1440}
                    value={form.dm_interval}
                    onChange={(e) => set("dm_interval", e.target.value)}
                    style={!intIn(form.dm_interval, 5, 1440) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
                <Field
                  settingId="config.disk.warn"
                  label={t("field.dmWarn")}
                  hint={t("field.dmWarn.hint")}
                  style={{ flex: "1 1 140px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={50}
                    max={99}
                    value={form.dm_warn}
                    onChange={(e) => set("dm_warn", e.target.value)}
                    style={!intIn(form.dm_warn, 50, 99) ? { borderColor: "var(--danger)" } : undefined}
                  />
                </Field>
                <Field
                  settingId="config.disk.cleanupPct"
                  label={t("field.dmCleanupPct")}
                  hint={t("field.dmCleanupPct.hint")}
                  style={{ flex: "1 1 140px" }}
                >
                  <input
                    className="input"
                    type="number"
                    min={50}
                    max={99}
                    value={form.dm_cleanup_pct}
                    onChange={(e) => set("dm_cleanup_pct", e.target.value)}
                    style={
                      !intIn(form.dm_cleanup_pct, 50, 99) || !diskThresholdsOrdered(form)
                        ? { borderColor: "var(--danger)" }
                        : undefined
                    }
                  />
                </Field>
              </div>
              {!diskThresholdsOrdered(form) && (
                <p style={{ color: "var(--danger)", fontSize: 12, margin: "6px 0 0" }}>
                  {t("field.dmThresholdOrder")}
                </p>
              )}

              {/* Automatic cleanup — what actually runs when the threshold is crossed. */}
              <div style={{ marginTop: 14, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
                <KvRow
                  settingId="config.disk.cleanupEnabled"
                  label={t("field.dmCleanupEnabled")}
                  hint={t("field.dmCleanupEnabled.hint")}
                >
                  <Toggle
                    checked={form.dm_cleanup_enabled}
                    onChange={(v) => set("dm_cleanup_enabled", v)}
                  />
                </KvRow>
                <div style={{ marginTop: 10, opacity: form.dm_cleanup_enabled ? 1 : 0.55 }}>
                  <p className="muted" style={{ fontSize: 12, margin: "0 0 6px" }}>
                    {t("disk.actions.hint")}
                  </p>
                  {(config?.disk_cleanup_actions ?? []).map((a) => (
                    <label
                      key={a.name}
                      className="row"
                      style={{ gap: 10, padding: "7px 0", alignItems: "flex-start", cursor: "pointer" }}
                    >
                      <input
                        type="checkbox"
                        checked={!!form.dm_actions[a.name]}
                        onChange={(e) => setDiskAction(a.name, e.target.checked)}
                        style={{ marginTop: 3 }}
                      />
                      <span style={{ minWidth: 0 }}>
                        <span style={{ fontFamily: "var(--mono, monospace)", fontSize: 13 }}>{a.name}</span>
                        {!a.safe && (
                          <span
                            className="badge"
                            style={{ marginLeft: 8, background: "var(--warn-soft)", color: "var(--warn)" }}
                            title={t("disk.badge.aggressive.title")}
                          >
                            {t("disk.badge.aggressive")}
                          </span>
                        )}
                        {!a.available && (
                          <span className="badge" style={{ marginLeft: 8 }} title={a.reason ?? ""}>
                            {t("disk.badge.unavailable")}
                          </span>
                        )}
                        <span className="muted" style={{ display: "block", fontSize: 12 }}>
                          {a.description}
                        </span>
                      </span>
                    </label>
                  ))}
                  <div className="admin-form-row" style={{ gap: 16, marginTop: 10 }}>
                    <Field
                      settingId="config.disk.cooldown"
                      label={t("field.dmCooldown")}
                      hint={t("field.dmCooldown.hint")}
                      style={{ flex: "1 1 150px" }}
                    >
                      <input
                        className="input"
                        type="number"
                        min={30}
                        max={10080}
                        value={form.dm_cooldown}
                        onChange={(e) => set("dm_cooldown", e.target.value)}
                        style={!intIn(form.dm_cooldown, 30, 10080) ? { borderColor: "var(--danger)" } : undefined}
                      />
                    </Field>
                    <Field
                      settingId="config.disk.forceDays"
                      label={t("field.dmForceDays")}
                      hint={t("field.dmForceDays.hint")}
                      style={{ flex: "1 1 150px" }}
                    >
                      <input
                        className="input"
                        type="number"
                        min={0}
                        max={90}
                        value={form.dm_force_days}
                        onChange={(e) => set("dm_force_days", e.target.value)}
                        style={!intIn(form.dm_force_days, 0, 90) ? { borderColor: "var(--danger)" } : undefined}
                      />
                    </Field>
                    <Field
                      settingId="config.disk.vacuum"
                      label={t("field.dmVacuum")}
                      hint={t("field.dmVacuum.hint")}
                      style={{ flex: "1 1 150px" }}
                    >
                      <input
                        className="input"
                        placeholder="200M"
                        value={form.dm_vacuum}
                        onChange={(e) => set("dm_vacuum", e.target.value)}
                        style={!validVacuum(form.dm_vacuum) ? { borderColor: "var(--danger)" } : undefined}
                      />
                    </Field>
                  </div>
                </div>
              </div>
            </Section>

            <Section settingId="config.paths" title={t("section.paths")} hint={t("section.paths.hint")}>
              <div className="admin-form-row" style={{ gap: 16 }}>
                <Field
                  settingId="config.paths.extraBindRoots"
                  label={t("field.extraBindRoots")}
                  hint={t("field.extraBindRoots.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <textarea
                    className="input"
                    rows={3}
                    placeholder={t("placeholder.onePerLine")}
                    value={form.extra_bind_roots}
                    onChange={(e) => set("extra_bind_roots", e.target.value)}
                    style={{ resize: "vertical", fontFamily: "var(--mono, monospace)", fontSize: 12.5 }}
                  />
                </Field>
                <Field
                  settingId="config.paths.archetypeDirs"
                  label={t("field.archetypeDirs")}
                  hint={t("field.archetypeDirs.hint")}
                  style={{ flex: "1 1 320px" }}
                >
                  <textarea
                    className="input"
                    rows={3}
                    placeholder={t("placeholder.onePerLine")}
                    value={form.archetype_dirs}
                    onChange={(e) => set("archetype_dirs", e.target.value)}
                    style={{ resize: "vertical", fontFamily: "var(--mono, monospace)", fontSize: 12.5 }}
                  />
                </Field>
              </div>
            </Section>
          </>
        )}
      </div>
    </>
  );
}
