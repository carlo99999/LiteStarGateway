/** Pure form logic for a guardrail rule: text fields in, request payload out.
 *
 * Kept out of the component so the interesting parts are testable without a
 * DOM. The rules mirror the backend's `domain/guardrail_config.py` — not to
 * replace it (the server stays the authority; a console is not a security
 * boundary) but so an operator gets the error next to the field instead of as a
 * round-trip 400.
 */

export const GUARDRAIL_KINDS = ["webhook", "judge"] as const;
export const GUARDRAIL_DIRECTIONS = ["request", "response"] as const;
export const FAIL_POLICIES = ["closed", "open"] as const;
/** Mirrors `JUDGE_CATEGORIES` in the backend's guardrail config. */
export const JUDGE_CATEGORIES = [
  "harassment",
  "hate",
  "self_harm",
  "sexual",
  "violence",
  "illicit",
  "prompt_injection",
] as const;

export type GuardrailKind = (typeof GUARDRAIL_KINDS)[number];
export type GuardrailDirection = (typeof GUARDRAIL_DIRECTIONS)[number];
export type FailPolicy = (typeof FAIL_POLICIES)[number];

export const MIN_TIMEOUT_MS = 100;
export const MAX_TIMEOUT_MS = 10_000;
export const MIN_CHAR_BUDGET = 100;
export const MAX_CHAR_BUDGET = 20_000;

export interface RuleFormState {
  name: string;
  kind: GuardrailKind;
  direction: GuardrailDirection;
  failPolicy: FailPolicy;
  position: string;
  /** Scope, as one select value: "" (team-wide), "model:<id>" or "router:<id>". */
  scope: string;
  enabled: boolean;
  /** webhook */
  url: string;
  timeoutMs: string;
  signingSecret: string;
  /** judge */
  judgeModel: string;
  charBudget: string;
  blockCategories: readonly string[];
}

export const EMPTY_RULE_FORM: RuleFormState = {
  name: "",
  kind: "webhook",
  direction: "request",
  // Defaulting to `closed` is the deliberate choice: a control that could not
  // run has not passed, and an operator who wants the advisory behaviour should
  // have to say so.
  failPolicy: "closed",
  position: "0",
  scope: "",
  enabled: true,
  url: "",
  timeoutMs: "",
  signingSecret: "",
  judgeModel: "",
  charBudget: "",
  blockCategories: [],
};

export interface RulePayload {
  name: string;
  kind: GuardrailKind;
  direction: GuardrailDirection;
  fail_policy: FailPolicy;
  position: number;
  model_id: string | null;
  router_id: string | null;
  enabled: boolean;
  config: Record<string, unknown>;
  signing_secret?: string;
  /** Only meaningful on an update: an omitted field means "unchanged", so a null
   * scope cannot also mean "widen this back to the whole team". */
  clear_scope?: boolean;
}


export const SCOPE_TEAM_WIDE = "";

/** Encode a scope pick for the single select. */
export function scopeValue(kind: "model" | "router", id: string): string {
  return `${kind}:${id}`;
}

/** Split a scope value into the two mutually exclusive API fields. The backend
 * refuses a rule carrying both, so exactly one is ever non-null. */
export function scopeToFields(scope: string): {
  model_id: string | null;
  router_id: string | null;
} {
  if (scope.startsWith("model:")) return { model_id: scope.slice(6), router_id: null };
  if (scope.startsWith("router:")) return { model_id: null, router_id: scope.slice(7) };
  return { model_id: null, router_id: null };
}

function optionalInt(text: string, label: string, low: number, high: number): number | undefined {
  const trimmed = text.trim();
  if (trimmed === "") return undefined;
  const value = Number(trimmed);
  if (!Number.isInteger(value)) throw new Error(`${label} must be a whole number`);
  if (value < low || value > high) {
    throw new Error(`${label} must be between ${low} and ${high}`);
  }
  return value;
}

/** Build the POST/PATCH body, or throw with a message meant for the operator.
 *
 * `forUpdate` matters for one field. On a PATCH the backend reads an omitted or
 * null scope as "leave it alone", so picking "all models" used to submit two
 * nulls, answer 200, and change nothing — an operator would believe they had
 * widened a content control that still covered one model. Widening is asked for
 * explicitly instead. */
