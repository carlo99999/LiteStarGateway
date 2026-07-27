import assert from "node:assert/strict";
import test from "node:test";
import { formatThresholds, normalizeOptional, parseThresholds } from "./alertConfig.ts";

test("parses, sorts and de-duplicates a comma-separated list", () => {
  assert.deepEqual(parseThresholds("80, 50, 80, 100"), [50, 80, 100]);
});

test("a blank string yields no thresholds", () => {
  assert.deepEqual(parseThresholds(""), []);
  assert.deepEqual(parseThresholds("   "), []);
});

test("rejects non-integers and out-of-range values", () => {
  assert.throws(() => parseThresholds("50, x"), /not a whole number/);
  assert.throws(() => parseThresholds("1.5"), /not a whole number/);
  assert.throws(() => parseThresholds("0"), /between 1 and 100/);
  assert.throws(() => parseThresholds("101"), /between 1 and 100/);
});

test("formatThresholds round-trips a parsed list", () => {
  assert.equal(formatThresholds([50, 80, 100]), "50, 80, 100");
  assert.equal(formatThresholds(parseThresholds("100, 50")), "50, 100");
});

test("normalizeOptional maps blank input to null and trims otherwise", () => {
  assert.equal(normalizeOptional(""), null);
  assert.equal(normalizeOptional("   "), null);
  assert.equal(normalizeOptional("  a@b.com "), "a@b.com");
});
