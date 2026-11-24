import assert from "node:assert/strict";
import { test } from "node:test";

import { actionsFor, agentOf, fixDraft, issueOf } from "./alertActions.ts";

const admin = { isAdmin: true };
const member = { isAdmin: false };

test("an assigned issue opens in the app, from its link or from an older row's key", () => {
  const linked = { kind: "issue_assigned", title: "Issue #56 assigned to carlos", action: { label: "Open issue", href: "/issues?issue=56" } };
  assert.equal(issueOf(linked), 56);
  assert.deepEqual(actionsFor(linked, member), [{ kind: "issue", id: 56 }]);
  assert.equal(issueOf({ kind: "issue_assigned", title: "x", dedupe_key: "issue-7-assign-human-carlos" }), 7);
});

test("a skill-name mismatch is repaired by an admin, and the agent can be asked either way", () => {
  const alert = {
    kind: "skills_spec_violation",
    title: "1 skill(s) have a name that doesn't match their folder",
    body: "zoho-email",
    agent: "nova",
    action: { label: "Open Files", href: "/files?agent=nova" },
  };
  assert.deepEqual(
    actionsFor(alert, admin).map((a) => a.kind),
    ["repair_skills", "ask_agent"],
  );
  assert.deepEqual(
    actionsFor(alert, member).map((a) => a.kind),
    ["ask_agent"],
  );
});

test("a broken connection or MCP sign-in is something the agent fixes", () => {
  const mcp = {
    kind: "mcp_auth_required",
    title: "mcp.example.com needs to be re-authorized for nova",
    action: { label: "Open MCP setup", href: "/agents/nova?tab=mcp" },
  };
  assert.equal(agentOf(mcp), "nova");
  const [ask] = actionsFor(mcp, admin);
  assert.equal(ask.kind, "ask_agent");
  assert.ok(ask.kind === "ask_agent" && ask.agent === "nova" && ask.draft.includes("needs to be re-authorized"));
  assert.equal(agentOf({ kind: "gmail_watch", title: "x", action: { href: "/connections?agent=jane%20doe" } }), "jane doe");
});

test("a failed background run opens Cortex and can go to the agent", () => {
  const alert = { kind: "cognition_worker_failed", title: "Background run 'triage' failed", agent: "nova", action: { href: "/cortex?agent=nova" } };
  assert.deepEqual(
    actionsFor(alert, admin).map((a) => a.kind),
    ["cortex", "ask_agent"],
  );
});

test("what only the dashboard can fix says so", () => {
  assert.deepEqual(actionsFor({ kind: "disk_pressure", title: "Disk usage warn", action: { label: "Open Overview", href: "/overview" } }, admin), [
    { kind: "dashboard", label: "Open Overview" },
  ]);
  assert.deepEqual(actionsFor({ kind: "vault_locked", title: "Vault is locked", action: { label: "Unlock the vault", href: "/vault" } }, admin), [
    { kind: "dashboard", label: "Unlock the vault" },
  ]);
  assert.deepEqual(actionsFor({ kind: "info", title: "Hello" }, admin), []);
});

test("the draft quotes the alert and asks for the fix", () => {
  assert.equal(fixDraft({ kind: "x", title: "T", body: " B " }), 'The deployment raised this alert: "T".\n\nB\n\nCan you fix it, or tell me what you need from me?');
});
