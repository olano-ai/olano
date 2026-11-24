import assert from "node:assert/strict";
import { test } from "node:test";

import { agentHealth, unavailableFromError, unavailableNotice } from "./health.ts";

test("a running agent can be stopped or restarted", () => {
  const h = agentHealth({ running: true, status: "running" });
  assert.equal(h.state, "running");
  assert.deepEqual([h.canStart, h.canStop, h.canRestart], [false, true, true]);
});

test("a stopped agent says whether someone stopped it", () => {
  const h = agentHealth({ running: false, paused: true, status: "paused" });
  assert.equal(h.state, "stopped");
  assert.match(h.detail, /stays stopped/);
  assert.equal(h.canStart, true);
});

test("a failed start shows its reason", () => {
  const h = agentHealth({ running: false, status: "error", error: "OPENAI_API_KEY is not set" });
  assert.equal(h.state, "error");
  assert.equal(h.detail, "OPENAI_API_KEY is not set");
  assert.equal(h.canStart, true);
});

test("a held back agent offers nothing to press", () => {
  const h = agentHealth({ running: false, status: "configured", blocked: true, blocked_reason: "Plan allows 3 agents" });
  assert.equal(h.state, "blocked");
  assert.equal(h.detail, "Plan allows 3 agents");
  assert.deepEqual([h.canStart, h.canStop, h.canRestart], [false, false, false]);
});

test("starting is its own state", () => {
  assert.equal(agentHealth({ running: false, status: "starting" }).state, "starting");
});

test("a paused agent is stopped even when its row still says running", () => {
  assert.equal(agentHealth({ running: true, paused: true, status: "running" }).state, "stopped");
});

test("a conversation with a stopped agent explains itself and offers Start to admins only", () => {
  const stopped = agentHealth({ running: false, paused: true, status: "paused" });
  const admin = unavailableNotice("Sebas", stopped, true);
  assert.equal(admin?.title, "Sebas is stopped");
  assert.equal(admin?.canStart, true);
  const member = unavailableNotice("Sebas", stopped, false);
  assert.equal(member?.canStart, false);
  assert.match(member?.body ?? "", /Ask an admin/);
  assert.equal(unavailableNotice("Sebas", agentHealth({ running: true }), false), null);
  assert.equal(unavailableNotice("Sebas", agentHealth({ running: false, status: "starting" }), false), null);
});

test("the deployment's refusal becomes the same banner", () => {
  const notice = unavailableFromError("Ana", { code: "agent_not_running", state: "error", reason: "no API key", can_start: true });
  assert.equal(notice?.title, "Ana couldn't start");
  assert.match(notice?.body ?? "", /no API key/);
  assert.equal(notice?.canStart, true);
  assert.equal(unavailableFromError("Ana", "Agent 'Ana' not found"), null);
  assert.equal(unavailableFromError("Ana", { code: "run_in_progress" }), null);
});
