import { test } from "node:test";
import assert from "node:assert/strict";
import { countBrowsingCalls, dockHidden, isBrowsingCall, rebaseDismissal } from "./dockActivity.ts";

test("only calls that drive the page count as browsing", () => {
  assert.equal(isBrowsingCall("browser_navigate"), true);
  assert.equal(isBrowsingCall("browser_click"), true);
  assert.equal(isBrowsingCall("browser_close"), false);
  assert.equal(isBrowsingCall("browser_get_auto_approvals"), false);
  assert.equal(isBrowsingCall("execute"), false);
  assert.equal(isBrowsingCall("read_file"), false);
});

test("the count spans every message of the conversation", () => {
  const msgs = [
    { tools: [{ name: "execute" }, { name: "browser_navigate" }] },
    {},
    { tools: [{ name: "browser_navigate" }, { name: "browser_click" }, { name: "browser_close" }] },
  ];
  assert.equal(countBrowsingCalls(msgs), 3);
  assert.equal(countBrowsingCalls([]), 0);
});

test("a hidden dock stays hidden until the agent browses again in the same session", () => {
  const d = { openedAt: 100, calls: 3 };
  assert.equal(dockHidden(null, 100, 3), false);
  assert.equal(dockHidden(d, 100, 3), true);
  // The next question: the agent navigates again in the session it kept open.
  assert.equal(dockHidden(d, 100, 4), false);
  // A new session always shows.
  assert.equal(dockHidden(d, 200, 3), false);
});

test("a new chat rebases the count so its first browsing call brings the dock back", () => {
  const d = { openedAt: 100, calls: 7 };
  const rebased = rebaseDismissal(d, 0);
  assert.deepEqual(rebased, { openedAt: 100, calls: 0 });
  assert.equal(dockHidden(rebased, 100, 0), true);
  assert.equal(dockHidden(rebased, 100, 1), false);
  // Nothing to do when the count holds or grows.
  assert.equal(rebaseDismissal(d, 7), d);
  assert.equal(rebaseDismissal(d, 9), d);
  assert.equal(rebaseDismissal(null, 0), null);
});
