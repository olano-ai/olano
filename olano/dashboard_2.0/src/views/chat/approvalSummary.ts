// A tool approval in plain words, for the simple shell (and anyone else who
// should not have to read a tool name and a JSON blob to decide).
//
// The card asks a person "may the agent do this?", and in the simple shell
// that person is often not technical: `gmail_send` and `{"to": ...}` answer
// what the agent will run, not what it will do. This turns the tool name and
// its arguments into one sentence ("Send an email") and, where the arguments
// carry it, the one detail the decision turns on ("To: ana@example.com"). The
// raw name and arguments stay one click away.
//
// Pure (no React, no i18n lookups): it returns string keys the card
// translates, so node's --test can drive it.

export type SayKey =
  | "run"
  | "writeFile"
  | "editFile"
  | "delete"
  | "email"
  | "post"
  | "pay"
  | "secretRead"
  | "secretWrite"
  | "peer"
  | "calendar"
  | "schedule"
  | "settings"
  | "generic";

export type TargetKey = "command" | "file" | "to" | "url" | "agent" | "subject" | "name";

export interface ApprovalSummary {
  /** `approval.say.<say>` names what the agent wants to do. */
  say: SayKey;
  /** The tool, readable ("Quickbooks create invoice"), for the generic
   *  sentence and the service a post goes to. */
  tool: string;
  /** The first word of the tool name, capitalized, for "post" ("Slack"). */
  service: string;
  /** The one argument the decision turns on, when the call carries it. */
  target?: { key: TargetKey; value: string };
}

/** `server__tool`, `mcp_server.tool` → `tool`, lowercased. */
export function bareToolName(name: string): string {
  const n = (name || "").trim().toLowerCase();
  const parts = n.split(/__|\./);
  return parts[parts.length - 1] || n;
}

function words(name: string): string[] {
  return name.split(/[_\-\s]+/).filter(Boolean);
}

function readable(name: string): string {
  const w = words(name).join(" ");
  return w ? w.charAt(0).toUpperCase() + w.slice(1) : "";
}

function argsObject(raw: unknown): Record<string, unknown> {
  if (raw && typeof raw === "object" && !Array.isArray(raw)) return raw as Record<string, unknown>;
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
    } catch {
      // Not JSON: no named arguments to read.
    }
  }
  return {};
}

// Which argument names carry the detail, in the order they are worth showing.
const TARGET_ARGS: [TargetKey, string[]][] = [
  ["command", ["command", "cmd", "script"]],
  ["file", ["file_path", "path", "filename", "file"]],
  ["to", ["to", "recipient", "recipients", "email", "channel", "chat_id"]],
  ["agent", ["agent_name", "agent", "remote_agent"]],
  ["url", ["url", "endpoint"]],
  ["subject", ["subject", "title"]],
  ["name", ["name"]],
];

function targetOf(args: Record<string, unknown>): ApprovalSummary["target"] {
  for (const [key, names] of TARGET_ARGS) {
    for (const n of names) {
      const v = args[n];
      if (typeof v === "string" && v.trim()) return { key, value: v.trim() };
      if (Array.isArray(v) && v.length && v.every((x) => typeof x === "string")) {
        return { key, value: (v as string[]).join(", ") };
      }
    }
  }
  return undefined;
}

function sayFor(n: string): SayKey {
  const w = words(n);
  const has = (...xs: string[]) => xs.some((x) => w.includes(x));
  if (/^(execute|shell|bash|safe_shell|run_shell|run_command|terminal|node_run|npm_install)$/.test(n)) return "run";
  if (n === "write_file") return "writeFile";
  if (n === "edit_file") return "editFile";
  if (has("wallet", "pay", "payment", "transfer") || n === "quickbooks_record_payment") return "pay";
  if (n === "secret_get" || n.endsWith("_get_webhook_secret")) return "secretRead";
  if (n === "secret_set" || n === "magic_link_request_secret") return "secretWrite";
  if (has("delete", "remove", "void", "unfollow")) return "delete";
  if (n === "talk_to_agent" || n === "talk_to" || n === "a2a_send_message") return "peer";
  if (has("mail", "gmail") || (has("send", "reply", "forward") && has("draft"))) return "email";
  if (has("calendar", "event")) return "calendar";
  if (has("scheduler", "schedule")) return "schedule";
  if (n.startsWith("self_") || n.startsWith("olano_")) return "settings";
  if (has("send", "post", "tweet", "comment", "reply", "dm", "message", "publish", "share", "invitation")) return "post";
  return "generic";
}

export function summarizeApproval(name: string | undefined, rawArgs: unknown): ApprovalSummary {
  const n = bareToolName(name || "");
  const args = argsObject(rawArgs);
  const first = words(n)[0] || "";
  return {
    say: sayFor(n),
    tool: readable(n) || "tool",
    service: first ? first.charAt(0).toUpperCase() + first.slice(1) : "",
    target: targetOf(args),
  };
}
