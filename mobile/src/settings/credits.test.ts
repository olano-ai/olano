import assert from "node:assert/strict";
import { test } from "node:test";

import { creditUnit, formatCredits, isLow, renewsOn, spendingServices, usedPercent } from "./credits.ts";

const unit = creditUnit({ unit_name: "credit", unit_name_plural: "credits", decimals: 0 });

test("amounts read like the dashboard's", () => {
  assert.equal(formatCredits(0, unit), "0 credits");
  assert.equal(formatCredits(1, unit), "1 credit");
  assert.equal(formatCredits(1250, unit), "1,250 credits");
  assert.equal(formatCredits(0.5, unit), "0.500 credits");
  assert.equal(formatCredits(0.004, unit), "0.0040 credits");
  assert.equal(formatCredits(12.5, creditUnit({ decimals: 2 })), "12.50 credits");
});

test("a deployment without a vocabulary still reads", () => {
  assert.deepEqual(creditUnit(null), { one: "credit", many: "credits", decimals: 0 });
  assert.equal(formatCredits(Number.NaN, creditUnit(undefined)), "0 credits");
});

test("the monthly bar", () => {
  assert.equal(usedPercent({ available: true, monthly_included: 1000, monthly_used: 250 }), 25);
  assert.equal(usedPercent({ available: true, monthly_included: 1000, monthly_used: 5000 }), 100);
  assert.equal(usedPercent({ available: true }), null);
});

test("low balance", () => {
  assert.equal(isLow({ available: true, balance: 40 }), true);
  assert.equal(isLow({ available: true, balance: 400, monthly_included: 10000 }), true);
  assert.equal(isLow({ available: true, balance: 900, monthly_included: 10000 }), false);
});

test("renewal date", () => {
  assert.equal(renewsOn({ available: true, period_end: "2026-11-01T00:00:00Z" }, "en-US"), "Nov 1");
  assert.equal(renewsOn({ available: true, period_end: "soon" }), null);
  assert.equal(renewsOn({ available: true }), null);
});

test("services that spent, biggest first", () => {
  const rows = spendingServices({
    available: true,
    services: [
      { service: "a", label: "Chat", calls: 3, credits: 10 },
      { service: "b", label: "Vision", calls: 0, credits: 0 },
      { service: "c", label: "Images", calls: 1, credits: 40 },
    ],
  });
  assert.deepEqual(
    rows.map((r) => r.label),
    ["Images", "Chat"],
  );
});
