// Schedules are standard cron: 0 or 7 = Sunday, 1-5 = Monday to Friday, names
// work too. The calendar, the weekday picker and the runtime must agree, or a
// task shows on one day and runs on another (the runtime used to count from
// Monday = 0, and a task written "1-5" ran Tuesday to Saturday).
import assert from "node:assert/strict";
import { test } from "node:test";

import { cronOccursOn, parseCron } from "../calendar/cron.ts";
import { defaultRecurrence, expandWeekdays, scheduleToSpec, specToSchedule } from "./recurrence.ts";

const SUNDAY = new Date(2026, 10, 22);
const MONDAY = new Date(2026, 10, 23);
const SATURDAY = new Date(2026, 10, 21);

test("the calendar reads 1-5 as Monday to Friday", () => {
  const weekdays = parseCron("0 8 * * 1-5")!;
  assert.equal(cronOccursOn(weekdays, MONDAY), true);
  assert.equal(cronOccursOn(weekdays, SATURDAY), false);
  assert.equal(cronOccursOn(weekdays, SUNDAY), false);
});

test("0 and 7 are both Sunday, and names mean themselves", () => {
  for (const expr of ["0 8 * * 0", "0 8 * * 7", "0 8 * * sun"]) {
    const f = parseCron(expr)!;
    assert.equal(cronOccursOn(f, SUNDAY), true, expr);
    assert.equal(cronOccursOn(f, MONDAY), false, expr);
  }
  const named = parseCron("0 8 * * mon-fri")!;
  assert.equal(cronOccursOn(named, MONDAY), true);
  assert.equal(cronOccursOn(named, SATURDAY), false);
});

test("the builder opens standard cron in the weekday picker", () => {
  // Picker indexes are Monday-first: 0=Mon .. 6=Sun.
  assert.equal(expandWeekdays("1-5"), "0,1,2,3,4");
  assert.equal(expandWeekdays("mon-fri"), "0,1,2,3,4");
  assert.equal(expandWeekdays("0"), "6");
  assert.equal(expandWeekdays("7"), "6");
  assert.equal(expandWeekdays("MON,thu"), "0,3");
  assert.equal(expandWeekdays("*/2"), "*/2");
  const spec = scheduleToSpec("30 9 * * 1-5", null);
  assert.equal(spec.freq, "weekly");
  assert.deepEqual(spec.weekdays, [0, 1, 2, 3, 4]);
});

test("the builder writes weekday names and reads them back", () => {
  const spec = { ...defaultRecurrence(), freq: "weekly" as const, hour: 9, minute: 0, weekdays: [6, 0, 4] };
  const { schedule } = specToSchedule(spec);
  assert.equal(schedule, "0 9 * * mon,fri,sun");
  assert.deepEqual(scheduleToSpec(schedule, null).weekdays, [0, 4, 6]);
});
