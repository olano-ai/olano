import { strict as assert } from "node:assert";
import { test } from "node:test";

import { addressFor, closedForGood, containSize, dismissKey, recordingClock, savedFileLine, scrollDelta, siteOf, statusLine, tapCount, toFraction } from "./model.ts";

test("the picture is fitted inside its box without cropping", () => {
  assert.deepEqual(containSize({ w: 390, h: 600 }, { w: 1280, h: 800 }), { w: 390, h: 244 });
  assert.deepEqual(containSize({ w: 390, h: 200 }, { w: 1280, h: 800 }), { w: 320, h: 200 });
  assert.deepEqual(containSize({ w: 390, h: 600 }, { w: 0, h: 0 }), { w: 0, h: 0 });
});

test("a touch becomes fractions of the picture; outside it, nothing", () => {
  const rect = { left: 10, top: 100, width: 400, height: 250 };
  assert.deepEqual(toFraction({ x: 10, y: 100 }, rect), { x: 0, y: 0 });
  assert.deepEqual(toFraction({ x: 210, y: 225 }, rect), { x: 0.5, y: 0.5 });
  assert.deepEqual(toFraction({ x: 410, y: 350 }, rect), { x: 1, y: 1 });
  assert.equal(toFraction({ x: 5, y: 200 }, rect), null);
  assert.equal(toFraction({ x: 200, y: 351 }, rect), null);
});

test("a swipe scrolls the page the way the finger moves it", () => {
  // Finger up 100 px on a 390 px picture of a 1280 px page: scroll down ~328 px.
  assert.equal(scrollDelta(-100, 390, 1280), 328);
  assert.equal(scrollDelta(50, 640, 1280), -100);
  assert.equal(scrollDelta(50, 0, 1280), 0);
});

test("quick taps in one place count as a double and a triple click", () => {
  assert.equal(tapCount(null, 1000, 10, 10), 1);
  assert.equal(tapCount({ at: 1000, x: 10, y: 10, count: 1 }, 1300, 14, 12), 2);
  assert.equal(tapCount({ at: 1000, x: 10, y: 10, count: 2 }, 1300, 14, 12), 3);
  assert.equal(tapCount({ at: 1000, x: 10, y: 10, count: 3 }, 1300, 14, 12), 3);
  assert.equal(tapCount({ at: 1000, x: 10, y: 10, count: 1 }, 1600, 10, 10), 1);
  assert.equal(tapCount({ at: 1000, x: 10, y: 10, count: 1 }, 1100, 60, 10), 1);
});

test("the status line says who has the browser, and what is waiting", () => {
  assert.deepEqual(statusLine("agent", null, false), { tone: "agent", text: "The agent is browsing" });
  assert.deepEqual(statusLine("user", null, false), { tone: "user", text: "You have control" });
  assert.deepEqual(statusLine("agent", "Sign in to the bank", false), { tone: "attention", text: "The agent needs you" });
  assert.equal(statusLine("agent", null, true).tone, "approval");
});

test("a socket closed because there is nothing to watch is not retried", () => {
  assert.ok(closedForGood(4404, "This agent has no open browser."));
  assert.ok(closedForGood(4403, ""));
  assert.ok(closedForGood(1000, ""));
  assert.ok(!closedForGood(1006, ""));
  assert.ok(!closedForGood(1011, ""));
});

test("a hidden window comes back for a new session or a new request for help", () => {
  assert.equal(dismissKey({ opened_at: 5, attention: null }), "5|");
  assert.notEqual(dismissKey({ opened_at: 5, attention: "Sign in" }), dismissKey({ opened_at: 5, attention: null }));
  assert.equal(dismissKey(null), "");
});

test("sites and typed addresses", () => {
  assert.equal(siteOf("https://www.example.com/a?b=1"), "example.com");
  assert.equal(siteOf(""), "");
  assert.equal(addressFor("example.com/x"), "https://example.com/x");
  assert.equal(addressFor("http://localhost:3000"), "http://localhost:3000");
  assert.equal(addressFor("  "), "");
});

test("a recording's clock and a saved file's line", () => {
  assert.equal(recordingClock(0), "0:00");
  assert.equal(recordingClock(9.9), "0:09");
  assert.equal(recordingClock(125), "2:05");
  assert.equal(
    savedFileLine({ path: "/w/recordings/browser-1.mp4", relative: "recordings/browser-1.mp4", name: "browser-1.mp4", bytes: 3 * 1024 * 1024, seconds: 42, url: "", files_url: "" }),
    "Saved browser-1.mp4 in recordings (0:42, 3.0 MB).",
  );
  assert.equal(
    savedFileLine({ path: "/w/screenshots/live-1.png", relative: "screenshots/live-1.png", name: "live-1.png", bytes: 20480, seconds: 0, url: "", files_url: "" }),
    "Saved live-1.png in screenshots (20 KB).",
  );
});
