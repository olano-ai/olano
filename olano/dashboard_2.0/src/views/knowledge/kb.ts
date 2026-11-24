// KB helpers — talk to the backend through the synthetic-tool-call convention
// the original dashboard used: POST /api/chat with `message` set to a kb_* tool
// invocation string, parsing the agent's plain-text reply. There are no
// dedicated KB REST endpoints; every capability is a tool the agent runs.
//
// Because the call is just a chat message, an agent WITHOUT the kb_* tools does
// not fail — its model reads `kb_search("birdhouse", …)` as a sentence and
// answers in prose ("I don't have a kb_search tool connected in this chat"),
// which then renders as if it were a search result. `kb_*` are not universal
// defaults (see olano/config/ops_defaults.py:UNIVERSAL_AGENT_TOOL_NAMES); they
// bind only when an agent's own `tools:` list names them, so most agents cannot
// serve this page at all. `fetchKBAvailability` is how the view finds that out
// BEFORE dispatching, so the user gets a fixable message instead of a shrug.

import { api } from "../../core/api";
import type { Agent } from "../../core/types";

// Shape returned by the non-streaming POST /api/chat endpoint.
export interface ChatResponse {
  response: string;
  thread_id: string;
  agent: string;
}

// One global SSE event frame ({ type, data, ts }). We only ever look at
// chat_response frames to live-refresh after a tool runs in another tab.
export interface StreamEvent {
  type?: string;
  data?: { agent?: string; thread_id?: string; response?: string };
  ts?: string;
}

// Built-in store names plus the "all" pseudo-store. Custom stores discovered
// from kb_list are appended at runtime.
export const BUILTIN_STORES = ["all", "knowledgebase", "workspace", "memory"] as const;
export type SearchMode = "similarity" | "mmr";

// A single source row parsed out of kb_list's text output.
export interface KBSource {
  store: string;
  id: string; // file path or URL
}

// Per-store stats line parsed from kb_list ("[store]  N sources · M chunks").
export interface KBStoreStats {
  store: string;
  sources: number;
  chunks: number;
}

export interface KBListing {
  empty: boolean;
  stats: KBStoreStats[];
  sources: KBSource[];
  raw: string;
}

/** Escape a JS string so it can be embedded inside a double-quoted tool-call
 *  argument (e.g. kb_search("…")). Mirrors the original dashboard's escaping. */
export function quote(s: string): string {
  return s.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

/** Resolve which agent should run a KB tool call: the selected agent if it is
 *  running, otherwise the first running agent. Null when none are available. */
export function pickKBAgent(agents: Agent[], selected: string): Agent | null {
  const chosen = agents.find((a) => a.name === selected);
  if (chosen && chosen.running) return chosen;
  return agents.find((a) => a.running) ?? null;
}

// One console thread per agent, minted on first use and kept for the life of
// the page. Omitting `thread_id` makes the backend mint a fresh UUID per call
// (runtime/auth.py:namespace_thread_id), so every search, list and delete used
// to leave its own checkpoint row and JSONL transcript behind — each one then
// showing up in the agent's Chat thread list. Reuse keeps that to one thread
// per agent per visit, and page-scoped (not persisted) so the thread can never
// grow across visits into a context the agent re-reads on every KB call.
const consoleThreads = new Map<string, string>();

export function kbThreadId(agent: string): string {
  const known = consoleThreads.get(agent);
  if (known) return known;
  const rand =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2);
  const id = `kb-console-${rand}`;
  consoleThreads.set(agent, id);
  return id;
}

// Reusing a thread means the model can see the previous call and its output. A
// bare tool-call string would let it answer the next one from that history —
// serving a stale listing after a delete, which is exactly what a live console
// must never do. Naming the call as a call, every time, keeps each dispatch
// self-contained (and makes the tool call itself more likely than prose).
function toolCallPrompt(call: string): string {
  return `Run this tool call now and reply with its raw output only, no commentary:\n${call}`;
}

/** Run a kb_* tool call via the chat endpoint and return the agent's reply.
 *  Throws if the reply itself reports an error (kb_* tools return "Error: …"). */
export async function runKBTool(agent: string, call: string): Promise<ChatResponse> {
  const res = await api.post<ChatResponse>("/api/chat", {
    message: toolCallPrompt(call),
    agent,
    thread_id: kbThreadId(agent),
  });
  const body = (res.response ?? "").trim();
  if (/^Error[:\s]/i.test(body)) {
    throw new Error(body);
  }
  return res;
}

// ── tool availability ──────────────────────────────────────────────────────

// Every kb_* tool this page dispatches, and the action each one backs. The
// page is only fully usable by an agent that has all of them; the search tab
// alone needs just `kb_search`, so availability is reported per tool rather
// than as one all-or-nothing flag.
export const KB_TOOL_NAMES = [
  "kb_search",
  "kb_list",
  "kb_add_file",
  "kb_add_url",
  "kb_create_store",
  "kb_sync",
  "kb_delete",
  "kb_remove_file",
] as const;

export interface KBAvailability {
  agent: string;
  // Names from KB_TOOL_NAMES this agent can actually run.
  present: Set<string>;
  // Names it cannot — either absent from its `tools:` or globally switched off.
  missing: string[];
}

