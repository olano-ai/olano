// Pure formatting + derivation helpers for the Scheduler view. Mirrors the
// fmtRel / fmtDateTime / agentColor helpers from the original dashboard.

import type { Task } from "./types";

// Translator function shape (matches the `t` returned by useT in core/i18n).
export type Translate = (key: string, vars?: Record<string, string>) => string;

// Relative time. Handles both past ("5m ago") and future ("in 5m") so the
// "Next run" column reads naturally — the original only formatted the past.
// Takes a translator so the words are localized; the numeric label is built
// the same way in every language.
export function fmtRel(iso: string | null | undefined, t: Translate): string {
  if (!iso) return "—";
  const dt = new Date(iso);
  const ms = dt.getTime();
  if (Number.isNaN(ms)) return iso;
  const diff = ms - Date.now();
  const future = diff > 0;
  const s = Math.abs(diff) / 1000;
  if (s < 60) return future ? t("rel.inMoment") : t("rel.justNow");
  let label: string;
  if (s < 3600) label = `${Math.floor(s / 60)}m`;
  else if (s < 86400) label = `${Math.floor(s / 3600)}h`;
  else if (s < 604800) label = `${Math.floor(s / 86400)}d`;
  else return dt.toLocaleDateString();
  return future ? t("rel.in", { label }) : t("rel.ago", { label });
}

// Absolute, human-readable timestamp (matches the original fmtDateTime).
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return iso;
  return dt.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Fixed-width stamp used inside the history drawer (YYYY-MM-DD HH:MM:SS).
export function fmtStamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return String(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())} ` +
    `${p(dt.getHours())}:${p(dt.getMinutes())}:${p(dt.getSeconds())}`
  );
}

// A task is "recurring" when it has a non-empty cron expression.
export const isCronTask = (t: Task): boolean => !!(t.schedule && t.schedule.trim());

// Derived status used for the card badge + accent colour.
export type TaskStatus = "active" | "completed" | "paused";

export function taskStatus(t: Task): TaskStatus {
  if (t.enabled) return "active";
  if (!isCronTask(t) && (t.run_count || 0) > 0) return "completed";
  return "paused";
}

// Earliest upcoming run across ALL tasks that have a next_run (matches the
// original loadScheduler "Next run" stat, which did not filter by enabled).
export function nextUpcoming(tasks: Task[]): Task | null {
  let next: Task | null = null;
  let bestTs = Infinity;
  for (const t of tasks) {
    if (!t.next_run) continue;
    const ts = new Date(t.next_run).getTime();
    if (Number.isNaN(ts)) continue;
    if (ts < bestTs) {
      bestTs = ts;
      next = t;
    }
  }
  return next;
}
