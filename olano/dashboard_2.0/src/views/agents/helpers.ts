// Pure derivation + formatting helpers for the Agents view. Mirrors the
// statusBadge / backendLabel / channelIcon / setAgentEnvironment shorthand
// logic from the original dashboard, retargeted at the Aurora CSS variables.

import { createElement, type ReactNode } from "react";
import { Send, MessageSquare, Gamepad2, MessageCircle, Mail, Link2, Cable } from "lucide-react";
import type { Agent } from "../../core/types";
import type { NoteKeys } from "./Toggle";
import type { AgentConfig, BackendBlock, EnvShorthand, SkillScope } from "./types";

// Map a registry-derived status onto an Aurora status colour. Used for the
// badge dot + accent so running/error/paused read at a glance.
// Client-side mirror of the backend's agent-name rule (POST /api/agents).
// Import this instead of re-typing the literal (AgentForm + onboarding wizard).
export const AGENT_NAME_RE = /^[a-z0-9][a-z0-9_-]*$/;

// What to call an agent on screen. Client-side mirror of `AgentConfig.label`
// (olano/config/agent_config.py) — one definition, so a card, a chat header
// and an org-chart node can never disagree about an agent's name. Takes the
// loose shape because callers hold an `Agent`, an `AgentConfig`, or a bare
// row from a list endpoint.
export function agentLabel(agent: { name?: string; display_name?: string } | null | undefined): string {
  if (!agent) return "";
  return (agent.display_name || "").trim() || agent.name || "";
}

// Preview of the handle the server will mint from a display name, so the
// create forms can show "Hillary → hillary" as someone types. A PREVIEW, not
// the authority: the server derives the handle itself (olano/config/
// agent_names.py, the same rule character for character, plus a `-2`, `-3`
// suffix when the slug is taken) and the create response carries the real
// one. Empty for a name nothing survives of (all-CJK, all-punctuation) — the
// server then falls back to the archetype slug.
export const AGENT_NAME_MAX_LEN = 64;
export function slugifyAgentName(input: string): string {
  let slug = input
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[-_]+/, "")
    .replace(/-+$/, "");
  if (slug.length > AGENT_NAME_MAX_LEN) slug = slug.slice(0, AGENT_NAME_MAX_LEN).replace(/-+$/, "");
  return slug;
}

// Persist an agent's title (its position, `org.title`). Goes through the org
// endpoint rather than PUT /api/agents/{name}: organization.yaml shadows the
// agent.json5 block once the agent has a central entry, and the org PUT is
// the one writer that keeps the two from disagreeing. Reads the current entry
// first so the title change never clobbers manager/reports/messaging modes.
export async function saveAgentTitle(agentName: string, title: string): Promise<void> {
  const { api } = await import("../../core/api");
  const current = await api.get<{ org: Record<string, unknown> }>(
    `/api/agents/${encodeURIComponent(agentName)}/org`,
  );
  await api.put(`/api/agents/${encodeURIComponent(agentName)}/org`, {
    ...(current?.org ?? {}),
    title: title.trim() || null,
  });
}

// The public base URL of one agent's OpenAI-compatible API.
//
// `window.location.origin` is the right source and not a guess: every
// deployment shape serves the SPA and `/api/*` from ONE origin (the all-in-one
// Caddyfile, the host-native one and server.mjs all forward `/api/*`
// to the runtime and fall through to the SPA), which is exactly why the
// endpoint lives under `/api/` at all. So the origin the user is reading this
// page on is the origin their client must call.
//
// Mirrors `pageUrl` in PagesTab / `fullIngressUrl` in WebhooksTab, guard
// included: a non-browser render (SSR, a test harness) has no `window`, and a
// relative URL is still a true answer there.
export function agentApiBaseUrl(agentName: string): string {
  const path = `/api/openai/${encodeURIComponent(agentName)}/v1`;
  try {
    return `${window.location.origin}${path}`;
  } catch {
    return path;
  }
}

export function statusColor(status: string): string {
  switch (status) {
    case "running":
      return "var(--success)";
    case "error":
      return "var(--danger)";
    case "starting":
    case "stopping":
    case "paused":
    case "configured":
      return "var(--warn)";
    default:
      return "var(--text-3)";
  }
}

// Normalize the many shapes `Agent.status` can take into one label.
export function agentStatus(a: Pick<Agent, "status" | "running" | "paused">): string {
  return a.status || (a.paused ? "paused" : a.running ? "running" : "configured");
}

// Known status words have a localized label ("status.<word>"); anything else is
// shown verbatim. Callers resolve the returned value via t() (which falls back
// to the raw string for unknown statuses).
const KNOWN_STATUS = new Set(["running", "error", "starting", "stopping", "paused", "configured"]);
export function statusLabelKey(status: string): string {
  return KNOWN_STATUS.has(status) ? `status.${status}` : status;
}

