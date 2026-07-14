import assert from "node:assert/strict";
import test from "node:test";
import { loadOptionalBudget } from "./budgetRequest.ts";

const budget = { limit_cost: 25, window: "monthly" };

function result<T>(status: number, data?: T, error?: unknown) {
  return { data, error, response: { status } };
}

test("returns budget data from a successful response", async () => {
  assert.equal(await loadOptionalBudget(async () => result(200, budget)), budget);
});

test("returns null only for a 404, with or without an error body", async () => {
  assert.equal(
    await loadOptionalBudget(async () => result(404, undefined, { detail: "Not found" })),
    null,
  );
  assert.equal(await loadOptionalBudget(async () => result(404)), null);
});

test("propagates useful messages for every non-404 HTTP error", async () => {
  for (const [status, error, message] of [
    [401, { detail: "Session expired" }, "Session expired"],
    [403, { error: { message: "Budget access denied" } }, "Budget access denied"],
    [500, undefined, "Failed to load budget"],
  ] as const) {
    await assert.rejects(
      loadOptionalBudget(async () => result(status, undefined, error)),
      new RegExp(message),
    );
  }
});

test("treats a non-404 response without data as an error", async () => {
  await assert.rejects(
    loadOptionalBudget(async () => result(204)),
    /Failed to load budget/,
  );
});

test("turns request rejection into a user-friendly network error", async () => {
  await assert.rejects(
    loadOptionalBudget(async () => {
      throw new TypeError("fetch failed");
    }),
    /Unable to reach the gateway/,
  );
});
