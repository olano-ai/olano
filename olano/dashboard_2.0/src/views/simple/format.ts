// Names and clocks — the two things every surface in this shell formats, and
// the two it must format the SAME way, or a rail row and the message it points
// at disagree about when something happened.
//
// Everything here is pure and takes the translator in, so a string it prints is
// a key in strings.ts like any other and there is no second dictionary hiding
// in a date helper.

import type { Lang } from "../../core/i18n";

/** Two letters from a name, for the human's own avatar chip. */
export function initials(name: string): string {
  const parts = (name || "?").split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0]?.[0] || "?") + (parts[1]?.[0] || "")).toUpperCase();
}

// The shell's three languages as BCP-47 tags, so Intl formats dates in the
// language the reader picked rather than the browser's. Kept as an explicit map
// (not `lang` passed straight through) because "zh" alone leaves the region —
// and therefore the date order — up to the engine.
const LOCALES: Record<string, string> = { en: "en-GB", zh: "zh-CN", es: "es-ES" };

function locale(lang: Lang | string): string {
  return LOCALES[String(lang)] ?? "en-GB";
}

const DAY_MS = 86_400_000;

/** Midnight-to-midnight distance in days: 0 today, 1 yesterday, … */
function daysApart(ts: number, now: number): number {
  const a = new Date(ts);
  const b = new Date(now);
  a.setHours(0, 0, 0, 0);
  b.setHours(0, 0, 0, 0);
  return Math.round((b.getTime() - a.getTime()) / DAY_MS);
}

/** "14:39" — the clock alone, in the reader's language. */
export function clockTime(ts: number, lang: Lang | string): string {
  return new Date(ts).toLocaleTimeString(locale(lang), { hour: "2-digit", minute: "2-digit" });
}

/**
 * The right-aligned stamp on a rail row: a clock today, "Yesterday" yesterday,
 * a weekday within the week, a short date beyond it. Exactly the ladder a
 * messenger's contact list walks, so a glance at the column reads as recency
 * rather than as a wall of identical timestamps.
 */
export function railTime(
  ts: number,
  now: number,
  lang: Lang | string,
  t: (key: string) => string,
): string {
  if (!ts) return "";
  const days = daysApart(ts, now);
  if (days <= 0) return clockTime(ts, lang);
  if (days === 1) return t("time.yesterday");
  if (days < 7) return new Date(ts).toLocaleDateString(locale(lang), { weekday: "short" });
  return new Date(ts).toLocaleDateString(locale(lang), { day: "2-digit", month: "2-digit" });
}

/**
 * The centred divider between two blocks of conversation: "Today 14:39",
 * "Yesterday 09:12", "Mon 12 Mar 18:04". It names the day AND the clock,
 * because the thing a reader is placing is "when did this exchange happen",
 * and a bare weekday leaves that half unanswered.
 */
export function daySeparator(
  ts: number,
  now: number,
  lang: Lang | string,
  t: (key: string) => string,
): string {
  const days = daysApart(ts, now);
  const clock = clockTime(ts, lang);
  if (days <= 0) return `${t("time.today")} ${clock}`;
  if (days === 1) return `${t("time.yesterday")} ${clock}`;
  const date = new Date(ts).toLocaleDateString(locale(lang), {
    weekday: days < 7 ? "short" : undefined,
    day: "numeric",
    month: "short",
    year: days > 300 ? "numeric" : undefined,
  });
  return `${date} ${clock}`;
}

/** Full stamp for a `title` attribute — never abbreviated, never relative. */
export function absoluteTime(ts: number, lang: Lang | string): string {
  return new Date(ts).toLocaleString(locale(lang), {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/**
 * "just now" → "5 mins ago" → "2 hours ago" → an absolute stamp past a day.
 * `now` is threaded in from one shared minute tick, so no message owns a timer.
 */
export function relativeTime(
  ts: number,
  now: number,
  lang: Lang | string,
  t: (key: string, vars?: Record<string, string>) => string,
): string {
  const sec = Math.floor((now - ts) / 1000);
  if (sec < 60) return t("msg.now");
  const min = Math.floor(sec / 60);
  if (min < 60) return t(min === 1 ? "msg.minOne" : "msg.minMany", { n: String(min) });
  const hr = Math.floor(min / 60);
  if (hr < 24) return t(hr === 1 ? "msg.hourOne" : "msg.hourMany", { n: String(hr) });
  return absoluteTime(ts, lang);
}

/** Epoch ms from an ISO stamp, 0 when it is missing or unparseable. */
export function parseTs(iso: string | null | undefined): number {
  if (!iso) return 0;
  const n = Date.parse(iso);
  return Number.isNaN(n) ? 0 : n;
}

/**
 * A transcript author as a person would say it.
 *
 * The backend writes humans as canonical principals (`human-owner`), which is
 * an identity string and not a name — nobody in a chat is called "human-owner".
 * Bots keep their own name, which IS what they are called everywhere else.
 */
export function authorLabel(
  author: string,
  kind: "human" | "agent" | "system",
  you: string,
  mine = true,
): string {
  if (kind !== "human") return author;
  if (mine) return you;
  // Another person's turn: their slug, readably. "human-carlos" → "carlos".
  // Labelling every human "You" was the bug — in a group an admin can read,
  // the other humans are exactly the people who must NOT be "You".
  const slug = author.startsWith("human-") ? author.slice("human-".length) : author;
  return slug || you;
}

/** One line of preview text: collapsed whitespace, hard-capped. */
export function previewText(body: string, max = 140): string {
  const flat = (body || "").replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}