// Resolve a backend block (or shorthand string) back to the friendly
// shorthand the Environment dropdown uses.
export function backendShorthand(b: AgentConfig["backend"]): EnvShorthand {
  if (!b) return "filesystem+shell";
  if (typeof b === "string") {
    if (b === "filesystem" || b === "read" || b === "state" || b === "store") return b;
    return "filesystem+shell";
  }
  const block = b as BackendBlock;
  if (block.type === "local_shell") return "filesystem+shell";
  if (block.type === "filesystem") return block.access_mode === "read" ? "read" : "filesystem";
  // Virtual (no real host filesystem) backends: "state" is thread-local/ephemeral,
  // "store" persists across threads via the shared KV store.
  if (block.type === "state") return "state";
  if (block.type === "store") return "store";
  return "filesystem+shell";
}

// Translation key for the head badge / environment summary. The component
// resolves it via t() so the label is localized.
export function backendLabelKey(b: AgentConfig["backend"]): string {
  switch (backendShorthand(b)) {
    case "filesystem+shell":
      return "env.execute";
    case "filesystem":
      return "env.write";
    case "read":
      return "env.read";
    case "state":
      return "env.sandbox";
    case "store":
      return "env.virtual";
  }
}

// What each Access / Environment choice actually grants, as (labelKey, textKey)
// pairs for <OptionNotes>. Shared: the same explanation shows under the picker
// in the create-agent form and under Access on an existing agent's Overview.
export const ENV_NOTES: NoteKeys[] = [
  ["env.execute", "env.executeDesc"],
  ["env.write", "env.writeDesc"],
  ["env.read", "env.readDesc"],
  ["env.sandbox", "env.sandboxDesc"],
  ["env.virtual", "env.virtualDesc"],
];

// Translation key for a pluralised count ("count.tool" / "count.tools"). The
// component resolves it via t("…", { n }) so number + noun localize together.
export function pluralKey(n: number, noun: "agent" | "subagent" | "tool" | "channel" | "issue"): string {
  return `count.${noun}${n === 1 ? "" : "s"}`;
}

// Channel glyph for connector rows. Flat lucide icons (monochrome, inherit
// currentColor) replace the old emoji so connectors read native to Aurora.
export function channelGlyph(type: string): ReactNode {
  const base = type.replace(/^platform:/, "");
  const map: Record<string, typeof Cable> = {
    telegram: Send,
    slack: MessageSquare,
    discord: Gamepad2,
    whatsapp: MessageCircle,
    email: Mail,
    imap: Mail,
    matrix: Link2,
  };
  const Icon = map[base] || Cable;
  return createElement(Icon, { size: 16, strokeWidth: 1.75 });
}

// Channel type → display label.
export function channelLabel(type: string): string {
  const bare = type.replace(/^platform:/, "");
  return bare
    .split(/[_:-]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

// Render a short "key=value · key=value" summary of a channel's config,
// skipping the discriminating `type` field. Returns the i18n key
// "channel.noConfig" (resolved by the caller via t()) when there is nothing to
// show; otherwise a verbatim technical summary string.
export function channelSummary(ch: ChannelObjectLike): string {
  const entries = Object.entries(ch).filter(([k]) => k !== "type");
  if (entries.length === 0) return "channel.noConfig";
  return entries
    .slice(0, 3)
    .map(([k, v]) => {
      const s = secretish(k) ? "••••••" : typeof v === "string" ? v : JSON.stringify(v);
      return `${k}=${s.length > 40 ? s.slice(0, 40) + "…" : s}`;
    })
    .join(" · ");
}

type ChannelObjectLike = Record<string, unknown>;

function secretish(key: string): boolean {
  return /token|secret|password|api[_-]?key/i.test(key);
}

// ── Skill scope ─────────────────────────────────────────────────────────────
// Translation keys for where a skill lives. The backend classifies the folder
// (olano/skills/browse.py); these two only name it, so the badge and the
// read-only explanation can never drift apart from each other.

export function scopeLabelKey(scope: SkillScope | null | undefined): string {
  switch (scope) {
    case "managed":
      return "skillView.scopeManaged";
    case "workspace":
      return "skillView.scopeWorkspace";
    case "learned":
      return "skillView.scopeLearned";
    case "configured":
      return "skillView.scopeConfigured";
    default:
      return "skillView.scopeUnknown";
  }
}

/** Why a skill in this scope is (or is not) editable from the dashboard. */
export function scopeHintKey(scope: SkillScope | null | undefined): string {
  switch (scope) {
    case "managed":
      return "skillView.hintManaged";
    case "workspace":
      return "skillView.hintWorkspace";
    case "learned":
      return "skillView.hintLearned";
    case "configured":
      return "skillView.hintConfigured";
    default:
      return "skillView.hintUnknown";
  }
}
