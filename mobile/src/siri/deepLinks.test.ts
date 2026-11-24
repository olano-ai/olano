import assert from "node:assert/strict";
import { test } from "node:test";

import { composeModeOf, deepLinkAction } from "./deepLinks.ts";

test("talk links name the agent", () => {
  assert.deepEqual(deepLinkAction("olano://talk?agent=anna"), { kind: "talk", agent: "anna" });
  assert.equal(deepLinkAction("olano://talk"), null);
  assert.equal(deepLinkAction("olano://talk?agent=%20"), null);
});

test("open links go to a conversation, a new one, or the agent as Chats opens it", () => {
  assert.deepEqual(deepLinkAction("olano://open?agent=anna&thread=abc"), { kind: "route", route: "/chats/anna/abc" });
  assert.deepEqual(deepLinkAction("olano://open?agent=anna&thread=new"), { kind: "route", route: "/chats/anna/new" });
  // No thread named: the last conversation, which takes a read of the deployment.
  assert.deepEqual(deepLinkAction("olano://open?agent=anna"), { kind: "agent", agent: "anna" });
  assert.deepEqual(deepLinkAction("olano://open?agent=a%20b&thread=x%3Ay"), { kind: "route", route: "/chats/a%20b/x%3Ay" });
});

test("a compose mode readies the composer in a conversation", () => {
  assert.deepEqual(deepLinkAction("olano://open?agent=anna&thread=new&compose=camera"), { kind: "route", route: "/chats/anna/new?compose=camera" });
  assert.deepEqual(deepLinkAction("olano://open?agent=anna&thread=abc&compose=text"), { kind: "route", route: "/chats/anna/abc?compose=text" });
  // No thread named: the composer needs a conversation, so a new one.
  assert.deepEqual(deepLinkAction("olano://open?agent=anna&compose=photos"), { kind: "route", route: "/chats/anna/new?compose=photos" });
  // An unknown mode is ignored, not passed on.
  assert.deepEqual(deepLinkAction("olano://open?agent=anna&thread=new&compose=sing"), { kind: "route", route: "/chats/anna/new" });
  assert.equal(composeModeOf("?compose=Camera"), "camera");
  assert.equal(composeModeOf("?compose=sing"), null);
  assert.equal(composeModeOf(""), null);
});

test("open links name a tab", () => {
  assert.deepEqual(deepLinkAction("olano://open?tab=inbox"), { kind: "route", route: "/inbox" });
  assert.deepEqual(deepLinkAction("olano://open?tab=cortex"), { kind: "route", route: "/cortex/pulse" });
  assert.deepEqual(deepLinkAction("olano://open?tab=scheduled"), { kind: "route", route: "/settings/scheduled" });
  assert.deepEqual(deepLinkAction("olano://open?tab=Chats"), { kind: "route", route: "/chats" });
  assert.equal(deepLinkAction("olano://open?tab=nowhere"), null);
  assert.equal(deepLinkAction("olano://open"), null);
});

test("other links are not ours", () => {
  assert.equal(deepLinkAction("olano://share"), null);
  assert.equal(deepLinkAction("https://example.com/open?agent=anna"), null);
  assert.equal(deepLinkAction("file:///tmp/x.pdf"), null);
  assert.equal(deepLinkAction("not a url"), null);
});
