import assert from "node:assert/strict";
import { test } from "node:test";

import { conversationPath, isConversation, lastConversation } from "./threadPick.ts";

const at = (h: number) => `2026-10-18T${String(h).padStart(2, "0")}:00:00+00:00`;

test("tapping an agent reopens the last conversation a person had with it", () => {
  const pick = lastConversation([
    { id: "sched", origin: "scheduled", channel: "scheduler", updated_at: at(12) },
    { id: "peer", origin: "peer", channel: "", updated_at: at(11) },
    { id: "cortex:abc", origin: "cortex", channel: "dashboard", updated_at: at(11) },
    { id: "mcp::x::y", origin: "external", channel: "mcp", updated_at: at(11) },
    { id: "tg", origin: "interactive", channel: "telegram", updated_at: at(9) },
    { id: "phone", origin: "interactive", channel: "mobile", updated_at: at(10) },
  ]);
  assert.equal(pick?.id, "phone");
});

test("an agent with only scheduled runs opens a new conversation", () => {
  assert.equal(lastConversation([{ id: "s", origin: "scheduled", channel: "scheduler", updated_at: at(1) }]), null);
  assert.equal(lastConversation([]), null);
});

test("Cortex threads stay under Cortex", () => {
  assert.equal(isConversation({ id: "pulse:1", origin: "cortex", channel: "dashboard" }), false);
  assert.equal(isConversation({ id: "a", origin: "interactive", channel: "dashboard" }), true);
  assert.equal(isConversation({ id: "b" }), true);
});

test("the path names the thread, encoded, or a new one", () => {
  assert.equal(conversationPath("nova", { id: "acp::a::b" }), "/chats/nova/acp%3A%3Aa%3A%3Ab");
  assert.equal(conversationPath("jane doe", null), "/chats/jane%20doe/new");
});
