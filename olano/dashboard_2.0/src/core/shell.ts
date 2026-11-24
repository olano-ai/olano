// Chat "!" shell mode — run a one-shot command server-side, capture the full
// output to a per-run log file, and keep only a head/tail preview in the chat.
// Results are buffered client-side and merged into the user's NEXT message (or
// sent on demand) so we never auto-spend agent context on raw shell output.

import { api } from "./api";

export interface ShellRunResult {
  id: string; // client-side handle (the buffer is keyed by it)
  command: string;
  exitCode: number | null;
  logPath: string | null; // workspace-relative; readable by the agent's fs tools
  head: string[];
  tail: string[];
  totalLines: number;
  totalBytes: number;
  truncated: boolean;
}

interface ShellRunWire {
  command: string;
  exit_code: number | null;
  log_path: string | null;
  head: string[];
  tail: string[];
  total_lines: number;
  total_bytes: number;
  truncated: boolean;
}

let SEQ = 0;

// Override how much head/tail the server returns. Defaults match the "!" preview
// (head/tail-25); a caller can pass e.g. { headLines: 0, tailLines: 100 } for a
// last-N-lines view. The backend clamps both budgets to 0–1000.
export interface ShellRunOpts {
  headLines?: number;
  tailLines?: number;
}

export async function runShell(
  command: string,
  agent: string,
  opts?: ShellRunOpts,
): Promise<ShellRunResult> {
  const r = await api.post<ShellRunWire>("/api/shell/run", {
    command,
    agent,
    head_lines: opts?.headLines,
    tail_lines: opts?.tailLines,
  });
  return {
    id: `sh${++SEQ}`,
    command: r.command,
    exitCode: r.exit_code,
    logPath: r.log_path,
    head: r.head || [],
    tail: r.tail || [],
    totalLines: r.total_lines ?? 0,
    totalBytes: r.total_bytes ?? 0,
    truncated: !!r.truncated,
  };
}

export function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// Pick a code-fence longer than the longest backtick run in the body so command
// output that itself contains ``` can't break out of the block.
function fenceFor(s: string): string {
  let max = 0;
  let cur = 0;
  for (const ch of s) {
    if (ch === "`") {
      cur += 1;
      if (cur > max) max = cur;
    } else {
      cur = 0;
    }
  }
  return "`".repeat(Math.max(3, max + 1));
}

// The in-chat block the user sees (a local-only "system" message). Explicitly
// labels the truncation so it's never mistaken for full output.
export function shellResultMarkdown(r: ShellRunResult): string {
  const meta =
    `\`${r.logPath ?? "(log unavailable)"}\` · ` + `${r.totalLines} lines / ${humanBytes(r.totalBytes)}`;
  let body: string;
  if (!r.truncated) {
    body = r.head.join("\n");
  } else {
    const omitted = r.totalLines - r.head.length - r.tail.length;
    body =
      `--- first ${r.head.length} of ${r.totalLines} lines (truncated) ---\n` +
      `${r.head.join("\n")}\n` +
      `[… ${omitted} lines omitted — full log: ${r.logPath} …]\n` +
      `--- last ${r.tail.length} of ${r.totalLines} lines (truncated) ---\n` +
      `${r.tail.join("\n")}`;
  }
  const fence = fenceFor(body);
  return `\`$ ${r.command}\`\n${meta}\n\n${fence}text\n${body}\n${fence}`;
}

// The combined message actually delivered to the agent: the user's prose first,
// then each buffered command + head/tail clearly delimited as context the user
// ran (not something the agent executed), with a pointer to the full log.
export function composeWithShell(userText: string, results: ShellRunResult[]): string {
  if (!results.length) return userText;
  const blocks = results
    .map((r) => {
      let body: string;
      if (!r.truncated) {
        body = r.head.join("\n");
      } else {
        const omitted = r.totalLines - r.head.length - r.tail.length;
        body =
          `${r.head.join("\n")}\n` +
          `[… ${omitted} lines omitted — full output saved to ${r.logPath} …]\n` +
          `${r.tail.join("\n")}`;
      }
      const head = `$ ${r.command}   (exit ${r.exitCode ?? "?"}, full output: ${r.logPath ?? "n/a"}, ${r.totalLines} lines)`;
      return `${head}\n${body}`;
    })
    .join("\n\n");
  const wrapped =
    `--- Shell session (commands I ran in the dashboard, not executed by you) ---\n` +
    `${blocks}\n` +
    `--- end shell session ---`;
  return userText ? `${userText}\n\n${wrapped}` : wrapped;
}
