import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { effectiveChatModel, restoreModelFromThread, restoredModelUsable } from "./chatModel.ts";

describe("restoreModelFromThread", () => {
  it("restores the model and effort the thread last answered with", () => {
    assert.deepEqual(restoreModelFromThread({ model: "anthropic:claude-sonnet-4-6", reasoning_effort: "high" }), {
      model: "anthropic:claude-sonnet-4-6",
      reasoningEffort: "high",
    });
  });

  it("restores nothing for a thread recorded before the stamp existed", () => {
    assert.deepEqual(restoreModelFromThread({}), { model: "", reasoningEffort: "" });
    assert.deepEqual(restoreModelFromThread({ model: null, reasoning_effort: null }), {
      model: "",
      reasoningEffort: "",
    });
  });
});

describe("effectiveChatModel", () => {
  it("lets the conversation's own pick win over the agent default", () => {
    assert.equal(
      effectiveChatModel({ sessionModel: "olano:max", agentModel: "olano:turbo", streamModel: "olano:pro" }),
      "olano:max",
    );
  });

  it("falls back to the agent's configured model for a new chat", () => {
    assert.equal(effectiveChatModel({ sessionModel: "", agentModel: "olano:turbo" }), "olano:turbo");
  });

  it("uses what the last turn reported only while the agent roster is unknown", () => {
    assert.equal(effectiveChatModel({ sessionModel: "", agentModel: undefined, streamModel: "olano:pro" }), "olano:pro");
    assert.equal(effectiveChatModel({ sessionModel: "", agentModel: undefined }), "");
  });
});

describe("restoredModelUsable", () => {
  const catalog = ["olano:turbo", "olano:pro", "anthropic:claude-sonnet-4-6"];

  it("accepts the default, a catalogued model and the agent's own model", () => {
    assert.equal(restoredModelUsable({ model: "", agentModel: "olano:turbo", catalogValues: catalog }), true);
    assert.equal(restoredModelUsable({ model: "olano:pro", agentModel: "olano:turbo", catalogValues: catalog }), true);
    assert.equal(
      restoredModelUsable({ model: "ollama:llama3.2", agentModel: "ollama:llama3.2", catalogValues: catalog }),
      true,
    );
  });

  it("refuses a model the box no longer offers", () => {
    assert.equal(
      restoredModelUsable({ model: "openai:gpt-4-retired", agentModel: "olano:turbo", catalogValues: catalog }),
      false,
    );
  });
});
