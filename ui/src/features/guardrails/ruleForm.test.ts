import assert from "node:assert/strict";
import test from "node:test";
import {
  describeRule,
  EMPTY_RULE_FORM,
  fromRule,
  requiresSecret,
  toPayload,
  type RuleFormState,
} from "./ruleForm.ts";

const SIGNING_MATERIAL = "webhook-signing-material";

function webhookForm(overrides: Partial<RuleFormState> = {}): RuleFormState {
  return {
    ...EMPTY_RULE_FORM,
    name: "pii-scan",
    url: "https://scanner.internal/check",
    signingSecret: SIGNING_MATERIAL,
    ...overrides,
  };
}

function judgeForm(overrides: Partial<RuleFormState> = {}): RuleFormState {
  return {
    ...EMPTY_RULE_FORM,
    name: "moderation",
    kind: "judge",
    judgeModel: "moderator",
    ...overrides,
  };
}

const STORED = {
  name: "pii-scan",
  kind: "webhook",
  direction: "response",
  fail_policy: "open",
  position: 2,
  model_id: "model-1",
  enabled: false,
  config: { url: "https://scanner.internal/check", timeout_ms: 900 },
};

// ── toPayload ────────────────────────────────────────────────────────────────

test("a webhook payload carries only the knobs that were set", () => {
  const payload = toPayload(webhookForm());

  assert.equal(payload.kind, "webhook");
  // `timeout_ms` is absent, not zero: an unset knob leaves the backend
  // provider's own default as the single place that default is written down.
  assert.deepEqual(payload.config, { url: "https://scanner.internal/check" });
  assert.equal(payload.signing_secret, SIGNING_MATERIAL);
  assert.equal(payload.model_id, null);
});

test("a cleartext webhook url is refused", () => {
  // The payload is the user's prompt, and there is a verdict coming back that
  // the gateway will trust.
  assert.throws(() => toPayload(webhookForm({ url: "http://scanner.internal/check" })), /https/);
});

test("a webhook needs a url and a judge needs a model", () => {
  assert.throws(() => toPayload(webhookForm({ url: "  " })), /url is required/);
  assert.throws(() => toPayload(judgeForm({ judgeModel: "" })), /judge model is required/);
});

test("an empty signing secret is omitted so the stored one is kept", () => {
  // A secret that cannot be read back cannot be resubmitted, so a blank field
  // on an edit has to mean "unchanged" rather than "clear".
  assert.equal("signing_secret" in toPayload(webhookForm({ signingSecret: "   " })), false);
});

test("the timeout is bounded to what a request path tolerates", () => {
  assert.throws(() => toPayload(webhookForm({ timeoutMs: "50" })), /between 100 and 10000/);
  assert.throws(() => toPayload(webhookForm({ timeoutMs: "20000" })), /between/);
  assert.throws(() => toPayload(webhookForm({ timeoutMs: "1.5" })), /whole number/);
  assert.equal(toPayload(webhookForm({ timeoutMs: "1500" })).config.timeout_ms, 1500);
});

test("the judge char budget is bounded", () => {
  assert.throws(() => toPayload(judgeForm({ charBudget: "1" })), /between 100 and 20000/);
  assert.equal(toPayload(judgeForm({ charBudget: "500" })).config.char_budget, 500);
});

test("no selected category means the key is omitted, not sent empty", () => {
  // Omitted means "every category blocks" on the backend, which is the stricter
  // reading — the right default for an empty selection.
  assert.equal(toPayload(judgeForm()).config.block_categories, undefined);
  assert.deepEqual(toPayload(judgeForm({ blockCategories: ["hate"] })).config.block_categories, [
    "hate",
  ]);
});

test("a name is required and a position cannot be negative", () => {
  assert.throws(() => toPayload(webhookForm({ name: " " })), /name is required/);
  assert.throws(() => toPayload(webhookForm({ position: "-1" })), /position/);
  assert.equal(toPayload(webhookForm({ position: "" })).position, 0);
});

test("an empty scope means team-wide", () => {
  const payload = toPayload(webhookForm({ scope: "" }));
  assert.equal(payload.model_id, null);
  assert.equal(payload.router_id, null);
});

test("a model scope fills model_id and leaves router_id null", () => {
  const payload = toPayload(webhookForm({ scope: "model:abc" }));
  assert.equal(payload.model_id, "abc");
  assert.equal(payload.router_id, null);
});

