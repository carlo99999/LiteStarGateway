import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { RulePayload } from "@/features/guardrails/ruleForm";

export type GuardrailRule = components["schemas"]["GuardrailRuleResponse"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** GET /teams/{id}/guardrails — the team's chain, in position order. */
export async function listGuardrailRules(teamId: string): Promise<GuardrailRule[]> {
  const { data, error } = await api.GET("/teams/{team_id}/guardrails", {
    params: { path: { team_id: teamId } },
  });
  if (error || !data) throw fail(error, "Failed to load guardrails");
  return data;
}

/** POST /teams/{id}/guardrails — add one provider to the chain. */
export async function createGuardrailRule(
  teamId: string,
  payload: RulePayload,
): Promise<GuardrailRule> {
  const { data, error } = await api.POST("/teams/{team_id}/guardrails", {
    params: { path: { team_id: teamId } },
    body: payload as never,
  });
  if (error || !data) throw fail(error, "Failed to create the guardrail");
  return data;
}

/** PATCH /teams/{id}/guardrails/{rule} — partial update. A payload without
 * `signing_secret` keeps the stored one. */
export async function updateGuardrailRule(
  teamId: string,
  ruleId: string,
  payload: Partial<RulePayload>,
): Promise<GuardrailRule> {
  const { data, error } = await api.PATCH("/teams/{team_id}/guardrails/{rule_id}", {
    params: { path: { team_id: teamId, rule_id: ruleId } },
    body: payload as never,
  });
  if (error || !data) throw fail(error, "Failed to update the guardrail");
  return data;
}

/** DELETE /teams/{id}/guardrails/{rule} — remove the rule from the chain. */
export async function deleteGuardrailRule(teamId: string, ruleId: string): Promise<void> {
  const { error } = await api.DELETE("/teams/{team_id}/guardrails/{rule_id}", {
    params: { path: { team_id: teamId, rule_id: ruleId } },
  });
  if (error) throw fail(error, "Failed to remove the guardrail");
}
