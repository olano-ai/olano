// Log-line parsing + level inference, lifted verbatim from the classic
// dashboard's `inferLevel` / `formatLogLine`. Lines are produced by the API's
// log handler with the format:
//   "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
// e.g. "2026-05-06 12:34:56,123 [ERROR] olano.runtime: boom".

export type LogLevel = "info" | "warn" | "error" | "debug";
export type LevelFilter = "all" | LogLevel;

export const LEVELS: LevelFilter[] = ["all", "info", "warn", "error", "debug"];

// Mirror of the original inferLevel: WARNING and WARN both map to "warn".
export function inferLevel(line: string): LogLevel {
  if (line.includes("[ERROR]")) return "error";
  if (line.includes("[WARNING]") || line.includes("[WARN]")) return "warn";
  if (line.includes("[DEBUG]")) return "debug";
  return "info";
}

// Pull "12:34:56" (time only, comma-millis stripped) and "name: message" out of
// "2026-05-06 12:34:56,123 [LEVEL] name: message". Falls back to the raw line
// with an empty timestamp when the shape doesn't match — same as the original.
export function formatLogLine(line: string): { ts: string; rest: string } {
  const m = /^(\S+\s\S+)\s\[(\w+)\]\s([^:]+):\s?(.*)$/.exec(line);
  if (!m) return { ts: "", rest: line };
  const ts = m[1].split(" ")[1]?.split(",")[0] ?? "";
  return { ts, rest: `${m[3]}: ${m[4]}` };
}

// Level badge colors, matching the classic .log-line__lvl--* rules. Token
// values (info-soft / warn-soft / danger-soft / surface-3) come from tokens.css.
export const LEVEL_BADGE: Record<LogLevel, { bg: string; fg: string }> = {
  info: { bg: "var(--info-soft)", fg: "var(--info)" },
  warn: { bg: "var(--warn-soft)", fg: "var(--warn)" },
  error: { bg: "var(--danger-soft)", fg: "var(--danger)" },
  debug: { bg: "var(--surface-3)", fg: "var(--text-3)" },
};

// Split `text` into alternating non-match / match segments for the active
// search query (case-insensitive). Returns one segment per chunk with a flag so
// the renderer can wrap matches in a highlight. An empty/whitespace query (or no
// hit) yields a single non-match segment — i.e. the original string untouched.
export interface HighlightSegment {
  text: string;
  match: boolean;
}

export function highlightSegments(text: string, query: string): HighlightSegment[] {
  const q = query.trim();
  if (!q) return [{ text, match: false }];
  const needle = q.toLowerCase();
  const hay = text.toLowerCase();
  const out: HighlightSegment[] = [];
  let from = 0;
  let idx = hay.indexOf(needle, from);
  if (idx === -1) return [{ text, match: false }];
  while (idx !== -1) {
    if (idx > from) out.push({ text: text.slice(from, idx), match: false });
    out.push({ text: text.slice(idx, idx + needle.length), match: true });
    from = idx + needle.length;
    idx = hay.indexOf(needle, from);
  }
  if (from < text.length) out.push({ text: text.slice(from), match: false });
  return out;
}
