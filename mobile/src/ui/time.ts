/**
 * Timestamps the way a phone's message list shows them: a time today,
 * "Yesterday", a weekday within the week, then a date.
 *
 * Pure apart from the clock, which is injectable, so `node --test` covers it.
 */

export function listTime(input: string | number | undefined, now = Date.now()): string {
  if (input === undefined || input === "") return "";
  const ms = typeof input === "number" ? input : Date.parse(input);
  if (Number.isNaN(ms)) return "";
  const date = new Date(ms);
  const today = new Date(now);
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const day = 24 * 60 * 60 * 1000;
  if (ms >= startOfToday) return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (ms >= startOfToday - day) return "Yesterday";
  if (ms >= startOfToday - 6 * day) return date.toLocaleDateString(undefined, { weekday: "long" });
  if (date.getFullYear() === today.getFullYear()) return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** "2 min ago", for things that happened recently and matter because they are recent. */
export function ago(input: string | number | undefined, now = Date.now()): string {
  if (input === undefined || input === "") return "";
  const ms = typeof input === "number" ? input : Date.parse(input);
  if (Number.isNaN(ms)) return "";
  const seconds = Math.max(0, Math.round((now - ms) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return days === 1 ? "yesterday" : `${days} days ago`;
  return listTime(ms, now);
}

/** The label between a conversation's days: "Today", "Yesterday", a weekday within the week, then a date. */
export function dayLabel(input: number | undefined, now = Date.now()): string {
  if (input === undefined || Number.isNaN(input)) return "";
  const date = new Date(input);
  const today = new Date(now);
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const day = 24 * 60 * 60 * 1000;
  if (input >= startOfToday) return "Today";
  if (input >= startOfToday - day) return "Yesterday";
  if (input >= startOfToday - 6 * day) return date.toLocaleDateString(undefined, { weekday: "long" });
  if (date.getFullYear() === today.getFullYear()) {
    return date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** Whether two moments fall on the same calendar day here. */
export function sameDay(a: number, b: number): boolean {
  const x = new Date(a);
  const y = new Date(b);
  return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
}

/** "3:42 PM", for one message. */
export function clockTime(input: number | undefined): string {
  if (input === undefined || Number.isNaN(input)) return "";
  return new Date(input).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** When something will happen: "today at 9:00 AM", "tomorrow at…", a weekday within the week, then a date. */
export function upcomingTime(input: string | number | undefined, now = Date.now()): string {
  if (input === undefined || input === "") return "";
  const ms = typeof input === "number" ? input : Date.parse(input);
  if (Number.isNaN(ms)) return "";
  const date = new Date(ms);
  const today = new Date(now);
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const day = 24 * 60 * 60 * 1000;
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (ms < startOfToday) return listTime(ms, now);
  if (ms < startOfToday + day) return `today at ${time}`;
  if (ms < startOfToday + 2 * day) return `tomorrow at ${time}`;
  if (ms < startOfToday + 7 * day) return `${date.toLocaleDateString(undefined, { weekday: "long" })} at ${time}`;
  const sameYear = date.getFullYear() === today.getFullYear();
  const label = date.toLocaleDateString(undefined, sameYear ? { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" });
  return `${label} at ${time}`;
}
