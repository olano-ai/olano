// Friendly recurrence model — the bridge between the no-cron-knowledge UI and
// the backend's storage format (a 5-field cron string OR a one-time run_at).
//
// The backend (olano/scheduler/cron.py) reads the cron string as standard cron
// and parses run_at with datetime.fromisoformat, so three rules drive
// everything here:
//   1. Cron is "minute hour day-of-month month day-of-week" (exactly 5 fields,
//      minute resolution — there is deliberately NO seconds field).
//   2. day-of-week is standard cron (0 or 7 = Sun, 1 = Mon) or names. The
//      builder writes names; the picker and ./cron.ts read both.
//   3. run_at is a NAIVE local wall-clock "YYYY-MM-DDTHH:MM:00"; the backend
//      attaches the chosen timezone. It must be zero-padded or fromisoformat
//      raises (so the task would be stored but never scheduled).
//
// These functions are pure and dependency-free so they can be reasoned about
// (and round-trip tested) in isolation from React.

export type Freq = "minutes" | "hourly" | "daily" | "weekly" | "monthly" | "once" | "advanced";

// Every field for every mode lives on one struct; only the fields relevant to
// `freq` are read by specToSchedule(). The builder keeps the rest populated
// with sensible values so switching modes never lands on an empty control.
export interface Recurrence {
  freq: Freq;
  everyMinutes: number; // minutes mode — interval, 1..59
  everyHours: number; // hourly mode — interval, 1..23
  everyDays: number; // daily mode — interval, 1..31
  atMinute: number; // hourly mode — minute past the hour, 0..59
  hour: number; // daily/weekly/monthly time, 0..23
  minute: number; // daily/weekly/monthly time, 0..59
  weekdays: number[]; // weekly mode — picker index, Monday first (0=Mon..6=Sun)
  dayOfMonth: number; // monthly mode — 1..31
  date: string; // once mode — "YYYY-MM-DD"
  time: string; // once mode — "HH:MM"
  cron: string; // advanced mode — raw 5-field cron
}

export const WEEKDAY_COUNT = 7;

export function clamp(n: number, lo: number, hi: number): number {
  if (!Number.isFinite(n)) return lo;
  return Math.min(hi, Math.max(lo, Math.round(n)));
}

const pad = (n: number): string => String(n).padStart(2, "0");

