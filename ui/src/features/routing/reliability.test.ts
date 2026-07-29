import assert from "node:assert/strict";
import test from "node:test";
import { breakerLabel, formatFailoverRate, trippedCount } from "./reliability.ts";

function view(overrides: Partial<Parameters<typeof trippedCount>[0]> = {}) {
  return {
    router: "auto",
    total: 10,
    by_attempts: { "1": 10 },
    failover_used: 0,
    failover_rate: 0,
    candidates: [],
    ...overrides,
  };
}

test("an in-flight query renders as unknown, not as zero", () => {
  // On a reliability panel "no breakers tripped" is reassurance and "we do not
  // know yet" is not; they must not look the same.
  assert.equal(trippedCount(undefined), "—");
  assert.equal(formatFailoverRate(undefined), "—");
});

test("tripped candidates are counted, half-open included", () => {
  // A half-open breaker is not taking traffic either — it is one trial away.
  const counted = trippedCount(
    view({
      candidates: [
        { model_name: "a", model_id: "1", breaker: "closed" },
        { model_name: "b", model_id: "2", breaker: "open" },
        { model_name: "c", model_id: "3", breaker: "half_open" },
      ],
    }),
  );
  assert.equal(counted, 2);
});

test("no candidates means nothing is shut out", () => {
  assert.equal(trippedCount(view()), 0);
});

test("the failover rate is shown as a percentage with one decimal", () => {
  assert.equal(formatFailoverRate(view({ failover_rate: 0.0526 })), "5.3%");
  assert.equal(formatFailoverRate(view({ failover_rate: 0 })), "0.0%");
  assert.equal(formatFailoverRate(view({ failover_rate: 1 })), "100.0%");
});

test("an unknown breaker state shows up as itself rather than disappearing", () => {
  // A state added on the backend must not silently render as blank.
  assert.equal(breakerLabel("closed"), "taking traffic");
  assert.equal(breakerLabel("open"), "shut out");
  assert.equal(breakerLabel("half_open"), "one trial pending");
  assert.equal(breakerLabel("quarantined"), "quarantined");
});
