import { test } from "node:test";
import assert from "node:assert/strict";
import { DOCS_BASE, docsUrlFor } from "./docsLinks.ts";

test("a dashboard tab opens its own page of the guide", () => {
  assert.equal(docsUrlFor("/agents"), `${DOCS_BASE}dashboard/agents/`);
  assert.equal(docsUrlFor("/agents/merli?tab=overview"), `${DOCS_BASE}dashboard/agents/`);
  assert.equal(docsUrlFor("/config#speech"), `${DOCS_BASE}dashboard/config/`);
});

test("routes whose tab name differs from the page name still resolve", () => {
  assert.equal(docsUrlFor("/activity"), `${DOCS_BASE}dashboard/agent-to-agent/`);
  assert.equal(docsUrlFor("/orgchart"), `${DOCS_BASE}dashboard/org-chart/`);
});

test("an unknown route and the root open the guide's home, never a guessed page", () => {
  assert.equal(docsUrlFor("/"), DOCS_BASE);
  assert.equal(docsUrlFor(""), DOCS_BASE);
  assert.equal(docsUrlFor("/login"), DOCS_BASE);
  assert.equal(docsUrlFor("/oauth/consent"), DOCS_BASE);
});

test("the base is a versioned, slash-terminated URL so pages append cleanly", () => {
  assert.ok(DOCS_BASE.startsWith("https://docs.olano.ai/"));
  assert.ok(DOCS_BASE.endsWith("/"));
});
