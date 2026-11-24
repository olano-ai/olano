// Minimal 5-field cron evaluation for the Calendar view. The Scheduler stores
// recurring tasks as standard "minute hour day-of-month month day-of-week" cron
// strings; to paint them on a month grid we need to know which DAYS a task fires
// (and a representative time chip), not the exact backend next_run. This file is
// purposely small and dependency-free: it answers "does this cron fire on this
// date?" and "what time should the chip show?" — enough for a calendar.
//
// Day-of-week is standard cron, as the runtime reads it: 0 or 7 = Sunday,
// 1 = Monday … 6 = Saturday, and three-letter names ("mon-fri") work too.
// When BOTH day-of-month and day-of-week are
// restricted, a day matches if EITHER field matches; otherwise the restricted
// field must match and the wildcard one is ignored.

export interface CronFields {
  minute: Set<number> | null; // null = wildcard ("*")
  hour: Set<number> | null;
  dom: Set<number> | null;
  month: Set<number> | null;
  dow: Set<number> | null;
}

// Parse one field into the set of allowed values, or null for a wildcard.
// Supports "*", "*/n", "a-b", "a-b/n", "a,b,c" and any comma-combination.
// Returns null (treat as wildcard) when a piece is unparseable so a malformed
// task degrades to "shows every day" rather than vanishing silently.
function parseField(expr: string, min: number, max: number): Set<number> | null {
  const trimmed = expr.trim();
  if (trimmed === "*" || trimmed === "?" || trimmed === "") return null;
  const out = new Set<number>();
  for (const raw of trimmed.split(",")) {
    const piece = raw.trim();
    if (!piece) continue;
    let range = piece;
    let step = 1;
    const slash = piece.indexOf("/");
    if (slash !== -1) {
      range = piece.slice(0, slash);
      const parsed = parseInt(piece.slice(slash + 1), 10);
      if (Number.isFinite(parsed) && parsed > 0) step = parsed;
    }
    let lo = min;
    let hi = max;
    if (range === "*") {
      // "*/step" — keep full range, step applies below.
    } else if (range.includes("-")) {
      const [a, b] = range.split("-");
      lo = parseInt(a, 10);
      hi = parseInt(b, 10);
    } else {
      lo = hi = parseInt(range, 10);
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    if (lo > hi) [lo, hi] = [hi, lo];
    lo = Math.max(lo, min);
    hi = Math.min(hi, max);
    for (let v = lo; v <= hi; v += step) out.add(v);
  }
  return out.size ? out : null;
}

// Weekday names ("mon-fri", "mon,thu") as standard cron numbers (sun = 0).
const DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

export function dowNamesToNumbers(field: string): string {
  return field.replace(/[a-z]{3}/gi, (name) => {
    const index = DOW_NAMES.indexOf(name.toLowerCase());
    return index === -1 ? name : String(index);
  });
}

export function parseCron(expr: string | null | undefined): CronFields | null {
  if (!expr) return null;
  const parts = expr.trim().split(/\s+/);
  if (parts.length < 5) return null;
  const [mi, ho, dm, mo, dw] = parts;
  let dow = parseField(dowNamesToNumbers(dw), 0, 7);
  if (dow && dow.has(7)) {
    // 7 is cron's second spelling of Sunday.
    dow = new Set(dow);
    dow.delete(7);
    dow.add(0);
  }
  return {
    minute: parseField(mi, 0, 59),
    hour: parseField(ho, 0, 23),
    dom: parseField(dm, 1, 31),
    month: parseField(mo, 1, 12),
    dow,
  };
}

export function cronOccursOn(f: CronFields, date: Date): boolean {
  if (f.month && !f.month.has(date.getMonth() + 1)) return false;
  const domWild = f.dom === null;
  const dowWild = f.dow === null;
  const domMatch = domWild || f.dom!.has(date.getDate());
  // JS getDay() is already cron's numbering (0=Sun … 6=Sat).
  const dowMatch = dowWild || f.dow!.has(date.getDay());
  if (!domWild && !dowWild) return domMatch || dowMatch;
  return domMatch && dowMatch;
}

// Earliest fire time of the day, in minutes-since-midnight. Used to order events
// within a day and to render the time chip.
export function cronEarliestMinutes(f: CronFields): number {
  const h = f.hour === null ? 0 : Math.min(...f.hour);
  const m = f.minute === null ? 0 : Math.min(...f.minute);
  return h * 60 + m;
}

// True when the cron resolves to a single concrete fire time (one hour + one
// minute) — so the chip can show "09:00" instead of a recurring-time hint.
export function cronSingleTime(f: CronFields): boolean {
  return f.hour !== null && f.hour.size === 1 && f.minute !== null && f.minute.size === 1;
}

export function fmtMinutes(total: number): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(Math.floor(total / 60))}:${p(total % 60)}`;
}
