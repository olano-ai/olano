import { test } from "node:test";
import assert from "node:assert/strict";
import { cardText, resultSucceeded } from "./connectResultText.ts";

test("a finished magic link and an authorized MCP sign-in are both successes", () => {
  assert.equal(resultSucceeded("done"), true);
  // Rows persisted before the backend normalized the MCP OAuth status.
  assert.equal(resultSucceeded("authorized"), true);
  for (const s of ["error", "expired", "revoked", ""]) assert.equal(resultSucceeded(s), false);
});

test("the card text drops the status glyph and the bold markers", () => {
  assert.equal(
    cardText("✅ Sign-in complete: **composio** is now connected to connect.composio.dev (11 tools)."),
    "Sign-in complete: composio is now connected to connect.composio.dev (11 tools).",
  );
  assert.equal(
    cardText("⚠️ Sign-in for **researcher** → example.com didn't complete."),
    "Sign-in for researcher → example.com didn't complete.",
  );
  // A lone asterisk is text, not markup.
  assert.equal(cardText("2 * 3 = 6"), "2 * 3 = 6");
});
