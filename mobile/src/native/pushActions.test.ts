import { strict as assert } from "node:assert";
import { test } from "node:test";

import { answerFor, answerNotice, routeFor } from "./pushActions.ts";

test("Approve on a chat approval answers every action in it", () => {
  assert.deepEqual(answerFor("approve", { kind: "approval", agent: "nova", thread_id: "t1", count: "2" }), {
    kind: "chat",
    threadId: "t1",
    agent: "nova",
    count: 2,
    approve: true,
  });
  assert.equal(answerFor("decline", { kind: "approval", agent: "nova", thread_id: "t1" })?.approve, false);
  assert.equal((answerFor("approve", { kind: "approval", thread_id: "t1", count: "x" }) as { count: number }).count, 1);
});

test("Approve on an inbox approval resolves that request", () => {
  assert.deepEqual(answerFor("decline", { kind: "inbox_approval", item_id: "r9" }), { kind: "inbox", id: "r9", approve: false });
});

test("a plain tap, or a notification without what it takes, answers nothing", () => {
  assert.equal(answerFor("tap", { kind: "approval", thread_id: "t1" }), null);
  assert.equal(answerFor("approve", { kind: "reply", thread_id: "t1" }), null);
  assert.equal(answerFor("approve", { kind: "inbox_approval" }), null);
});

test("the app says what happened", () => {
  const chat = answerFor("approve", { kind: "approval", agent: "nova", thread_id: "t1" })!;
  assert.equal(answerNotice(chat, "done"), "Approved. nova carries on.");
  assert.match(answerNotice(chat, "gone"), /already answered/);
  assert.match(answerNotice({ kind: "inbox", id: "r", approve: false }, "done"), /^Declined\. The agent/);
});

test("a notification opens what it is about", () => {
  assert.equal(routeFor({ kind: "reply", agent: "jane doe", thread_id: "t:1" }), "/chats/jane%20doe/t%3A1");
  assert.equal(routeFor({ kind: "pulse", agent: "nova", lane: "pulse" }), "/cortex/pulse");
  assert.equal(routeFor({ kind: "pulse", agent: "nova", lane: "approval" }), "/inbox");
  assert.equal(routeFor({ kind: "alert" }), "/inbox");
  assert.equal(routeFor({ kind: "reply", agent: "nova" }), null);
  assert.equal(routeFor({ kind: "other" }), null);
});

test("a browser check opens the agent's chats", () => {
  assert.equal(routeFor({ kind: "browser_check", agent: "nova" }), "/chats/nova");
  assert.equal(routeFor({ kind: "browser_check" }), null);
});
