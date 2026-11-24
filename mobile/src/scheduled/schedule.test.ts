import assert from "node:assert/strict";
import { test } from "node:test";

import type { ScheduledTask } from "../shared/dashboard.ts";
import { byAgent, isFinished, nameFromPrompt, nextText, translate, whenText } from "./schedule.ts";

function task(over: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: "t1",
    name: "Morning brief",
    prompt: "Summarise my inbox",
    schedule: "0 9 * * *",
    run_at: null,
    timezone: "UTC",
    enabled: true,
    agent_name: "merli",
    created_at: "2026-10-01T00:00:00Z",
    last_run: null,
    next_run: "2026-10-18T09:00:00+00:00",
    run_count: 0,
    visibility: "private",
    created_by: "merli",
    ...over,
  };
}

test("a schedule reads as a sentence", () => {
  assert.equal(whenText(task()), "Every day at 09:00");
  assert.equal(whenText(task({ schedule: "*/15 * * * *" })), "Every 15 minutes");
  assert.equal(whenText(task({ schedule: "30 * * * *" })), "Every hour at :30");
  assert.equal(whenText(task({ schedule: "0 8 1 * *" })), "On day 1 of every month at 08:00");
  assert.equal(whenText(task({ schedule: "0 8 1-7 * 1" })), "Custom schedule (0 8 1-7 * 1)");
  assert.match(whenText(task({ schedule: null, run_at: "2026-12-24T18:30:00" })), /^Once, on .+ at 18:30$/);
  assert.equal(translate("sb.sum.everyMin", { n: "5" }), "Every 5 minutes");
});

test("the status line says paused, finished or when it runs next", () => {
  const fmt = (iso: string) => `<${iso}>`;
  const now = Date.parse("2026-10-17T12:00:00Z");
  assert.equal(nextText(task({ enabled: false }), fmt, now), "Paused");
  assert.equal(nextText(task(), fmt, now), "Next: <2026-10-18T09:00:00+00:00>");
  const once = task({ schedule: null, run_at: "2026-10-01T09:00:00", next_run: null });
  assert.equal(isFinished(once, now), true);
  assert.equal(nextText(once, fmt, now), "Finished");
  assert.equal(isFinished(task({ schedule: null, run_at: "2026-11-01T09:00:00", next_run: null }), now), false);
});

test("tasks are grouped by agent, tasks with no agent last", () => {
  const groups = byAgent([
    task({ id: "a", agent_name: "zed", name: "B" }),
    task({ id: "b", agent_name: null, name: "X" }),
    task({ id: "c", agent_name: "amy", name: "Z" }),
    task({ id: "d", agent_name: "zed", name: "A" }),
  ]);
  assert.deepEqual(
    groups.map((g) => [g.agent, g.tasks.map((t) => t.id)]),
    [
      ["amy", ["c"]],
      ["zed", ["d", "a"]],
      ["", ["b"]],
    ],
  );
});

test("a task without a name is named from its first line", () => {
  assert.equal(nameFromPrompt("  Check the build\nand tell me"), "Check the build");
  assert.equal(
    nameFromPrompt("Summarise every unread email from customers and draft replies for the urgent ones"),
    "Summarise every unread email from customers and…",
  );
});
