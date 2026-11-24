import { strict as assert } from "node:assert";
import { test } from "node:test";

import { sourceBadges } from "./sources.ts";

test("a conversation had in this app says Mobile, in its own colour", () => {
  assert.deepEqual(sourceBadges({ origin: "interactive", channel: "mobile" }), [{ label: "Mobile", tone: "mobile" }]);
});

test("a plain chat says Dashboard", () => {
  assert.deepEqual(sourceBadges({ origin: "interactive", channel: "dashboard" }), [{ label: "Dashboard", tone: "neutral" }]);
  assert.deepEqual(sourceBadges({ channel: "api" }), [{ label: "Dashboard", tone: "neutral" }]);
  assert.deepEqual(sourceBadges({}), [{ label: "Dashboard", tone: "neutral" }]);
});

test("messengers get their own colour and product name", () => {
  assert.deepEqual(sourceBadges({ origin: "interactive", channel: "telegram" }), [{ label: "Telegram", tone: "telegram" }]);
  assert.deepEqual(sourceBadges({ channel: "whatsapp_twilio" }), [{ label: "WhatsApp (Twilio)", tone: "whatsapp" }]);
  assert.deepEqual(sourceBadges({ channel: "email" }), [{ label: "Email", tone: "email" }]);
});

test("how a conversation started comes first; a messenger it ran on follows", () => {
  assert.deepEqual(sourceBadges({ origin: "scheduled", channel: "scheduler" }), [{ label: "Scheduled", tone: "scheduled" }]);
  assert.deepEqual(sourceBadges({ origin: "peer", channel: "dashboard" }), [{ label: "Agent", tone: "peer" }]);
  assert.deepEqual(sourceBadges({ origin: "cortex" }), [{ label: "Cortex", tone: "cortex" }]);
  assert.deepEqual(sourceBadges({ origin: "scheduled", channel: "telegram" }), [
    { label: "Scheduled", tone: "scheduled" },
    { label: "Telegram", tone: "telegram" },
  ]);
  assert.deepEqual(sourceBadges({ origin: "external", channel: "mcp" }), [
    { label: "External", tone: "external" },
    { label: "MCP", tone: "mcp" },
  ]);
});
