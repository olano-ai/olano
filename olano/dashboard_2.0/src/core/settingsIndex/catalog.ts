// Hand-authored settings entries for the surfaces built from plain JSX.
//
// Surfaces that already carry a declarative field catalog (the 152 connector
// fields in views/agents/connectorCatalog.ts, the credential fields in
// views/connections/types.ts) are NOT listed here — ./adapters.ts derives them,
// so adding a connector field makes it searchable with no edit to this file.
//
// Every id here must match a data-setting-id rendered by the owning view;
// tests/e2e/test_settings_index.py fails the build otherwise.

import { STR as BROWSER_STR } from "../../views/browser/strings";
import { STR as OBSERVABILITY_STR } from "../../views/observability/strings";
import { STR as CONFIG_STR } from "../../views/config/strings";
import type { SettingsEntry } from "./types";

const CONFIG = { route: "/config", dict: CONFIG_STR, adminOnly: true } as const;

/** A /config section card. */
function section(id: string, labelKey: string, keywords?: string[], conditional?: boolean): SettingsEntry {
  return {
    ...CONFIG,
    id,
    kind: "section",
    labelKey,
    hintKey: `${labelKey}.hint`,
    breadcrumbKeys: ["nav.config"],
    keywords,
    conditional,
  };
}

/** One control inside a /config section. */
function field(
  id: string,
  labelKey: string,
  sectionKey: string,
  keywords?: string[],
  conditional?: boolean,
): SettingsEntry {
  return {
    ...CONFIG,
    id,
    kind: "field",
    labelKey,
    hintKey: `${labelKey}.hint`,
    breadcrumbKeys: ["nav.config", sectionKey],
    keywords,
    conditional,
  };
}

