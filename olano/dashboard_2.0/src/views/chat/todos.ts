// Parsing for the agent's `write_todos` tool. The tool call carries a todo
// list ([{content, status}, ...]); we surface it as a live checklist that
// updates in place as the agent flips items pending → in_progress → completed.

export type TodoStatus = "pending" | "in_progress" | "completed";

export interface TodoItem {
  content: string;
  status: TodoStatus;
}

// Once a turn has ended, a step left `in_progress` means the agent stopped
// working on it without reporting completion (smaller models often skip the
// final `write_todos` bookkeeping call). Show it as completed instead of a dot
// that reads as "stuck on step N". Not-started (`pending`) steps are left as-is
// so the count stays honest ("2/3 done") rather than over-reporting.
export function settleTodos(items: TodoItem[]): TodoItem[] {
  let changed = false;
  const out = items.map((it) => {
    if (it.status === "in_progress") {
      changed = true;
      return { ...it, status: "completed" as const };
    }
    return it;
  });
  return changed ? out : items;
}

function coerce(arr: unknown): TodoItem[] | null {
  if (!Array.isArray(arr) || arr.length === 0) return null;
  const out: TodoItem[] = [];
  for (const raw of arr) {
    if (!raw || typeof raw !== "object") continue;
    const obj = raw as Record<string, unknown>;
    const content = typeof obj.content === "string" ? obj.content : "";
    if (!content) continue;
    const status = obj.status === "completed" || obj.status === "in_progress" ? obj.status : "pending";
    out.push({ content, status });
  }
  return out.length ? out : null;
}

// Accepts either the tool-call args (`{"todos": [...]}` or a bare array) or the
// tool-result content (may embed a JSON array or a Python-repr dict list).
export function parseTodos(raw: string): TodoItem[] | null {
  if (!raw) return null;
  try {
    const obj = JSON.parse(raw);
    const direct = coerce(obj) ?? coerce((obj as { todos?: unknown })?.todos);
    if (direct) return direct;
  } catch {
    // not clean JSON — fall through to extraction
  }
  // Extract the first JSON-looking array from a noisier string.
  const m = raw.match(/\[[\s\S]*\]/);
  if (m) {
    try {
      const arr = coerce(JSON.parse(m[0]));
      if (arr) return arr;
    } catch {
      // fall through
    }
  }
  // Python-repr fallback: {'content': '…', 'status': '…'} dicts.
  const dictRe = /\{([^{}]+)\}/g;
  const todos: TodoItem[] = [];
  let dm: RegExpExecArray | null;
  while ((dm = dictRe.exec(raw)) !== null) {
    const item: Record<string, string> = {};
    const kvRe = /['"](\w+)['"]\s*:\s*['"]([^'"]*)['"]/g;
    let kv: RegExpExecArray | null;
    while ((kv = kvRe.exec(dm[1])) !== null) item[kv[1]] = kv[2];
    if (item.content && item.status) {
      const status = item.status === "completed" || item.status === "in_progress" ? item.status : "pending";
      todos.push({ content: item.content, status });
    }
  }
  return todos.length ? todos : null;
}