export function todayLocal(): string {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// A blank-slate spec: "every day at 09:00", the most common scheduled task.
export function defaultRecurrence(): Recurrence {
  return {
    freq: "daily",
    everyMinutes: 15,
    everyHours: 1,
    everyDays: 1,
    atMinute: 0,
    hour: 9,
    minute: 0,
    weekdays: [0], // Monday
    dayOfMonth: 1,
    date: todayLocal(),
    time: "09:00",
    cron: "",
  };
}

// ── spec → backend (cron string OR run_at) ──────────────────────────────────
export function specToSchedule(r: Recurrence): { schedule: string; run_at: string } {
  switch (r.freq) {
    case "minutes": {
      const n = clamp(r.everyMinutes, 1, 59);
      return { schedule: `*/${n} * * * *`, run_at: "" };
    }
    case "hourly": {
      const n = clamp(r.everyHours, 1, 23);
      const m = clamp(r.atMinute, 0, 59);
      const hourField = n === 1 ? "*" : `*/${n}`;
      return { schedule: `${m} ${hourField} * * *`, run_at: "" };
    }
    case "daily": {
      const n = clamp(r.everyDays, 1, 31);
      const dom = n === 1 ? "*" : `*/${n}`;
      return { schedule: `${clamp(r.minute, 0, 59)} ${clamp(r.hour, 0, 23)} ${dom} * *`, run_at: "" };
    }
    case "weekly": {
      const days = (r.weekdays.length ? Array.from(new Set(r.weekdays)) : [0]).sort((a, b) => a - b);
      // Written as names: they read the same to a person, a model and every
      // version of the runtime, where a number depends on where the week starts.
      const names = days.filter((d) => d >= 0 && d <= 6).map((d) => DOW_NAMES[d]);
      return { schedule: `${clamp(r.minute, 0, 59)} ${clamp(r.hour, 0, 23)} * * ${names.join(",")}`, run_at: "" };
    }
    case "monthly": {
      const d = clamp(r.dayOfMonth, 1, 31);
      return { schedule: `${clamp(r.minute, 0, 59)} ${clamp(r.hour, 0, 23)} ${d} * *`, run_at: "" };
    }
    case "once": {
      const time = /^\d{1,2}:\d{2}$/.test(r.time) ? r.time : "09:00";
      const [h, mi] = time.split(":");
      return { schedule: "", run_at: r.date ? `${r.date}T${pad(+h)}:${mi}:00` : "" };
    }
    case "advanced":
      return { schedule: r.cron.trim(), run_at: "" };
  }
}

// ── backend → spec (best-effort recognition) ─────────────────────────────────
// Recognizes everything specToSchedule() can emit, plus the common hand-written
// shapes; anything else falls back to "advanced" with the raw cron preserved so
// editing an arbitrary existing task is lossless.

type Tok =
  | { kind: "wild" }
  | { kind: "step"; n: number }
  | { kind: "num"; v: number }
  | { kind: "list"; vals: number[] }
  | { kind: "other" };

// A day-of-week field in standard cron (names or numbers, 0 or 7 = Sunday,
// plain ranges and lists: "mon-fri", "1-5", "mon,thu") as the list of picker
// indexes (0=Mon..6=Sun) the weekday picker understands. Anything else is
// returned unchanged and lands in the Advanced field.
const DOW_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

export function expandWeekdays(field: string): string {
  const numeric = field.trim().replace(/[a-z]{3}/gi, (name) => {
    const index = DOW_NAMES.indexOf(name.toLowerCase());
    return index === -1 ? name : String((index + 1) % 7);
  });
  if (!/^\d(-\d)?(,\d(-\d)?)*$/.test(numeric)) return field;
  const days: number[] = [];
  for (const piece of numeric.split(",")) {
    const [a, b] = piece.split("-").map((v) => parseInt(v, 10));
    if (a > 7 || (b ?? 0) > 7) return field;
    if (b !== undefined && b < a) return field;
    for (let d = a; d <= (b ?? a); d++) days.push((d + 6) % 7);
  }
  return Array.from(new Set(days)).sort((x, y) => x - y).join(",");
}

function tok(field: string): Tok {
  const f = field.trim();
  if (f === "*" || f === "?") return { kind: "wild" };
  const step = /^\*\/(\d+)$/.exec(f);
  if (step) return { kind: "step", n: parseInt(step[1], 10) };
  if (/^\d+$/.test(f)) return { kind: "num", v: parseInt(f, 10) };
  if (/^\d+(,\d+)+$/.test(f)) return { kind: "list", vals: f.split(",").map((s) => parseInt(s, 10)) };
  return { kind: "other" };
}

// Parse "YYYY-M-D[T ]H:M[:S]" literally (NOT via Date(), which would re-zone the
// wall-clock). Returns zero-padded parts the native date/time inputs accept.
export function splitRunAt(iso: string | null | undefined): { date: string; time: string } {
  if (!iso) return { date: "", time: "" };
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2})/.exec(iso.trim());
  if (!m) return { date: "", time: "" };
  const [, y, mo, d, h, mi] = m;
  return { date: `${y}-${pad(+mo)}-${pad(+d)}`, time: `${pad(+h)}:${mi}` };
}

