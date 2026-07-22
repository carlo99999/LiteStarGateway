import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type PlaygroundResult = components["schemas"]["PlaygroundResultResponse"];

/** POST /playground/compare — run one prompt against several models and get the
 * response, latency, tokens and estimated cost for each. Calls are metered. */
export async function comparePrompt(
  teamId: string,
  modelNames: string[],
  prompt: string,
  maxCompletionTokens: number | null,
): Promise<PlaygroundResult[]> {
  const { data, error } = await api.POST("/playground/compare", {
    body: {
      team_id: teamId,
      model_names: modelNames,
      messages: [{ role: "user", content: prompt }],
      max_completion_tokens: maxCompletionTokens,
    },
  });
  if (error || !data) {
    const env = error as { error?: { message?: string }; detail?: string } | undefined;
    throw new Error(env?.error?.message ?? env?.detail ?? "Comparison failed");
  }
  return data;
}
