import assert from "node:assert/strict";
import { test } from "node:test";

import { viewSizeChanged, viewSizeFor } from "./viewSize.ts";

test("the frame wanted is the picture in device pixels, more when zoomed", () => {
  assert.deepEqual(viewSizeFor({ w: 390, h: 244 }, 3), { w: 1170, h: 732 });
  assert.deepEqual(viewSizeFor({ w: 390, h: 244 }, 3, 2), { w: 2340, h: 1464 });
  assert.deepEqual(viewSizeFor({ w: 390, h: 244 }, 0), { w: 390, h: 244 });
  assert.equal(viewSizeFor({ w: 0, h: 244 }, 3), null);
});

test("a change of a few pixels is not reported, a real one is", () => {
  const last = { w: 1170, h: 732 };
  assert.equal(viewSizeChanged(null, last), true);
  assert.equal(viewSizeChanged(last, { w: 1180, h: 740 }), false);
  assert.equal(viewSizeChanged(last, { w: 1400, h: 732 }), true);
  assert.equal(viewSizeChanged(last, null), false);
});