export function scheduleToSpec(schedule: string | null | undefined, runAt: string | null | undefined): Recurrence {
  const base = defaultRecurrence();
  const sched = (schedule || "").trim();

  // One-time task: no cron, just a run_at.
  if (!sched) {
    const { date, time } = splitRunAt(runAt);
    if (date) return { ...base, freq: "once", date, time: time || "09:00" };
    // Truly empty → keep the daily default for a fresh create.
    return runAt ? { ...base, freq: "once" } : base;
  }

  const parts = sched.split(/\s+/);
  if (parts.length !== 5) return { ...base, freq: "advanced", cron: sched };
  const [Mi, Ho, Dm, Mo] = parts.map(tok);
  const Dw = tok(expandWeekdays(parts[4]));
  const wild = (t: Tok) => t.kind === "wild";

  // every N minutes:  */N * * * *
  if (Mi.kind === "step" && wild(Ho) && wild(Dm) && wild(Mo) && wild(Dw)) {
    return { ...base, freq: "minutes", everyMinutes: clamp(Mi.n, 1, 59) };
  }
  // every N hours at :M:  M (*/N|*) * * *
  if (Mi.kind === "num" && (Ho.kind === "step" || wild(Ho)) && wild(Dm) && wild(Mo) && wild(Dw)) {
    return {
      ...base,
      freq: "hourly",
      atMinute: clamp(Mi.v, 0, 59),
      everyHours: Ho.kind === "step" ? clamp(Ho.n, 1, 23) : 1,
    };
  }
  // every N days at H:M:  M H (*/N|*) * *
  if (Mi.kind === "num" && Ho.kind === "num" && (Dm.kind === "step" || wild(Dm)) && wild(Mo) && wild(Dw)) {
    return {
      ...base,
      freq: "daily",
      minute: clamp(Mi.v, 0, 59),
      hour: clamp(Ho.v, 0, 23),
      everyDays: Dm.kind === "step" ? clamp(Dm.n, 1, 31) : 1,
    };
  }
  // weekly on weekdays at H:M:  M H * * (num|list)
  if (Mi.kind === "num" && Ho.kind === "num" && wild(Dm) && wild(Mo) && (Dw.kind === "num" || Dw.kind === "list")) {
    const days = (Dw.kind === "num" ? [Dw.v] : Dw.vals).filter((d) => d >= 0 && d <= 6);
    return { ...base, freq: "weekly", minute: clamp(Mi.v, 0, 59), hour: clamp(Ho.v, 0, 23), weekdays: days.length ? days : [0] };
  }
  // monthly on day D at H:M:  M H D * *
  if (Mi.kind === "num" && Ho.kind === "num" && Dm.kind === "num" && wild(Mo) && wild(Dw)) {
    return { ...base, freq: "monthly", minute: clamp(Mi.v, 0, 59), hour: clamp(Ho.v, 0, 23), dayOfMonth: clamp(Dm.v, 1, 31) };
  }
  // Unrecognized → keep the raw cron for power users.
  return { ...base, freq: "advanced", cron: sched };
}

// ── timezone helpers ─────────────────────────────────────────────────────────
const FALLBACK_ZONES = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Sao_Paulo",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Madrid",
  "Europe/Moscow",
  "Africa/Cairo",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Asia/Singapore",
  "Australia/Sydney",
];

// The IANA zone list. Intl.supportedValuesOf is widely available (2022+) but not
// in the ES2022 type lib, so it's reached through a cast + guarded at runtime.
export function listTimeZones(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  let zones: string[] = FALLBACK_ZONES;
  try {
    if (typeof intl.supportedValuesOf === "function") {
      const got = intl.supportedValuesOf("timeZone");
      if (Array.isArray(got) && got.length) zones = got;
    }
  } catch {
    /* keep fallback */
  }
  // The runtime defaults to "UTC"; make sure it's always offered even if the
  // platform list omits it.
  return zones.includes("UTC") ? zones : ["UTC", ...zones];
}

export function guessTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

// Current UTC offset of a zone as a short label, e.g. "GMT-4". Display only.
export function tzOffsetLabel(tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" }).formatToParts(
      new Date(),
    );
    return parts.find((p) => p.type === "timeZoneName")?.value ?? "";
  } catch {
    return "";
  }
}

// ── human-readable summary ───────────────────────────────────────────────────
export type Translate = (key: string, vars?: Record<string, string>) => string;

// Locale weekday name from a picker index (0=Mon..6=Sun). Uses a known
// Monday (2024-01-01) as the anchor so it needs no per-language string table.
export function weekdayName(apsDow: number, long = false): string {
  const d = new Date(2024, 0, 1 + apsDow); // Jan 1 2024 is a Monday
  return d.toLocaleDateString(undefined, { weekday: long ? "long" : "short" });
}

function fmtTime(hour: number, minute: number): string {
  return `${pad(clamp(hour, 0, 23))}:${pad(clamp(minute, 0, 59))}`;
}

