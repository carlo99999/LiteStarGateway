import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type McpServer = components["schemas"]["McpServerResponse"];
export type McpTool = components["schemas"]["McpToolResponse"];
export type KeyToolPolicy = components["schemas"]["KeyToolPolicyResponse"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** GET /teams/{id}/mcp-servers — own + extended + global, minus what this team
 * detached. Bearer tokens are never returned, only `has_auth`. */
export async function listMcpServers(teamId: string): Promise<McpServer[]> {
  const { data, error } = await api.GET("/teams/{team_id}/mcp-servers", {
    params: { path: { team_id: teamId } },
  });
  if (error || !data) throw fail(error, "Failed to load the tool servers");
  return data;
}

/** GET /teams/{id}/mcp-servers/{server}/tools — the stored inventory. Does not
 * contact the server; discovery is a separate, explicit action. */
export async function listMcpTools(teamId: string, serverId: string): Promise<McpTool[]> {
  const { data, error } = await api.GET("/teams/{team_id}/mcp-servers/{server_id}/tools", {
    params: { path: { team_id: teamId, server_id: serverId } },
  });
  if (error || !data) throw fail(error, "Failed to load the inventory");
  return data;
}

export interface CreateServerPayload {
  name: string;
  url: string;
  auth?: string | null;
  tool_allowlist?: string[] | null;
}

/** POST /teams/{id}/mcp-servers — the url must be https and inside
 * `MCP_ALLOWED_HOSTS`, which is checked by the gateway, not here. */
export async function createMcpServer(
  teamId: string,
  payload: CreateServerPayload,
): Promise<McpServer> {
  const { data, error } = await api.POST("/teams/{team_id}/mcp-servers", {
    params: { path: { team_id: teamId } },
    body: payload as never,
  });
  if (error || !data) throw fail(error, "Failed to register the tool server");
  return data;
}

/** PATCH /teams/{id}/mcp-servers/{server} — only for a server this team owns. */
export async function updateMcpServer(
  teamId: string,
  serverId: string,
  payload: { enabled?: boolean; name?: string; url?: string; auth?: string },
): Promise<McpServer> {
  const { data, error } = await api.PATCH("/teams/{team_id}/mcp-servers/{server_id}", {
    params: { path: { team_id: teamId, server_id: serverId } },
    body: payload as never,
  });
  if (error || !data) throw fail(error, "Failed to update the tool server");
  return data;
}

/** POST /teams/{id}/mcp-servers/{server}/discover — asks the server what it
 * offers. Outbound traffic, so it is `tools:manage` and it is audited. Within the
 * inventory TTL the gateway answers from storage unless `force` is set. */
export async function discoverMcpTools(
  teamId: string,
  serverId: string,
  force = false,
): Promise<McpTool[]> {
  const { data, error } = await api.POST("/teams/{team_id}/mcp-servers/{server_id}/discover", {
    params: { path: { team_id: teamId, server_id: serverId }, query: { force } },
  });
  if (error || !data) throw fail(error, "Discovery failed");
  return data;
}

/** DELETE /teams/{id}/mcp-servers/{server} — deletes a server this team owns,
 * detaches one it does not. The response says which happened; the caller must
 * surface it rather than assume the row is gone. */
export async function removeMcpServer(teamId: string, serverId: string): Promise<string> {
  const { data, error } = await api.DELETE("/teams/{team_id}/mcp-servers/{server_id}", {
    params: { path: { team_id: teamId, server_id: serverId } },
  });
  if (error || !data) throw fail(error, "Failed to remove the tool server");
  return data.outcome;
}

/** POST /teams/{id}/mcp-servers/{server}/reattach — undo a detach. Exposed
 * because a detach the console cannot undo would read as a deletion. */
export async function reattachMcpServer(teamId: string, serverId: string): Promise<McpServer> {
  const { data, error } = await api.POST("/teams/{team_id}/mcp-servers/{server_id}/reattach", {
    params: { path: { team_id: teamId, server_id: serverId } },
  });
  if (error || !data) throw fail(error, "Failed to reattach the tool server");
  return data;
}

/** PUT .../tools/{tool}/effect — an operator's classification, which discovery
 * never overwrites. */
export async function declareToolEffect(
  teamId: string,
  serverId: string,
  toolName: string,
  effect: "read" | "write" | "destructive",
): Promise<void> {
  const { error } = await api.PUT(
    "/teams/{team_id}/mcp-servers/{server_id}/tools/{tool_name}/effect",
    {
      params: { path: { team_id: teamId, server_id: serverId, tool_name: toolName } },
      body: { effect } as never,
    },
  );
  if (error) throw fail(error, "Failed to declare the effect");
}

/** GET /teams/{id}/keys/{key}/tool-policy — `restricted: false` is the default
 * state, not an error. */
export async function getKeyToolPolicy(
  teamId: string,
  keyId: string,
): Promise<KeyToolPolicy> {
  const { data, error } = await api.GET("/teams/{team_id}/keys/{key_id}/tool-policy", {
    params: { path: { team_id: teamId, key_id: keyId } },
  });
  if (error || !data) throw fail(error, "Failed to load the key's tool policy");
  return data;
}

/** PUT /teams/{id}/keys/{key}/tool-policy — create or replace. */
export async function setKeyToolPolicy(
  teamId: string,
  keyId: string,
  payload: { allowed_tools?: string[] | null; destructive_enabled?: boolean | null },
): Promise<KeyToolPolicy> {
  const { data, error } = await api.PUT("/teams/{team_id}/keys/{key_id}/tool-policy", {
    params: { path: { team_id: teamId, key_id: keyId } },
    body: payload as never,
  });
  if (error || !data) throw fail(error, "Failed to save the key's tool policy");
  return data;
}

/** DELETE /teams/{id}/keys/{key}/tool-policy — makes the key permissive again,
 * so it is a widening rather than a cleanup. */
export async function clearKeyToolPolicy(teamId: string, keyId: string): Promise<void> {
  const { error } = await api.DELETE("/teams/{team_id}/keys/{key_id}/tool-policy", {
    params: { path: { team_id: teamId, key_id: keyId } },
  });
  if (error) throw fail(error, "Failed to clear the key's tool policy");
}
