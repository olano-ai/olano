import { strict as assert } from "node:assert";
import { test } from "node:test";

import { ago, listTime, upcomingTime } from "./time.ts";

const NOW = new Date(2026, 9, 16, 15, 0, 0).getTime();
const HOUR = 60 * 60 * 1000;

test("listTime: a time today, Yesterday, a weekday, then a date", () => {
  assert.match(listTime(NOW - 2 * HOUR, NOW), /\d/);
  assert.equal(listTime(NOW - 20 * HOUR, NOW), "Yesterday");
  assert.equal(listTime(NOW - 3 * 24 * HOUR, NOW), new Date(NOW - 3 * 24 * HOUR).toLocaleDateString(undefined, { weekday: "long" }));
  assert.equal(listTime("", NOW), "");
  assert.equal(listTime("not a date", NOW), "");
});

test("ago: recent things in minutes and hours", () => {
  assert.equal(ago(NOW - 10 * 1000, NOW), "just now");
  assert.equal(ago(NOW - 5 * 60 * 1000, NOW), "5 min ago");
  assert.equal(ago(NOW - 3 * HOUR, NOW), "3 h ago");
  assert.equal(ago(NOW - 26 * HOUR, NOW), "yesterday");
});

import { dayLabel, sameDay } from "./time.ts";

test("dayLabel: Today, Yesterday, a weekday, then a date", () => {
  assert.equal(dayLabel(NOW - HOUR, NOW), "Today");
  assert.equal(dayLabel(NOW - 20 * HOUR, NOW), "Yesterday");
  assert.equal(
    dayLabel(NOW - 3 * 24 * HOUR, NOW),
    new Date(NOW - 3 * 24 * HOUR).toLocaleDateString(undefined, { weekday: "long" }),
  );
  assert.equal(dayLabel(undefined, NOW), "");
  assert.ok(sameDay(NOW, NOW - HOUR));
  assert.ok(!sameDay(NOW, NOW - 20 * HOUR));
});

test("an upcoming time says today, tomorrow, a weekday, then a date", () => {
  const now = new Date(2026, 9, 17, 12, 0).getTime(); // Saturday
  assert.match(upcomingTime(new Date(2026, 9, 17, 18, 0).getTime(), now), /^today at /);
  assert.match(upcomingTime(new Date(2026, 9, 18, 9, 0).getTime(), now), /^tomorrow at /);
  assert.match(upcomingTime(new Date(2026, 9, 21, 9, 0).getTime(), now), /^Wednesday at /);
  assert.match(upcomingTime(new Date(2026, 11, 24, 9, 0).getTime(), now), /^Dec 24 at /);
  assert.equal(upcomingTime("", now), "");
});
