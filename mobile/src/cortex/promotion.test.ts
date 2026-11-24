import assert from "node:assert/strict";
import { test } from "node:test";

import type { CortexItem } from "../net/types.ts";
import { promotionState, promotionText } from "./promotion.ts";

const base: CortexItem = { id: "i1", kind: "brainstorm", title: "Shorter onboarding", status: "active", agent: "nova" };

test("an item at rest has no promotion to report", () => {
  assert.equal(promotionState(base), null);
  assert.equal(promotionState({ ...base, job: { id: "j", kind: "explore", status: "running" } }), null);
});

test("a running promotion is on its way to the job's stage, with the step the agent is on", () => {
  const state = promotionState({
    ...base,
    job: { id: "j", kind: "promote", status: "running", detail: { to: "idea" }, last_step: { text: "  writing\nREADME.md ", tool: "write_file" } },
  });
  assert.deepEqual(state, { phase: "moving", to: "idea", step: "write_file: writing README.md" });
  assert.equal(promotionText(state!, "Nova").title, "Nova is making this an idea…");
  assert.equal(promotionText(state!, "Nova").body, "write_file: writing README.md");
});

test("a queued promotion with no step yet explains the wait", () => {
  const state = promotionState({ ...base, job: { id: "", kind: "promote", status: "queued", detail: { to: "next" } } });
  assert.equal(state?.phase, "moving");
  assert.equal(state?.to, "idea");
  assert.match(promotionText(state!, "").body, /minute or two/);
  assert.match(promotionText(state!, "").title, /^The agent is making this an idea/);
});

test("a promoted item names where it went and the page to open", () => {
  const state = promotionState({
    ...base,
    status: "promoted",
    links: { promoted_to: "ideas/shorter", promoted_to_id: "i9", promoted_to_kind: "idea" },
  });
  assert.deepEqual(state, { phase: "promoted", to: "idea", id: "i9" });
  assert.equal(promotionText(state!, "Nova").title, "Now an idea");
});

test("a promoted item from an older index falls back to the funnel's next stage", () => {
  assert.deepEqual(promotionState({ ...base, kind: "idea", status: "promoted" }), { phase: "promoted", to: "prototype", id: "" });
});

test("a failed run is reported with its error, and a stop or crash with a plain reason", () => {
  const failed = promotionState({ ...base, job: { id: "j", kind: "promote", status: "error", error: "Budget spent.", detail: { to: "idea" } } });
  assert.deepEqual(failed, { phase: "failed", to: "idea", error: "Budget spent." });
  assert.equal(promotionText(failed!, "Nova").title, "Could not make it an idea");
  assert.deepEqual(promotionState({ ...base, job: { id: "j", kind: "promote", status: "stopped" } }), {
    phase: "failed",
    to: "idea",
    error: "The run was stopped.",
  });
  assert.deepEqual(promotionState({ ...base, job: { id: "j", kind: "promote", status: "interrupted" } }), {
    phase: "failed",
    to: "idea",
    error: "The run was interrupted.",
  });
});
