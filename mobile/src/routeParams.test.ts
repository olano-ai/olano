import assert from "node:assert/strict";
import { test } from "node:test";

import { decodeSegment, isPageFor } from "./routeParams.ts";

test("a segment decodes once, and a stray percent is left alone", () => {
  assert.equal(decodeSegment("acp%3A%3Aaria%3A%3Anova"), "acp::aria::nova");
  assert.equal(decodeSegment("100%"), "100%");
});

test("a conversation page owns the address that names its agent and thread, however encoded", () => {
  assert.equal(isPageFor("/chats/nova/new", "nova", "new"), true);
  assert.equal(isPageFor("/chats/nova/acp%3A%3Aa%3A%3Ab", "nova", "acp::a::b"), true);
  assert.equal(isPageFor("/chats/jane%20doe/x", "jane doe", "x"), true);
  // Another thread of the same agent, the agent's list, a different agent.
  assert.equal(isPageFor("/chats/nova/supplier-comparison", "nova", "new"), false);
  assert.equal(isPageFor("/chats/nova", "nova", "new"), false);
  assert.equal(isPageFor("/chats/aria/new", "nova", "new"), false);
  assert.equal(isPageFor("/chats/nova/new/extra", "nova", "new"), false);
});
