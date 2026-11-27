import { test } from "node:test";
import assert from "node:assert/strict";
import { bareToolName, summarizeApproval } from "./approvalSummary.ts";

test("an email send reads as an email, with who it goes to", () => {
  const s = summarizeApproval("gmail_send", { to: "ana@example.com", subject: "Hi", body: "..." });
  assert.equal(s.say, "email");
  assert.deepEqual(s.target, { key: "to", value: "ana@example.com" });
});

test("a shell command shows the command as the detail", () => {
  const s = summarizeApproval("execute", '{"command": "rm -rf build"}');
  assert.equal(s.say, "run");
  assert.deepEqual(s.target, { key: "command", value: "rm -rf build" });
});

test("file writes and edits name the file", () => {
  assert.equal(summarizeApproval("write_file", { file_path: "/w/a.md" }).say, "writeFile");
  const e = summarizeApproval("edit_file", { file_path: "/w/a.md" });
  assert.equal(e.say, "editFile");
  assert.equal(e.target?.key, "file");
});

test("money, secrets, deletions and other agents get their own words", () => {
  assert.equal(summarizeApproval("wallet_send", {}).say, "pay");
  assert.equal(summarizeApproval("secret_get", { name: "X" }).say, "secretRead");
  assert.equal(summarizeApproval("secret_set", {}).say, "secretWrite");
  assert.equal(summarizeApproval("calendar_delete_event", {}).say, "delete");
  assert.equal(summarizeApproval("talk_to_agent", { agent_name: "maria" }).say, "peer");
});

test("a post names the service it goes to", () => {
  const s = summarizeApproval("slack_send_message", { channel: "#general" });
  assert.equal(s.say, "post");
  assert.equal(s.service, "Slack");
  assert.deepEqual(s.target, { key: "to", value: "#general" });
});

test("MCP-prefixed names are judged by the tool itself", () => {
  assert.equal(bareToolName("zapier__gmail_send"), "gmail_send");
  assert.equal(summarizeApproval("zapier__gmail_send", {}).say, "email");
});

test("an unknown tool falls back to its readable name, never JSON", () => {
  const s = summarizeApproval("shopify_create_product", { title: "Mug" });
  assert.equal(s.say, "generic");
  assert.equal(s.tool, "Shopify create product");
  assert.deepEqual(s.target, { key: "subject", value: "Mug" });
});

test("no name and no args still yields a sentence", () => {
  const s = summarizeApproval(undefined, null);
  assert.equal(s.say, "generic");
  assert.equal(s.tool, "tool");
  assert.equal(s.target, undefined);
});
