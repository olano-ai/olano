import assert from "node:assert/strict";
import { test } from "node:test";

import { filterChips } from "./threadFilters.ts";

test("each conversation counts under one chip, in the dashboard's order", () => {
  const chips = filterChips([
    { origin: "interactive", channel: "dashboard" },
    { origin: "interactive", channel: "mobile" },
    { origin: "interactive", channel: "mobile" },
    { origin: "interactive", channel: "telegram" },
    { origin: "scheduled", channel: "telegram" },
    { origin: "scheduled", channel: "scheduler" },
    { origin: "peer", channel: "" },
  ]);
  assert.deepEqual(
    chips.map((c) => `${c.label} ${c.count}`),
    ["All 7", "Dashboard 1", "Mobile 2", "Telegram 1", "Scheduled 2", "Agents 1"],
  );
});

test("an empty bucket gets no chip", () => {
  assert.deepEqual(
    filterChips([{ origin: "interactive", channel: "dashboard" }]).map((c) => c.label),
    ["All", "Dashboard"],
  );
});
