import assert from "node:assert/strict";
import { test } from "node:test";

import type { IssueEvent } from "../net/types.ts";
import { agentPrincipal, bareName, eventLine, issueDraft, issueQuery, myPrincipal, principalKind } from "./issues.ts";

const event = (kind: string, payload: IssueEvent["payload"] = {}, actor = "human-carlos"): IssueEvent => ({
  id: 1,
  issue_id: 5,
  ts: "2026-10-17T10:00:00Z",
  actor,
  kind,
  payload,
});

test("principals name people and agents", () => {
  assert.equal(principalKind("human-carlos"), "human");
  assert.equal(principalKind("ai-nova"), "agent");
  assert.equal(principalKind("system"), "other");
  assert.equal(bareName("ai-nova"), "nova");
  assert.equal(agentPrincipal("nova"), "ai-nova");
});

test("each event that is not a comment reads as one line", () => {
  const name = bareName;
  assert.equal(eventLine(event("comment", { body: "hi" }), name), null);
  assert.equal(eventLine(event("close", { reason: "done" }), name), "carlos closed this: done");
  assert.equal(eventLine(event("reopen"), name), "carlos reopened this");
  assert.equal(eventLine(event("assign", { assignee: "ai-nova" }), name), "carlos assigned it to nova");
  assert.equal(eventLine(event("assign", { assignee: "" }), name), "carlos unassigned it");
  assert.equal(eventLine(event("label", { labels: ["bug", "ui"] }), name), "carlos set the labels to bug, ui");
  assert.equal(eventLine(event("type", { type: "bug" }), name), "carlos made it a bug");
});

test("asking an agent about an issue names it", () => {
  assert.equal(issueDraft({ id: 56, title: "Fix login" }), 'Let\'s talk about issue #56, "Fix login". Read it with its comments first.');
});

test("the issue filters ask the tracker for open ones, mine, or all", () => {
  assert.equal(myPrincipal({ user: { username: "carlos" } }), "human-carlos");
  assert.equal(myPrincipal(null), "human-owner");
  assert.deepEqual(issueQuery("open", "human-carlos"), { state: "open", assignee: "" });
  assert.deepEqual(issueQuery("mine", "human-carlos"), { state: "open", assignee: "human-carlos" });
  assert.deepEqual(issueQuery("all", "human-carlos"), { state: "all", assignee: "" });
});
