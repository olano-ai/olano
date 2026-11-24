/**
 * Scheduled tasks as the app shows them: when a task runs, in words, and
 * the state of its last and next run. The schedule itself is the dashboard's
 * recurrence model (a 5-field cron or a one-time `run_at`), shared through
 * src/shared/dashboard.ts so both build the same schedules.
 *
 * Pure: no DOM, no network, so `node --test` covers it.
 */

import { describeRecurrence, scheduleToSpec, type Recurrence, type ScheduledTask, type TaskRun } from "../shared/dashboard.ts";

/** The dashboard's summary sentences (views/scheduler/strings.ts, English). */
const SUMMARY: Record<string, string> = {
  "sb.sum.everyMin": "Every {n} minutes",
  "sb.sum.everyMin1": "Every minute",
  "sb.sum.everyHour": "Every {n} hours at :{m}",
  "sb.sum.everyHour1": "Every hour at :{m}",
  "sb.sum.everyDay": "Every {n} days at {t}",
  "sb.sum.everyDay1": "Every day at {t}",
  "sb.sum.weekly": "Every week on {days} at {t}",
  "sb.sum.monthly": "On day {d} of every month at {t}",
  "sb.sum.once": "Once, on {date} at {t}",
  "sb.sum.advanced": "Custom schedule ({cron})",
  "sb.sum.invalid": "Choose when this task should run",
};

export function translate(key: string, vars: Record<string, string> = {}): string {
  const text = SUMMARY[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (_, name: string) => vars[name] ?? "");
}

export function describe(recurrence: Recurrence): string {
  return describeRecurrence(recurrence, translate);
}

/** When the task runs, in words ("Every day at 09:00"). */
export function whenText(task: Pick<ScheduledTask, "schedule" | "run_at">): string {
  return describe(scheduleToSpec(task.schedule, task.run_at));
}

/** A one-time task whose time has passed: it ran (or was missed) and will not run again. */
export function isFinished(task: Pick<ScheduledTask, "schedule" | "run_at" | "next_run">, now = Date.now()): boolean {
  if (task.schedule) return false;
  if (task.next_run) return false;
  const at = task.run_at ? Date.parse(task.run_at) : NaN;
  return Number.isFinite(at) ? at < now : false;
}

/** The task's status line: paused, finished, or when it runs next. */
export function nextText(task: ScheduledTask, formatTime: (iso: string) => string, now = Date.now()): string {
  if (!task.enabled) return "Paused";
  if (isFinished(task, now)) return "Finished";
  if (task.next_run) return `Next: ${formatTime(task.next_run)}`;
  return "Waiting to be scheduled";
}

/** When a run happened (older rows say `timestamp`). */
export function runTime(run: TaskRun): string {
  return run.ran_at || run.timestamp || "";
}

export function runFailed(run: TaskRun): boolean {
  return !!(run.error && run.error.trim());
}

/** Tasks grouped by the agent that runs them, agents in name order, tasks by name. */
export function byAgent(tasks: readonly ScheduledTask[]): { agent: string; tasks: ScheduledTask[] }[] {
  const groups = new Map<string, ScheduledTask[]>();
  for (const task of tasks) {
    const key = task.agent_name || "";
    groups.set(key, [...(groups.get(key) ?? []), task]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (a === "" ? 1 : b === "" ? -1 : a.localeCompare(b)))
    .map(([agent, list]) => ({ agent, tasks: list.sort((x, y) => (x.name || "").localeCompare(y.name || "")) }));
}

/** A name for a new task from its instructions, when the person gave none. */
export function nameFromPrompt(prompt: string): string {
  const first = prompt.trim().split(/\n/)[0].replace(/\s+/g, " ");
  if (first.length <= 48) return first;
  const cut = first.slice(0, 48);
  const space = cut.lastIndexOf(" ");
  return `${(space > 24 ? cut.slice(0, space) : cut).replace(/[\s,.;:]+$/, "")}…`;
}
