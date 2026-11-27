import { test } from "node:test";
import assert from "node:assert/strict";
import { mergeActionRows, rowsFromActions, savedShape } from "./actionRows.ts";

test("a reload keeps a row added but not saved yet", () => {
  const prev = [
    { name: "my_order", script: "actions/my_order.py" },
    { name: "", script: "", draft: true },
  ];
  const next = mergeActionRows(prev, { my_order: "actions/my_order.py" });
  assert.equal(next.length, 2);
  assert.deepEqual(next[1], { name: "", script: "", draft: true });
});

test("a draft whose save landed is not shown twice", () => {
  const prev = [{ name: "refund", script: "actions/refund.py", draft: true }];
  const next = mergeActionRows(prev, { refund: "actions/refund.py" });
  assert.deepEqual(next, [{ name: "refund", script: "actions/refund.py" }]);
});

test("saved rows follow the server, so a removal elsewhere is reflected", () => {
  const prev = rowsFromActions({ a: "x.py", b: "y.py" });
  assert.deepEqual(mergeActionRows(prev, { a: "x.py" }), [{ name: "a", script: "x.py" }]);
});

test("an untouched empty row is not an unsaved change", () => {
  const rows = [{ name: "a", script: "x.py" }, { name: " ", script: "", draft: true }];
  assert.deepEqual(savedShape(rows), [{ name: "a", script: "x.py" }]);
});
