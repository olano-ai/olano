// Per-agent chat preferences (mode + detail), persisted to localStorage so a
// user's chat setup survives reloads. Mirrors the old dashboard's
// `state.chatPrefs` (key `lc.chatPrefs`), but scoped per agent.
//
// The model and reasoning-effort overrides are deliberately NOT here any more:
// they are per conversation (`Session.model` / `Session.reasoningEffort` in
// sessionStore.ts, seeded from the thread on open), so a pick in one chat never
// leaks into the next and a new chat always opens on the agent's default. A
// `model` / `reasoning_effort` left in storage by an older build is ignored.

export type ChatMode = "auto" | "plan" | "manual" | "act";

// How much tool/subagent activity the conversation renders:
//   quiet   → no tool calls at all (just the agent's prose, todos, approvals)
//   normal  → a compact activity line per tool call (the default)
//   verbose → full tool input/output cards + subagent transcripts
export type ChatDetail = "quiet" | "normal" | "verbose";

export interface ChatPrefs {
  mode: ChatMode;
  detail: ChatDetail;
}

export const CHAT_MODES: ChatMode[] = ["auto", "plan", "manual", "act"];
export const CHAT_DETAILS: ChatDetail[] = ["quiet", "normal", "verbose"];

const DEFAULT: ChatPrefs = { mode: "auto", detail: "normal" };
const KEY = "lc.chatPrefs";

// Stored prefs may be a legacy shape (boolean `verbose` instead of `detail`,
// a per-agent `model` / `reasoning_effort` override) left by an older build;
// keep them readable and drop what no longer applies.
type StoredPrefs = Partial<ChatPrefs> & { verbose?: boolean; model?: string; reasoning_effort?: string };
type Store = Record<string, StoredPrefs>;

// Map a stored entry onto the current shape, migrating the legacy `verbose`
// boolean to a `detail` mode when no explicit detail is present.
function migrate(saved: StoredPrefs | undefined): Partial<ChatPrefs> {
  if (!saved) return {};
  const { verbose, model: _model, reasoning_effort: _effort, ...rest } = saved;
  if (rest.detail == null && typeof verbose === "boolean") {
    return { ...rest, detail: verbose ? "verbose" : "normal" };
  }
  return rest;
}

function readStore(): Store {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Store) : {};
  } catch {
    return {};
  }
}

function writeStore(store: Store): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(store));
  } catch {
    // storage disabled / quota — keep working in-memory only.
  }
}

export function getPrefs(agent: string): ChatPrefs {
  if (!agent) return { ...DEFAULT };
  return { ...DEFAULT, ...migrate(readStore()[agent]) };
}

export function savePrefs(agent: string, prefs: ChatPrefs): void {
  if (!agent) return;
  const store = readStore();
  store[agent] = prefs;
  writeStore(store);
}

// Cycle auto → plan → manual → act → auto.
export function nextMode(mode: ChatMode): ChatMode {
  const i = CHAT_MODES.indexOf(mode);
  return CHAT_MODES[(i + 1) % CHAT_MODES.length];
}

// Cycle quiet → normal → verbose → quiet.
export function nextDetail(detail: ChatDetail): ChatDetail {
  const i = CHAT_DETAILS.indexOf(detail);
  return CHAT_DETAILS[(i + 1) % CHAT_DETAILS.length];
}
