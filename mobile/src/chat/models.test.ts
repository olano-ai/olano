import { strict as assert } from "node:assert";
import { test } from "node:test";

import {
  levelForModel,
  modelChoices,
  modelLabel,
  modelUsable,
  reasoningLevels,
  type ProvidersResponse,
} from "./models.ts";

const CATALOG: ProvidersResponse = {
  providers: [
    {
      key: "olano",
      name: "Olano",
      models: ["max", "pro", "turbo"],
      auth: { connected: true },
      model_meta: { pro: { reasoning_efforts: ["low", "medium", "high"], default_effort: "medium" } },
    },
    {
      key: "anthropic",
      name: "Anthropic",
      models: ["claude-sonnet-4-6"],
      env_var: "ANTHROPIC_API_KEY",
      env_set: true,
      model_meta: { "claude-sonnet-4-6": { reasoning_efforts: ["low", "high"], default_effort: null } },
    },
    { key: "openai", name: "OpenAI", models: ["gpt-5"], env_var: "OPENAI_API_KEY", env_set: false },
    { key: "fireworks", name: "Fireworks", models: ["llama"], available: false },
  ],
  model_aliases: [
    { value: "default:pro", tier: "pro", target: "olano:pro" },
    { value: "default:max", tier: "max", target: "openai:gpt-5" },
  ],
};

test("only models the deployment can run, defaults first, then Olano cheapest first", () => {
  const values = modelChoices(CATALOG).map((c) => c.value);
  assert.deepEqual(values, ["default:pro", "olano:turbo", "olano:pro", "olano:max", "anthropic:claude-sonnet-4-6"]);
  const pro = modelChoices(CATALOG).find((c) => c.value === "olano:pro");
  assert.equal(pro?.label, "Olano Pro");
  assert.equal(pro?.hint, "More power for harder, multi-step work");
  assert.equal(modelChoices(CATALOG)[0].hint, "Currently Olano Pro");
  assert.deepEqual(modelChoices(undefined), []);
});

test("short names for the header", () => {
  assert.equal(modelLabel("olano:turbo"), "Olano Turbo");
  assert.equal(modelLabel("default:max"), "Default · Max");
  assert.equal(modelLabel("anthropic:claude-sonnet-4-6"), "claude-sonnet-4-6");
  assert.equal(modelLabel(""), "");
});

test("reasoning levels come from the model, a default answering as its target", () => {
  assert.deepEqual(reasoningLevels(CATALOG, "olano:pro"), { levels: ["low", "medium", "high"], defaultLevel: "medium" });
  assert.deepEqual(reasoningLevels(CATALOG, "default:pro"), { levels: ["low", "medium", "high"], defaultLevel: "medium" });
  assert.equal(reasoningLevels(CATALOG, "olano:turbo"), null);
  assert.equal(reasoningLevels(CATALOG, ""), null);
});

test("a level the next model lacks goes back to its default", () => {
  assert.equal(levelForModel(CATALOG, "anthropic:claude-sonnet-4-6", "high"), "high");
  assert.equal(levelForModel(CATALOG, "anthropic:claude-sonnet-4-6", "medium"), "");
  assert.equal(levelForModel(CATALOG, "olano:turbo", "high"), "");
});

test("a restored model must still be offered, or be the agent's own", () => {
  const choices = modelChoices(CATALOG);
  assert.equal(modelUsable("olano:max", "olano:pro", choices), true);
  assert.equal(modelUsable("openai:gpt-5", "olano:pro", choices), false);
  assert.equal(modelUsable("custom:thing", "custom:thing", choices), true);
  assert.equal(modelUsable("", "olano:pro", choices), true);
});