export function describeRecurrence(r: Recurrence, t: Translate): string {
  switch (r.freq) {
    case "minutes": {
      const n = clamp(r.everyMinutes, 1, 59);
      return n === 1 ? t("sb.sum.everyMin1") : t("sb.sum.everyMin", { n: String(n) });
    }
    case "hourly": {
      const n = clamp(r.everyHours, 1, 23);
      const m = pad(clamp(r.atMinute, 0, 59));
      return n === 1 ? t("sb.sum.everyHour1", { m }) : t("sb.sum.everyHour", { n: String(n), m });
    }
    case "daily": {
      const n = clamp(r.everyDays, 1, 31);
      const time = fmtTime(r.hour, r.minute);
      return n === 1 ? t("sb.sum.everyDay1", { t: time }) : t("sb.sum.everyDay", { n: String(n), t: time });
    }
    case "weekly": {
      const days = (r.weekdays.length ? Array.from(new Set(r.weekdays)) : [0])
        .sort((a, b) => a - b)
        .map((d) => weekdayName(d))
        .join(", ");
      return t("sb.sum.weekly", { days, t: fmtTime(r.hour, r.minute) });
    }
    case "monthly":
      return t("sb.sum.monthly", { d: String(clamp(r.dayOfMonth, 1, 31)), t: fmtTime(r.hour, r.minute) });
    case "once": {
      if (!r.date) return t("sb.sum.invalid");
      const time = /^\d{1,2}:\d{2}$/.test(r.time) ? r.time : "09:00";
      const [y, mo, d] = r.date.split("-").map(Number);
      const label = new Date(y, (mo || 1) - 1, d || 1).toLocaleDateString(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
        year: "numeric",
      });
      return t("sb.sum.once", { date: label, t: time });
    }
    case "advanced":
      return r.cron.trim() ? t("sb.sum.advanced", { cron: r.cron.trim() }) : t("sb.sum.invalid");
  }
}

// Validity gate so the modal can block submit before the backend rejects it.
export function isRecurrenceValid(r: Recurrence): boolean {
  if (r.freq === "once") return !!r.date && /^\d{1,2}:\d{2}$/.test(r.time);
  if (r.freq === "advanced") return r.cron.trim().split(/\s+/).length === 5;
  if (r.freq === "weekly") return r.weekdays.length > 0;
  return true;
}

// ── cadence guard ───────────────────────────────────────────────────────────
// A scheduled task is a full agent turn: model calls, tool calls, MCP traffic —
// commonly 10-30s, and billed. Below this the next firing routinely lands while
// the previous run is still going, and APScheduler DROPS it (one run of a task
// at a time, by design). A `* * * * *` task in production spent an hour
// dropping runs while looking healthy (2026-08-14) — the warning exists so the
// cadence is a choice rather than a discovery.
export const CADENCE_WARN_BELOW_MINUTES = 5;

/**
 * The interval between firings in minutes, for recurring schedules that repeat
 * within the hour. `null` when the schedule is slower than hourly, one-off, or
 * too irregular to reduce to a single number — none of which need the warning.
 */
export function cadenceMinutes(r: Recurrence): number | null {
  if (r.freq === "minutes") return clamp(r.everyMinutes, 1, 59);
  if (r.freq === "hourly") return clamp(r.everyHours, 1, 23) * 60;
  if (r.freq !== "advanced") return null;

  const parts = r.cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour] = parts;
  // Only an every-hour cron can fire sub-hourly; anything pinning the hour
  // repeats at most once an hour.
  if (hour !== "*") return null;
  if (minute === "*") return 1;
  const step = /^\*\/(\d{1,2})$/.exec(minute);
  if (step) {
    const n = Number(step[1]);
    return n >= 1 && n <= 59 ? n : null;
  }
  // An explicit list ("0,30") fires at those minutes; the tightest gap is what
  // matters, so use the smallest difference between consecutive entries.
  if (/^\d{1,2}(,\d{1,2})+$/.test(minute)) {
    const mins = minute
      .split(",")
      .map(Number)
      .filter((n) => n >= 0 && n <= 59)
      .sort((a, b) => a - b);
    if (mins.length < 2) return null;
    let tightest = 60 - mins[mins.length - 1] + mins[0]; // wrap past the hour
    for (let i = 1; i < mins.length; i++) tightest = Math.min(tightest, mins[i] - mins[i - 1]);
    return tightest;
  }
  return null;
}

/** Whether this schedule fires too fast for a comfortable agent turn. */
export function isCadenceTooFrequent(r: Recurrence): boolean {
  const mins = cadenceMinutes(r);
  return mins !== null && mins < CADENCE_WARN_BELOW_MINUTES;
}
