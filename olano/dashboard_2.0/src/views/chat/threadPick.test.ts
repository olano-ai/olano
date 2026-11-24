import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isHumanConversation, pickFromNewestPage, pickLatestThread } from "./threadPick.ts";

const at = (h: number) => `2026-09-06T${String(h).padStart(2, "0")}:00:00+00:00`;

describe("isHumanConversation", () => {
  it("counts a dashboard chat, a Cortex hand-off and a messenger conversation", () => {
    assert.equal(isHumanConversation({ id: "a", origin: "interactive", channel: "dashboard" }), true);
    assert.equal(isHumanConversation({ id: "b", origin: "cortex", channel: "dashboard" }), true);
    assert.equal(isHumanConversation({ id: "c", origin: "interactive", channel: "telegram" }), true);
    assert.equal(isHumanConversation({ id: "d", origin: "interactive", channel: "whatsapp" }), true);
    // The Olano mobile app: a person on their phone.
    assert.equal(isHumanConversation({ id: "m", origin: "interactive", channel: "mobile" }), true);
    // An older backend sends no provenance at all: that is a plain chat.
    assert.equal(isHumanConversation({ id: "e" }), true);
  });

  it("rules out the scheduler, other agents, and inbound protocol calls", () => {
    assert.equal(isHumanConversation({ id: "a", origin: "scheduled", channel: "scheduler" }), false);
    assert.equal(isHumanConversation({ id: "b", origin: "heartbeat", channel: "scheduler" }), false);
    assert.equal(isHumanConversation({ id: "c", origin: "peer", channel: "" }), false);
    assert.equal(isHumanConversation({ id: "d", origin: "autonomous", channel: "" }), false);
    assert.equal(isHumanConversation({ id: "e", origin: "external", channel: "mcp" }), false);
    assert.equal(isHumanConversation({ id: "f", origin: "external", channel: "a2a" }), false);
  });

  it("files a scheduled run that was delivered on Telegram under the scheduler", () => {
    assert.equal(isHumanConversation({ id: "a", origin: "scheduled", channel: "telegram" }), false);
  });
});

describe("pickLatestThread", () => {
  it("returns null for an agent with no threads", () => {
    assert.equal(pickLatestThread([]), null);
  });

  it("prefers the newest human conversation over a newer scheduled run", () => {
    const pick = pickLatestThread([
      { id: "sched", origin: "scheduled", channel: "scheduler", updated_at: at(12) },
      { id: "tg", origin: "interactive", channel: "telegram", updated_at: at(10) },
      { id: "dash", origin: "interactive", channel: "dashboard", updated_at: at(8) },
    ]);
    assert.equal(pick?.id, "tg");
  });

  it("ranks a dashboard chat and a messenger chat by recency alone", () => {
    const pick = pickLatestThread([
      { id: "tg", origin: "interactive", channel: "telegram", updated_at: at(9) },
      { id: "dash", origin: "interactive", channel: "dashboard", updated_at: at(11) },
    ]);
    assert.equal(pick?.id, "dash");
  });

  it("falls back to the newest thread of any kind when no person is in any of them", () => {
    const pick = pickLatestThread([
      { id: "mcp", origin: "external", channel: "mcp", updated_at: at(7) },
      { id: "sched", origin: "scheduled", channel: "scheduler", updated_at: at(9) },
      { id: "peer", origin: "peer", channel: "", updated_at: at(8) },
    ]);
    assert.equal(pick?.id, "sched");
  });

  it("does not trust the input order", () => {
    const pick = pickLatestThread([
      { id: "old", origin: "interactive", channel: "dashboard", updated_at: at(1) },
      { id: "new", origin: "interactive", channel: "dashboard", updated_at: at(2) },
    ]);
    assert.equal(pick?.id, "new");
  });

  it("sorts a thread with no timestamp last", () => {
    const pick = pickLatestThread([
      { id: "undated", origin: "interactive", channel: "dashboard" },
      { id: "dated", origin: "interactive", channel: "dashboard", updated_at: at(3) },
    ]);
    assert.equal(pick?.id, "dated");
  });
});

describe("pickFromNewestPage", () => {
  const page = [
    { id: "sched", origin: "scheduled", channel: "scheduler", updated_at: at(12) },
    { id: "cortex", origin: "cortex", channel: "dashboard", updated_at: at(11) },
    { id: "tg", origin: "interactive", channel: "telegram", updated_at: at(10) },
  ];

  it("decides from the page when a person's conversation is on it, as the full list would", () => {
    const got = pickFromNewestPage(page, true);
    assert.equal(got.decided, true);
    // A Cortex hand-off is a person's conversation here (the server's
    // kind=conversation filter leaves it out, which is why the page is read
    // unfiltered and the rule applied on this side).
    assert.equal(got.pick?.id, "cortex");
    assert.equal(got.pick?.id, pickLatestThread(page)?.id);
  });

  it("decides a page with no person on it only when the page is the whole list", () => {
    const machines = [
      { id: "peer", origin: "peer", channel: "", updated_at: at(8) },
      { id: "sched", origin: "scheduled", channel: "scheduler", updated_at: at(9) },
    ];
    const whole = pickFromNewestPage(machines, false);
    assert.deepEqual([whole.decided, whole.pick?.id], [true, "sched"]);
    // More threads below: tier 1 may sit further down, so only the full list can say.
    assert.deepEqual(pickFromNewestPage(machines, true), { decided: false, pick: null });
  });

  it("decides an empty agent as a fresh draft", () => {
    assert.deepEqual(pickFromNewestPage([], false), { decided: true, pick: null });
  });
});
