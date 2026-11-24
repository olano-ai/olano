// `node --test` over the pure link extractor: which tool results become a
// card, and which of those are rendered inline as a secret form.

import assert from "node:assert/strict";
import { test } from "node:test";

import { extractConnectLinks, inlineFormLink, inlineFormSpent, type ConnectLink } from "./connectLinks.ts";
import type { ToolCall } from "./turnText.ts";

const ORIGIN = "https://acme.olano.run";

function call(name: string, result: string): ToolCall {
  return { id: name, name, args: "", result, hasResult: true } as ToolCall;
}

function secretResult(over: Record<string, unknown> = {}): string {
  return JSON.stringify({
    id: "hs_0123456789abcdef",
    url: `${ORIGIN}/api/connect/tok`,
    pin: "123456",
    flow: "secret-entry",
    label: "API key",
    expires_at: "2026-10-25T10:00:00+00:00",
    ttl_minutes: 15,
    agent_guidance: "tell the user to paste the key into the card below",
    ...over,
  });
}

test("a magic-link result carries its id and flow onto the link", () => {
  const [link] = extractConnectLinks([call("magic_link_request_secret", secretResult())]);
  assert.equal(link.kind, "magic-link");
  assert.equal(link.id, "hs_0123456789abcdef");
  assert.equal(link.flow, "secret-entry");
  assert.equal(link.pin, "123456");
});

test("a truncated persisted result still yields the id and flow", () => {
  // Transcripts cut tool results; every field we need sits before the cut.
  const raw = secretResult().slice(0, 140);
  assert.throws(() => JSON.parse(raw));
  const [link] = extractConnectLinks([call("magic_link_request_secret", raw)]);
  assert.equal(link.id, "hs_0123456789abcdef");
  assert.equal(link.flow, "secret-entry");
});

test("secret-entry and provider-key on the client's own origin render inline", () => {
  for (const flow of ["secret-entry", "provider-key"]) {
    const [link] = extractConnectLinks([call("magic_link_request_secret", secretResult({ flow }))]);
    assert.equal(inlineFormLink(link, ORIGIN), true, flow);
    assert.equal(inlineFormLink(link, `${ORIGIN}/`), true, "trailing slash on the origin");
  }
});

test("every other flow keeps the link card", () => {
  for (const flow of ["connector-oauth", "google", "edit-prompt", "edit-file", "channel-connect", ""]) {
    const [link] = extractConnectLinks([call("magic_link_connect_service", secretResult({ flow }))]);
    assert.equal(inlineFormLink(link, ORIGIN), false, flow || "(no flow)");
  }
});

test("a link to another origin never becomes an inline credential field", () => {
  // A tool merely named magic_link_* (an MCP tool, say) could return any URL.
  const [link] = extractConnectLinks([
    call("magic_link_request_secret", secretResult({ url: "https://evil.example/api/connect/tok" })),
  ]);
  assert.equal(inlineFormLink(link, ORIGIN), false);
  assert.equal(inlineFormLink(link, ""), false, "no known origin: never inline");
});

test("an MCP OAuth link and a link with no id are not inline", () => {
  const oauth: ConnectLink = { href: `${ORIGIN}/x`, kind: "mcp-oauth", serverUrl: "https://mcp.example/mcp" };
  assert.equal(inlineFormLink(oauth, ORIGIN), false);
  const noId: ConnectLink = { href: `${ORIGIN}/api/connect/t`, kind: "magic-link", flow: "secret-entry" };
  assert.equal(inlineFormLink(noId, ORIGIN), false);
});

test("spent statuses are the terminal ones", () => {
  for (const s of ["done", "error", "expired", "revoked"]) assert.equal(inlineFormSpent(s), true, s);
  for (const s of ["pending", "in_progress", ""]) assert.equal(inlineFormSpent(s), false, s || "(empty)");
});