export function toPayload(form: RuleFormState, { forUpdate = false } = {}): RulePayload {
  const name = form.name.trim();
  if (name === "") throw new Error("name is required");
  const position = Number(form.position.trim() === "" ? "0" : form.position);
  if (!Number.isInteger(position) || position < 0) {
    throw new Error("position must be zero or a positive whole number");
  }
  const config: Record<string, unknown> = {};
  if (form.kind === "webhook") {
    const url = form.url.trim();
    if (url === "") throw new Error("url is required for a webhook guardrail");
    if (!url.startsWith("https://")) {
      // The payload is the user's prompt: cleartext is not a choice to make by
      // accident, and there is a verdict coming back that we will trust.
      throw new Error("url must be https");
    }
    config.url = url;
    const timeout = optionalInt(form.timeoutMs, "timeout", MIN_TIMEOUT_MS, MAX_TIMEOUT_MS);
    if (timeout !== undefined) config.timeout_ms = timeout;
  } else {
    const judgeModel = form.judgeModel.trim();
    if (judgeModel === "") throw new Error("judge model is required for a judge guardrail");
    config.judge_model = judgeModel;
    const budget = optionalInt(form.charBudget, "char budget", MIN_CHAR_BUDGET, MAX_CHAR_BUDGET);
    if (budget !== undefined) config.char_budget = budget;
    if (form.blockCategories.length > 0) config.block_categories = [...form.blockCategories];
  }
  const payload: RulePayload = {
    name,
    kind: form.kind,
    direction: form.direction,
    fail_policy: form.failPolicy,
    position,
    ...scopeToFields(form.scope),
    enabled: form.enabled,
    config,
  };
  if (forUpdate && form.scope === SCOPE_TEAM_WIDE) payload.clear_scope = true;
  const secret = form.signingSecret.trim();
  // Omitted rather than sent empty: on an edit, omission is what tells the
  // backend to keep the stored secret, and a secret nobody can read back
  // cannot be resubmitted.
  if (secret !== "") payload.signing_secret = secret;
  return payload;
}

/** Refuse a create that the backend would refuse anyway, with a clearer
 * message: a webhook rule needs a secret to sign with, and `has_secret` on an
 * existing rule is what makes the field optional on an edit. */
export function requiresSecret(form: RuleFormState, hasStoredSecret: boolean): boolean {
  return form.kind === "webhook" && !hasStoredSecret && form.signingSecret.trim() === "";
}

/** The stored shape as the generated API types describe it: `model_id` and
 * `config` are optional there, so they are optional here rather than being
 * asserted away at the call site. */
interface StoredRule {
  name: string;
  kind: string;
  direction: string;
  fail_policy: string;
  position: number;
  enabled: boolean;
  model_id?: string | null;
  router_id?: string | null;
  config?: Record<string, unknown>;
}

function asString(value: unknown): string {
  return value === undefined || value === null ? "" : String(value);
}

function asKind(value: string): GuardrailKind {
  return value === "judge" ? "judge" : "webhook";
}

/** Fill the form from a stored rule, for editing. The signing secret is always
 * blank: it is never returned by any endpoint, and a blank field means
 * "unchanged" all the way to the database. */
export function fromRule(rule: StoredRule): RuleFormState {
  const config = rule.config ?? {};
  const categories = config.block_categories;
  return {
    name: rule.name,
    kind: asKind(rule.kind),
    direction: rule.direction === "response" ? "response" : "request",
    failPolicy: rule.fail_policy === "open" ? "open" : "closed",
    position: String(rule.position),
    scope: rule.router_id
      ? scopeValue("router", rule.router_id)
      : rule.model_id
        ? scopeValue("model", rule.model_id)
        : SCOPE_TEAM_WIDE,
    enabled: rule.enabled,
    url: asString(config.url),
    timeoutMs: asString(config.timeout_ms),
    signingSecret: "",
    judgeModel: asString(config.judge_model),
    charBudget: asString(config.char_budget),
    blockCategories: Array.isArray(categories) ? categories.map(String) : [],
  };
}

/** Human summary of what a rule does, for the list. */
export function describeRule(rule: StoredRule): string {
  const scope = rule.router_id ? "one router" : rule.model_id ? "one model" : "all models";
  const config = rule.config ?? {};
  const target = rule.kind === "webhook" ? asString(config.url) : asString(config.judge_model);
  return `${rule.direction} · ${scope} · fail ${rule.fail_policy} · ${target}`;
}