export function hasKBTool(av: KBAvailability | null, name: string): boolean {
  // Unknown (still loading, or the probe failed) is treated as available: the
  // page must not lock itself out over a probe it could not complete.
  return av === null || av.present.has(name);
}

/** Probe which kb_* tools an agent can run.
 *
 *  Two sources, because either one alone under-reports: `tools_resolved` on
 *  the agent detail (what the config grants, mirroring `whoami`) minus the
 *  home-wide tool switch (`registry.get_disabled_tools`, which strips names at
 *  boot without touching the config). The disabled list is best-effort — a
 *  member without access to it just gets the config-level answer. */
export async function fetchKBAvailability(agent: string): Promise<KBAvailability> {
  const detail = await api.cachedGet<{ tools_resolved?: string[] }>(
    `/api/agents/${encodeURIComponent(agent)}`,
  );
  let off = new Set<string>();
  try {
    const res = await api.cachedGet<{ disabled?: string[] }>("/api/catalog/tools/disabled");
    off = new Set(res?.disabled ?? []);
  } catch {
    // Best effort — fall through with the config-level answer.
  }
  const resolved = new Set((detail?.tools_resolved ?? []).filter((n) => !off.has(n)));
  const present = new Set<string>(KB_TOOL_NAMES.filter((n) => resolved.has(n)));
  return {
    agent,
    present,
    missing: KB_TOOL_NAMES.filter((n) => !present.has(n)),
  };
}

// ── parsers ────────────────────────────────────────────────────────────────
// kb_list emits one of:
//   "Knowledge base is empty."
//   "No sources in store '<x>'."
//   "Knowledge base contents:\n  [store]  N sources · M chunks\n      - id\n …"
//   "[store]  N sources · M chunks\n  - id\n …"   (single-store form)
// We tolerate both forms and recover a flat list of {store, id} rows.

const STAT_RE = /\[([^\]]+)\]\s+(\d+)\s+sources?\s+·\s+(\d+)\s+chunks?/;

export function parseKBList(raw: string): KBListing {
  const text = (raw ?? "").trim();
  if (!text || /knowledge base is empty/i.test(text) || /^no sources in store/i.test(text)) {
    return { empty: true, stats: [], sources: [], raw: text };
  }

  const stats: KBStoreStats[] = [];
  const sources: KBSource[] = [];
  let current = "knowledgebase";

  for (const lineRaw of text.split("\n")) {
    const line = lineRaw.replace(/\s+$/, "");
    const statMatch = STAT_RE.exec(line);
    if (statMatch) {
      current = statMatch[1];
      stats.push({
        store: statMatch[1],
        sources: Number(statMatch[2]) || 0,
        chunks: Number(statMatch[3]) || 0,
      });
      continue;
    }
    const bullet = /^\s*-\s+(.*)$/.exec(line);
    if (bullet && bullet[1].trim()) {
      sources.push({ store: current, id: bullet[1].trim() });
    }
  }

  return { empty: sources.length === 0 && stats.length === 0, stats, sources, raw: text };
}

/** Heuristic: does a source id look like a URL (vs a file path)? URLs are
 *  removed from the index with kb_delete; files can also be removed from disk
 *  with kb_remove_file. */
export function isUrlSource(id: string): boolean {
  return /^https?:\/\//i.test(id);
}

/** Short, human label for a source id (basename for files, host+path for URLs). */
export function sourceLabel(id: string): string {
  if (isUrlSource(id)) return id;
  const parts = id.split(/[/\\]/);
  return parts[parts.length - 1] || id;
}

/** Parse kb_search's "[N] store=… score=… \n source: … \n …content…" output
 *  into structured hits so the UI can render ranked cards. Falls back to an
 *  empty list (caller then shows the raw markdown). */
export interface SearchHit {
  index: number;
  store: string;
  score: string;
  source: string;
  content: string;
}

export function parseSearchResults(raw: string): SearchHit[] {
  const text = raw ?? "";
  if (/^no results found/i.test(text.trim())) return [];
  const hits: SearchHit[] = [];
  // Split on result headers like "[1] store=…".
  const blocks = text.split(/\n(?=\[\d+\]\s)/);
  for (const block of blocks) {
    const header = /^\[(\d+)\]\s+store=(\S+)\s+(score=[\d.]+|mmr)/.exec(block.trim());
    if (!header) continue;
    const sourceMatch = /source:\s*(.+)/.exec(block);
    // Content is everything after the "source:" line, de-indented.
    let content = "";
    const lines = block.split("\n");
    const srcIdx = lines.findIndex((l) => /source:/.test(l));
    if (srcIdx >= 0) {
      content = lines
        .slice(srcIdx + 1)
        .map((l) => l.replace(/^\s{0,5}/, ""))
        .join("\n")
        .trim();
    }
    hits.push({
      index: Number(header[1]) || hits.length + 1,
      store: header[2],
      score: header[3],
      source: (sourceMatch?.[1] || "unknown").trim(),
      content,
    });
  }
  return hits;
}