export const CONFIG_ENTRIES: SettingsEntry[] = [
  // ── General ──────────────────────────────────────────────────────────────
  section("config.general", "section.general", ["defaults", "language", "appearance"]),
  field("config.general.defaultAgent", "field.defaultAgent", "section.general", ["fallback agent"]),
  field("config.general.preferredModel", "field.preferredModel", "section.general", ["llm", "default model"]),
  field("config.general.theme", "field.theme", "section.general", ["dark mode", "light mode", "appearance"]),

  // ── Default models ───────────────────────────────────────────────────────
  section("config.defaultModels", "section.defaultModels", ["alias", "tier", "llm"]),
  field("config.defaultModels.turbo", "field.defaultTurbo", "section.defaultModels", ["alias", "fast tier"]),
  field("config.defaultModels.pro", "field.defaultPro", "section.defaultModels", ["alias"]),
  field("config.defaultModels.max", "field.defaultMax", "section.defaultModels", ["alias", "reasoning"]),

  // ── Cognition ────────────────────────────────────────────────────────────
  section("config.cognition", "section.cognition", ["cortex", "thinking", "llm"]),
  field("config.cognition.fastModel", "field.fastModel", "section.cognition", ["cortex", "cheap model"]),
  field("config.cognition.deepModel", "field.deepModel", "section.cognition", ["cortex", "heavy model"]),

  // ── Speech ───────────────────────────────────────────────────────────────
  section("config.speech", "section.speech", ["voice", "audio", "stt", "tts", "transcription"]),
  field("config.speech.stt", "field.speechStt", "section.speech", ["transcription", "speech to text", "whisper"]),
  field("config.speech.tts", "field.speechTts", "section.speech", ["voice replies", "text to speech"]),
  // Only rendered once the direction has a provider (TTS ships disabled).
  field("config.speech.stt.model", "field.speechModel", "section.speech", ["transcription model"], true),
  field("config.speech.tts.model", "field.speechModel", "section.speech", ["voice model"], true),
  field("config.speech.tts.voice", "field.speechVoice", "section.speech", ["voice name"], true),
  field("config.speech.tts.format", "field.speechFormat", "section.speech", ["audio format", "mp3", "opus"], true),
  field("config.speech.stt.url", "field.speechUrl", "section.speech", ["custom endpoint"], true),
  field("config.speech.tts.url", "field.speechUrl", "section.speech", ["custom endpoint"], true),
  field("config.speech.stt.keyName", "field.speechKeyName", "section.speech", ["api key"], true),
  field("config.speech.tts.keyName", "field.speechKeyName", "section.speech", ["api key"], true),

  // ── Media & search ───────────────────────────────────────────────────────
  section("config.media", "section.media", ["vision", "images", "web search"]),
  field("config.media.vision", "field.mediaVision", "section.media", ["image describe", "photo"]),
  field("config.media.image", "field.mediaImage", "section.media", ["image generation", "avatars"]),
  field("config.media.webSearch", "field.webSearch", "section.media", ["browsing", "search provider"]),

  // ── Network & access ─────────────────────────────────────────────────────
  section("config.network", "section.network", ["ports", "access", "team accounts"]),
  field("config.network.runtimeApiPort", "field.runtimeApiPort", "section.network", ["port", "api"]),
  field("config.network.dashboardPort", "field.dashboardPort", "section.network", ["port", "ui"]),
  field("config.network.multiUser", "field.multiUser", "section.network", ["team accounts", "members"]),
  field("config.network.highlightsAll", "field.highlightsAll", "section.network", ["insights", "visibility"]),
  field("config.network.playground", "field.playground", "section.network", ["sandbox tab"]),

  // ── Member bot creation ──────────────────────────────────────────────────
  section("config.selfServe", "section.selfServe", ["member bots", "self serve"]),
  field("config.selfServe.enabled", "field.ssaEnabled", "section.selfServe", ["allow members"]),
  field("config.selfServe.max", "field.ssaMax", "section.selfServe", ["quota", "limit"]),
  field("config.selfServe.archetypes", "field.ssaArchetypes", "section.selfServe", ["templates", "experts"]),

  // ── Group chats ──────────────────────────────────────────────────────────
  section("config.groups", "section.groups", ["group chat", "multi agent"]),
  field("config.groups.enabled", "field.grpEnabled", "section.groups"),
  field("config.groups.min", "field.grpMin", "section.groups", ["minimum agents"]),
  field("config.groups.max", "field.grpMax", "section.groups", ["maximum agents"]),
  field("config.groups.autonomous", "field.grpAutonomous", "section.groups", ["agents talk to each other"]),
  field("config.groups.hops", "field.grpHops", "section.groups", ["relay depth"]),
  field("config.groups.timeout", "field.grpTimeout", "section.groups"),

  // ── Agent self-configuration (only when autonomy toggles exist) ──────────
  section("config.autonomy", "section.autonomy", ["self configuration", "agent edits itself"], true),

  // ── Rate limits ──────────────────────────────────────────────────────────
  section("config.rateLimits", "section.rateLimits", ["throttle", "quota", "flood"]),
  field("config.rateLimits.perMinute", "field.rlPerMinute", "section.rateLimits", ["throttle"]),
  field("config.rateLimits.perHour", "field.rlPerHour", "section.rateLimits", ["throttle"]),
  field("config.rateLimits.perDay", "field.rlPerDay", "section.rateLimits", ["throttle"]),

  // ── Magic links / dashboard access ───────────────────────────────────────
  section("config.magicLinks", "section.magicLinks", ["login link", "passwordless"]),
  field("config.magicLinks.enabled", "field.mlEnabled", "section.magicLinks", ["passwordless login"]),
  field("config.magicLinks.ttl", "field.mlTtl", "section.magicLinks", ["expiry", "lifetime"]),
  field("config.magicLinks.pin", "field.mlPin", "section.magicLinks", ["code", "verification"]),
  field("config.magicLinks.attempts", "field.mlAttempts", "section.magicLinks", ["retries", "lockout"]),
  field("config.magicLinks.groups", "field.mlGroups", "section.magicLinks", ["group chats"]),
  section("config.ownerLogin", "section.ownerLogin", ["dashboard access", "owner", "sign in"]),
  field("config.ownerLogin.enabled", "field.olEnabled", "section.ownerLogin", ["owner login"]),
  field("config.ownerLogin.autoBind", "field.olAutoBind", "section.ownerLogin", ["first login"]),
  field("config.ownerLogin.sessionHours", "field.olSessionHours", "section.ownerLogin", ["session length", "logout"]),

  // ── Olano model gateway (only on an attached deployment) ─────────────────
  section("config.gateway", "section.gateway", ["credits", "managed models"], true),
  field("config.gateway.state", "field.gwState", "section.gateway", ["enrollment", "credits"], true),

  // ── Usage budgets (only when configured) ─────────────────────────────────
  section("config.budgets", "section.budgets", ["spend", "credits", "cap"], true),
  field("config.budgets.alerts", "field.budgetAlerts", "section.budgets", ["spend alerts"], true),
  field("config.budgets.percents", "field.budgetPercents", "section.budgets", ["thresholds"], true),

  // ── MCP connection health ────────────────────────────────────────────────
  section("config.externalAccess", "section.externalAccess", ["mcp server", "a2a", "oauth", "tokens", "inbound", "expose"]),
  field("config.externalAccess.mcp", "field.ea.mcp", "section.externalAccess", ["mcp endpoint", "claude", "chatgpt", "cursor"]),
  field("config.externalAccess.a2a", "field.ea.a2a", "section.externalAccess", ["agent card", "agent2agent"]),
  field("config.externalAccess.https", "field.ea.requireHttps", "section.externalAccess", ["tls", "https"]),
  field("config.externalAccess.ttl", "field.ea.ttl", "section.externalAccess", ["token lifetime", "expiry"]),
  field("config.externalAccess.a2aRetention", "field.ea.a2aRetention", "section.externalAccess", ["task retention", "a2a tasks", "task store"]),
  field("config.externalAccess.oauth", "field.ea.oauthEnabled", "section.externalAccess", ["authorization server", "consent"]),
  field("config.externalAccess.dcr", "field.ea.oauthDcr", "section.externalAccess", ["client registration", "rfc 7591"]),
  field("config.externalAccess.accessMinutes", "field.ea.oauthAccess", "section.externalAccess", ["access token"]),
  field("config.externalAccess.refreshDays", "field.ea.oauthRefresh", "section.externalAccess", ["refresh token"]),
  field("config.externalAccess.jwtIssuer", "field.ea.jwtIssuer", "section.externalAccess", ["jwt", "identity provider", "auth0", "keycloak"]),
  field("config.externalAccess.jwtJwks", "field.ea.jwtJwks", "section.externalAccess", ["jwks", "signing keys"]),
  field("config.externalAccess.jwtAudience", "field.ea.jwtAudience", "section.externalAccess", ["aud", "audience"]),
  section("config.mcpHealth", "section.mcpHealth", ["mcp", "oauth", "reconnect"]),
  field("config.mcpHealth.enabled", "field.mhEnabled", "section.mcpHealth", ["mcp sweep"]),
  field("config.mcpHealth.refreshTokens", "field.mhRefreshTokens", "section.mcpHealth", ["oauth refresh"]),
  field("config.mcpHealth.autoReconnect", "field.mhAutoReconnect", "section.mcpHealth", ["retry", "backoff"]),
  field("config.mcpHealth.interval", "field.mhInterval", "section.mcpHealth", ["sweep interval"]),
  field("config.mcpHealth.window", "field.mhWindow", "section.mcpHealth", ["renewal window"]),

  // ── Mobile app ───────────────────────────────────────────────────────────
  section("config.mobileApp", "section.mobileApp", ["mobile app", "phone", "ios", "android", "tabs", "customers"]),
  field("config.mobileApp.cortex", "field.maCortex", "section.mobileApp", ["cortex tab", "hide cortex"]),
  field("config.mobileApp.inbox", "field.maInbox", "section.mobileApp", ["inbox tab", "hide inbox"]),
  field("config.mobileApp.inboxApprovals", "field.maInboxApprovals", "section.mobileApp", ["approvals"]),
  field("config.mobileApp.inboxAlerts", "field.maInboxAlerts", "section.mobileApp", ["alerts", "notifications"]),
  field("config.mobileApp.inboxIssues", "field.maInboxIssues", "section.mobileApp", ["issues"]),
  field("config.mobileApp.files", "field.maFiles", "section.mobileApp", ["files"]),
  field("config.mobileApp.credits", "field.maCredits", "section.mobileApp", ["credits", "usage", "balance"]),
  field("config.mobileApp.transcribeRow", "field.maTranscribeRow", "section.mobileApp", ["transcribe", "voice notes"]),
  field("config.mobileApp.transcribeDefault", "field.maTranscribeDefault", "section.mobileApp", ["speech to text", "voice notes", "on device"]),

  // ── Deployed pages ───────────────────────────────────────────────────────
  section("config.pages", "section.pages", ["pages", "pages_deploy", "web app", "server", "npm", "build"]),
  field("config.pages.enabled", "field.pgEnabled", "section.pages", ["page builds", "page servers", "kill switch"]),
  field("config.pages.maxServices", "field.pgMaxServices", "section.pages", ["servers per agent", "limit"]),
  field("config.pages.health", "field.pgHealth", "section.pages", ["health check", "restart"]),
  field("config.pages.timeout", "field.pgTimeout", "section.pages", ["request timeout", "504"]),
  field("config.pages.body", "field.pgBody", "section.pages", ["upload size", "413"]),
  field("config.pages.concurrency", "field.pgConcurrency", "section.pages", ["concurrent requests", "busy"]),

  // ── Browser (strings live with the section in views/browser) ─────────────
  { ...section("config.browser", "br.section", ["browser", "chromium", "web", "playwright", "live view"]), dict: BROWSER_STR },
  { ...field("config.browser.mode", "br.mode", "br.section", ["headless", "live browser", "computer use"]), dict: BROWSER_STR },
  { ...field("config.browser.provider", "br.provider", "br.section", ["remote browser", "cdp", "browserbase", "proxy", "BROWSER_PROXY_URL", "residential"]), dict: BROWSER_STR },
  { ...field("config.browser.maxSessions", "br.maxSessions", "br.section", ["concurrent browsers"]), dict: BROWSER_STR },
  { ...field("config.browser.idleTimeout", "br.idle", "br.section", ["idle timeout"]), dict: BROWSER_STR },
  { ...field("config.browser.timezone", "br.timezone", "br.section", ["time zone", "timezone", "clock"]), dict: BROWSER_STR },
  // ── Observability / Langfuse (strings live with the section in views/observability) ──
  { ...section("config.observability", "ob.section", ["langfuse", "tracing", "traces", "observability", "telemetry", "llm analytics", "evals"]), dict: OBSERVABILITY_STR },
  { ...field("config.observability.enabled", "ob.enabled", "ob.section", ["langfuse on", "enable tracing"]), dict: OBSERVABILITY_STR },
  { ...field("config.observability.host", "ob.host", "ob.section", ["langfuse url", "region", "LANGFUSE_BASE_URL", "self-hosted langfuse"]), dict: OBSERVABILITY_STR },
  { ...field("config.observability.capture", "ob.capture", "ob.section", ["privacy", "redaction", "metadata only", "full content"]), dict: OBSERVABILITY_STR },
  { ...field("config.observability.sampleRate", "ob.sample", "ob.section", ["sampling", "sample rate"]), dict: OBSERVABILITY_STR },
  { ...field("config.observability.environment", "ob.environment", "ob.section", ["environment", "staging", "production"]), dict: OBSERVABILITY_STR },
  { ...field("config.observability.background", "ob.background", "ob.section", ["cortex traces"]), dict: OBSERVABILITY_STR },
  { ...field("config.observability.keys", "ob.keys", "ob.section", ["langfuse keys", "public key", "secret key", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]), dict: OBSERVABILITY_STR },

  // ── Sandbox ──────────────────────────────────────────────────────────────
  section("config.sandbox", "section.sandbox", ["docker", "container", "resources"]),
  field("config.sandbox.memory", "field.sbMemory", "section.sandbox", ["ram", "docker memory"]),
  field("config.sandbox.cpus", "field.sbCpus", "section.sandbox", ["cpu limit", "docker cpus"]),

  // ── Outbound access (egress) ─────────────────────────────────────────────
  section("config.egress", "section.egress", ["egress", "network", "firewall", "domains", "ssrf"]),
  field("config.egress.allow", "field.egAllow", "section.egress", ["allowlist", "egress_allow", "whitelist"]),
  field("config.egress.deny", "field.egDeny", "section.egress", ["blocklist", "egress_deny", "blacklist"]),

  // ── Cloud catalogs ───────────────────────────────────────────────────────
  section("config.catalogs", "section.catalogs", ["sync", "models catalog", "archetypes", "connectors"]),
  // Renders only on a managed box provisioned with a sealed data volume
  // (custody mode != none), hence conditional.
  section(
    "config.diskEncryption",
    "section.diskEncryption",
    ["luks", "recovery key", "encryption", "sealed", "custody", "encrypted volume"],
    true,
  ),

  // ── Disk monitor & cleanup ───────────────────────────────────────────────
  section("config.disk", "section.disk", ["storage", "cleanup", "space"]),
  field("config.disk.enabled", "field.dmEnabled", "section.disk", ["disk monitor"]),
  field("config.disk.path", "field.dmPath", "section.disk", ["mount", "volume"]),
  field("config.disk.interval", "field.dmInterval", "section.disk", ["check interval"]),
  field("config.disk.warn", "field.dmWarn", "section.disk", ["warning threshold"]),
  field("config.disk.cleanupPct", "field.dmCleanupPct", "section.disk", ["cleanup threshold"]),
  field("config.disk.cleanupEnabled", "field.dmCleanupEnabled", "section.disk", ["automatic cleanup"]),
  field("config.disk.cooldown", "field.dmCooldown", "section.disk", ["cleanup cooldown"]),
  field("config.disk.forceDays", "field.dmForceDays", "section.disk", ["forced cleanup"]),
  field("config.disk.vacuum", "field.dmVacuum", "section.disk", ["journald", "logs"]),

  // ── Paths ────────────────────────────────────────────────────────────────
  section("config.paths", "section.paths", ["directories", "mounts"]),
  field("config.paths.extraBindRoots", "field.extraBindRoots", "section.paths", ["mounts", "sandbox paths"]),
  field("config.paths.archetypeDirs", "field.archetypeDirs", "section.paths", ["templates", "experts"]),
];