test("a router scope fills router_id and leaves model_id null", () => {
  // The two are mutually exclusive: the backend refuses a rule carrying both,
  // because resolve_chain would only ever honour the router tier.
  const payload = toPayload(webhookForm({ scope: "router:r-1" }));
  assert.equal(payload.router_id, "r-1");
  assert.equal(payload.model_id, null);
});

test("the other kind's config is never carried along", () => {
  // Switching kind in the form must not leave `judge_model` on a webhook rule:
  // the backend rejects unknown config keys, and rightly so.
  const payload = toPayload(webhookForm({ judgeModel: "leftover", charBudget: "500" }));
  assert.deepEqual(payload.config, { url: "https://scanner.internal/check" });
});

// ── requiresSecret ───────────────────────────────────────────────────────────

test("a new webhook rule with no secret typed is refused before the round trip", () => {
  assert.equal(requiresSecret(webhookForm({ signingSecret: "" }), false), true);
});

test("a webhook rule that already has a stored secret does not need one typed", () => {
  assert.equal(requiresSecret(webhookForm({ signingSecret: "" }), true), false);
});

test("a judge rule needs no secret, because it signs nothing", () => {
  assert.equal(requiresSecret(judgeForm({ signingSecret: "" }), false), false);
});

// ── fromRule ─────────────────────────────────────────────────────────────────

test("a stored rule round-trips into the form", () => {
  const form = fromRule(STORED);

  assert.equal(form.name, "pii-scan");
  assert.equal(form.kind, "webhook");
  assert.equal(form.direction, "response");
  assert.equal(form.failPolicy, "open");
  assert.equal(form.position, "2");
  assert.equal(form.scope, "model:model-1");
  assert.equal(form.enabled, false);
  assert.equal(form.url, "https://scanner.internal/check");
  assert.equal(form.timeoutMs, "900");
});

test("the signing secret field is always blank when editing", () => {
  // It is never returned by any endpoint; putting anything there — even a
  // placeholder that could be submitted — would misstate what is stored.
  assert.equal(fromRule(STORED).signingSecret, "");
});

test("judge categories are read back, defaulting to none", () => {
  const judge = { ...STORED, kind: "judge", config: { judge_model: "m" } };
  assert.deepEqual(fromRule(judge).blockCategories, []);
  assert.deepEqual(
    fromRule({ ...judge, config: { judge_model: "m", block_categories: ["hate"] } })
      .blockCategories,
    ["hate"],
  );
});

// ── describeRule ─────────────────────────────────────────────────────────────

test("a team-wide rule is described by side, scope, fail policy and target", () => {
  assert.equal(
    describeRule({
      name: "x",
      kind: "webhook",
      direction: "request",
      fail_policy: "closed",
      position: 0,
      model_id: null,
      enabled: true,
      config: { url: "https://s/x" },
    }),
    "request · all models · fail closed · https://s/x",
  );
});

test("a model-scoped rule says so", () => {
  assert.equal(
    describeRule({
      name: "x",
      kind: "judge",
      direction: "response",
      fail_policy: "open",
      position: 1,
      model_id: "m1",
      enabled: true,
      config: { judge_model: "moderator" },
    }),
    "response · one model · fail open · moderator",
  );
});

test("widening a scoped rule back to the whole team is asked for explicitly", () => {
  // On a PATCH the backend reads a null scope as "leave it alone", so sending
  // two nulls answered 200 and changed nothing: the operator believed a control
  // now covered every model when it still covered one.
  const widened = toPayload(webhookForm({ scope: "" }), { forUpdate: true });

  assert.equal(widened.clear_scope, true);
  assert.equal(widened.model_id, null);
  assert.equal(widened.router_id, null);
});

test("a create never asks to clear the scope", () => {
  // There is nothing to widen yet, and the create DTO has no such field.
  assert.equal(toPayload(webhookForm({ scope: "" })).clear_scope, undefined);
});

test("scoping an update to a router does not also ask to clear it", () => {
  const scoped = toPayload(webhookForm({ scope: "router:router-1" }), { forUpdate: true });

  assert.equal(scoped.clear_scope, undefined);
  assert.equal(scoped.router_id, "router-1");
  assert.equal(scoped.model_id, null);
});
