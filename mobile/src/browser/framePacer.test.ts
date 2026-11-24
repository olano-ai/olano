import assert from "node:assert/strict";
import { test } from "node:test";

import { FramePacer } from "./framePacer.ts";

function harness() {
  const shown: number[] = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const pacer = new FramePacer<number>(
    (f) => shown.push(f),
    400,
    (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length as unknown as ReturnType<typeof setTimeout>;
    },
    (t) => {
      const i = (t as unknown as number) - 1;
      if (timers[i]) timers[i].fn = () => undefined;
    },
  );
  const fire = () => timers.splice(0).forEach((t) => t.fn());
  return { pacer, shown, fire };
}

test("one frame at a time, the newest of those that waited", () => {
  const { pacer, shown } = harness();
  pacer.offer(1);
  pacer.offer(2);
  pacer.offer(3);
  assert.deepEqual(shown, [1]);
  pacer.shown();
  assert.deepEqual(shown, [1, 3]);
  pacer.shown();
  pacer.shown();
  assert.deepEqual(shown, [1, 3]);
  pacer.offer(4);
  assert.deepEqual(shown, [1, 3, 4]);
});

test("a picture that never says it drew is not waited on forever", () => {
  const { pacer, shown, fire } = harness();
  pacer.offer(1);
  pacer.offer(2);
  fire();
  assert.deepEqual(shown, [1, 2]);
});

test("reset drops what was waiting", () => {
  const { pacer, shown } = harness();
  pacer.offer(1);
  pacer.offer(2);
  pacer.reset();
  pacer.shown();
  assert.deepEqual(shown, [1]);
  pacer.offer(3);
  assert.deepEqual(shown, [1, 3]);
});
