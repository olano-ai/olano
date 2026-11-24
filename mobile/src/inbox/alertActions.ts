/**
 * What the person can do about an alert from the app.
 *
 * The deployment's alerts carry a fix for the dashboard (`action.href`, a
 * dashboard route such as `/agents/nova?tab=mcp`), and the app has no
 * dashboard pages. Most of those fixes are things an agent can do for the
 * person (reconnect a service with a magic link, re-authorize an MCP server),
 * so the app offers to ask the agent; an issue opens in the app; a skill-name
 * mismatch is repaired in place by an admin; what only the dashboard can fix
 * says so instead of offering a button that cannot work.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

export interface AlertLike {
  kind: string;
  title: string;
  body?: string;
  agent?: string | null;
  dedupe_key?: string | null;
  action?: { label?: string; href?: string } | null;
}

export type AlertAction =
  | { kind: "issue"; id: number }
  | { kind: "repair_skills"; agent: string | null }
  | { kind: "ask_agent"; agent: string; draft: string }
  | { kind: "cortex"; agent: string | null }
  | { kind: "dashboard"; label: string };

/** Alerts an agent can fix for the person with its own tools. */
const AGENT_FIXABLE = new Set(["channel_credential_missing", "mcp_auth_required", "gmail_watch", "cognition_worker_failed"]);

/** The issue an alert is about: its link, or the dedupe key of an older row without one. */
export function issueOf(alert: AlertLike): number | null {
  const href = alert.action?.href ?? "";
  const fromHref = /^\/issues\?(?:.*&)?issue=(\d+)/.exec(href);
  if (fromHref) return Number(fromHref[1]);
  const fromKey = /^issue-(\d+)-/.exec(alert.dedupe_key ?? "");
  return fromKey ? Number(fromKey[1]) : null;
}

/** The agent an alert is about: its own field, else the `agent=` or `/agents/<name>` of its link. */
export function agentOf(alert: AlertLike): string | null {
  if (alert.agent) return alert.agent;
  const href = alert.action?.href ?? "";
  const query = /[?&]agent=([^&#]+)/.exec(href);
  if (query) return decode(query[1]);
  const path = /^\/agents\/([^/?#]+)/.exec(href);
  return path ? decode(path[1]) : null;
}

function decode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** The message that asks the agent to fix it, for the person to send (or change first). */
export function fixDraft(alert: AlertLike): string {
  const body = (alert.body ?? "").trim();
  return `The deployment raised this alert: "${alert.title}".${body ? `\n\n${body}` : ""}\n\nCan you fix it, or tell me what you need from me?`;
}

/** What the app offers for an alert, most useful first. */
export function actionsFor(alert: AlertLike, opts: { isAdmin: boolean }): AlertAction[] {
  const out: AlertAction[] = [];
  const issue = issueOf(alert);
  if (issue !== null) out.push({ kind: "issue", id: issue });
  const agent = agentOf(alert);
  if (alert.kind === "skills_spec_violation" && opts.isAdmin) out.push({ kind: "repair_skills", agent });
  if (alert.kind === "cognition_worker_failed" || (alert.action?.href ?? "").startsWith("/cortex")) {
    out.push({ kind: "cortex", agent });
  }
  if (agent && issue === null && (AGENT_FIXABLE.has(alert.kind) || alert.kind === "skills_spec_violation" || !alert.action?.href)) {
    out.push({ kind: "ask_agent", agent, draft: fixDraft(alert) });
  }
  if (!out.length && alert.action?.href) out.push({ kind: "dashboard", label: alert.action.label || "the dashboard" });
  return out;
}
