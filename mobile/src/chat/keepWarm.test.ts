import assert from "node:assert/strict";
import { test } from "node:test";

import { KeepWarm } from "./keepWarm.ts";

function fakeTimers() {
  const queue: { fn: () => void; at: number; id: number }[] = [];
  let now = 0;
  let next = 1;
  return {
    schedule: (fn: () => void, ms: number) => {
      const id = next++;
      queue.push({ fn, at: now + ms, id });
      return id as unknown as ReturnType<typeof setTimeout>;
    },
    cancel: (timer: ReturnType<typeof setTimeout>) => {
      const i = queue.findIndex((q) => q.id === (timer as unknown as number));
      if (i >= 0) queue.splice(i, 1);
    },
    advance: (ms: number) => {
      now += ms;
      for (const due of queue.filter((q) => q.at <= now).sort((a, b) => a.at - b.at)) {
        queue.splice(queue.indexOf(due), 1);
        due.fn();
      }
    },
  };
}

test("released after the delay, not before", () => {
  const t = fakeTimers();
  let released = 0;
  const warm = new KeepWarm(() => released++, 1000, t.schedule, t.cancel);
  warm.releaseSoon();
  assert.equal(warm.pending, true);
  t.advance(999);
  assert.equal(released, 0);
  t.advance(1);
  assert.equal(released, 1);
  assert.equal(warm.pending, false);
});

test("a hold before the delay keeps it; the next releaseSoon starts over", () => {
  const t = fakeTimers();
  let released = 0;
  const warm = new KeepWarm(() => released++, 1000, t.schedule, t.cancel);
  warm.releaseSoon();
  t.advance(800);
  warm.hold();
  t.advance(500);
  assert.equal(released, 0);
  warm.releaseSoon();
  t.advance(999);
  assert.equal(released, 0);
  t.advance(1);
  assert.equal(released, 1);
});

test("releaseNow releases once and cancels a pending release", () => {
  const t = fakeTimers();
  let released = 0;
  const warm = new KeepWarm(() => released++, 1000, t.schedule, t.cancel);
  warm.releaseSoon();
  warm.releaseNow();
  assert.equal(released, 1);
  t.advance(2000);
  assert.equal(released, 1);
});
