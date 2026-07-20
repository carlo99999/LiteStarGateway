import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import {
  fetchAllPages,
  pageRequest,
  pageResult,
  type PageRequest,
  type PageResult,
} from "@/lib/api/pagination";

export type Model = components["schemas"]["ModelResponse"];
export type ModelType = components["schemas"]["ModelType"];
export type Provider = components["schemas"]["Provider"];

/** The fields the create form collects. Costs and ceilings are optional. */
export interface NewModel {
  name: string;
  provider: Provider;
  credentialId: string;
  type: ModelType;
  providerModelId: string;
  maxOutputTokens: number | null;
  apiVersion: string | null;
  inputCostPerToken: number | null;
  outputCostPerToken: number | null;
  enabled: boolean;
}

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

async function requestModels(teamId: string, request: PageRequest): Promise<Model[]> {
  const { data, error } = await api.GET("/teams/{team_id}/models", {
    params: { path: { team_id: teamId }, query: request },
  });
  if (error || !data) throw fail(error, "Failed to load models");
  return data;
}

/** GET /teams/{id}/models — one table page. */
export async function listModelsPage(teamId: string, offset: number): Promise<PageResult<Model>> {
  const request = pageRequest(offset);
  return pageResult(await requestModels(teamId, request), offset);
}

/** Complete team model collection, for selectors (e.g. router candidates). */
export async function listAllModels(teamId: string, signal?: AbortSignal): Promise<Model[]> {
  return fetchAllPages(
    async (request) => {
      const { data, error } = await api.GET("/teams/{team_id}/models", {
        params: { path: { team_id: teamId }, query: request },
        signal,
      });
      if (error || !data) throw fail(error, "Failed to load models");
      return data;
    },
    { keyOf: (model) => model.id },
  );
}

/** POST /teams/{id}/models — create a model deployment. `provider` must match
 * the referenced credential's provider or the request is rejected (400). */
export async function createModel(teamId: string, model: NewModel): Promise<Model> {
  const { data, error } = await api.POST("/teams/{team_id}/models", {
    params: { path: { team_id: teamId } },
    body: {
      name: model.name,
      provider: model.provider,
      credential_id: model.credentialId,
      type: model.type,
      provider_model_id: model.providerModelId,
      max_output_tokens: model.maxOutputTokens,
      api_version: model.apiVersion,
      input_cost_per_token: model.inputCostPerToken,
      output_cost_per_token: model.outputCostPerToken,
      enabled: model.enabled,
    },
  });
  if (error || !data) throw fail(error, "Failed to create model");
  return data;
}

/** PATCH /teams/{id}/models/{modelId} — enable or disable a model. */
export async function setModelEnabled(
  teamId: string,
  modelId: string,
  enabled: boolean,
): Promise<Model> {
  const { data, error } = await api.PATCH("/teams/{team_id}/models/{model_id}", {
    params: { path: { team_id: teamId, model_id: modelId } },
    body: { enabled },
  });
  if (error || !data) throw fail(error, "Failed to change model status");
  return data;
}

/** DELETE /teams/{id}/models/{modelId} — remove a model. */
export async function deleteModel(teamId: string, modelId: string): Promise<void> {
  const { error } = await api.DELETE("/teams/{team_id}/models/{model_id}", {
    params: { path: { team_id: teamId, model_id: modelId } },
  });
  if (error) throw fail(error, "Failed to delete model");
}
