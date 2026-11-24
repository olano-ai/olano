import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { shouldPreloadDraft, stageChatHandoff, takeChatHandoff } from "./handoff.ts";

describe("takeChatHandoff", () => {
  it("is one-shot: the second claim gets nothing", () => {
    stageChatHandoff({ agent: "merli", threadId: "pulse:1", draft: "x" });
    assert.deepEqual(takeChatHandoff(), { agent: "merli", threadId: "pulse:1", draft: "x" });
    assert.equal(takeChatHandoff(), null);
  });

  it("a later stage replaces an unclaimed one", () => {
    stageChatHandoff({ agent: "a", threadId: "pulse:1" });
    stageChatHandoff({ agent: "b", threadId: "cortex:2" });
    assert.equal(takeChatHandoff()?.threadId, "cortex:2");
  });
});

describe("shouldPreloadDraft", () => {
  it("only a brand-new thread with an empty composer is pre-filled", () => {
    assert.equal(shouldPreloadDraft({ messages: [], input: "" }), true);
    assert.equal(shouldPreloadDraft({ messages: [], input: "   " }), true);
  });

  it("an existing conversation continues on its history, untouched", () => {
    assert.equal(shouldPreloadDraft({ messages: [{}], input: "" }), false);
  });

  it("a message the person is mid-way through typing is never overwritten", () => {
    assert.equal(shouldPreloadDraft({ messages: [], input: "half a thou" }), false);
  });

  it("no session, nothing to fill", () => {
    assert.equal(shouldPreloadDraft(undefined), false);
  });
});
