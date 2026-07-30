import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { canManageTools } from "@/features/teams/access";
import { useAccessibleTeams } from "@/features/teams/useAccessibleTeams";
import { KeyToolPolicyPanel } from "@/features/tools/KeyToolPolicyPanel";
import {
  createMcpServer,
  declareToolEffect,
  discoverMcpTools,
  listMcpServers,
  listMcpTools,
  reattachMcpServer,
  removeMcpServer,
  updateMcpServer,
  type McpServer,
} from "@/features/tools/api";
import {
  describeEffect,
  describeInventory,
  describeLastDiscovery,
  describeOrigin,
  describeRemoval,
  removalOutcome,
  resolveInventory,
  type ServerOrigin,
  type ToolEffect,
} from "@/features/tools/inventory";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

const EFFECTS: readonly ToolEffect[] = ["read", "write", "destructive"];

/** One server's stored inventory, with the three empty-ish states kept apart.
 *
 * A failed query renders as an error, never as "no tools" — the defect this
 * console has produced in three separate rounds. And an empty inventory is two
 * different facts here: nobody has run discovery, or the server genuinely offers
 * nothing. Only the first is the operator's to act on. */
function Inventory({
  teamId,
  server,
  canDeclare,
}: {
  teamId: string;
  server: McpServer;
  canDeclare: boolean;
}) {
  const queryClient = useQueryClient();
  const tools = useQuery({
    queryKey: ["teams", teamId, "mcp-servers", server.id, "tools"],
    queryFn: () => listMcpTools(teamId, server.id),
  });
  const declare = useMutation({
    mutationFn: ({ name, effect }: { name: string; effect: ToolEffect }) =>
      declareToolEffect(teamId, server.id, name, effect),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ["teams", teamId, "mcp-servers", server.id, "tools"],
      }),
  });

  const view = resolveInventory({
    isLoading: tools.isLoading,
    isError: tools.isError,
    errorMessage: toError(tools.error)?.message ?? null,
    tools: tools.data,
    lastDiscoveredAt: server.last_discovered_at,
  });

  if (view.kind !== "tools") {
    return (
      <p
        className={
          view.kind === "error"
            ? "font-mono text-xs text-destructive"
            : "font-mono text-xs text-muted-foreground"
        }
      >
        {view.kind === "error" ? describeInventory(view) : `// ${describeInventory(view)}`}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {(tools.data ?? []).map((tool) => (
        <div key={tool.name} className="flex flex-wrap items-center gap-3">
          <span className="min-w-48 font-mono text-xs text-foreground">{tool.name}</span>
          <span className="font-mono text-xs text-muted-foreground">
            {describeEffect(tool.effect as ToolEffect)}
          </span>
          {canDeclare ? (
            <select
              className={SELECT_CLASS}
              value={tool.effect}
              disabled={declare.isPending}
              onChange={(event) =>
                declare.mutate({ name: tool.name, effect: event.target.value as ToolEffect })
              }
            >
              {EFFECTS.map((effect) => (
                <option key={effect} value={effect}>
                  {effect}
                </option>
              ))}
            </select>
          ) : null}
          {tool.description ? (
            <span className="font-mono text-xs text-muted-foreground">{tool.description}</span>
          ) : null}
        </div>
      ))}
      {declare.error ? (
        <p className="font-mono text-xs text-destructive">
          ! {toError(declare.error)?.message}
        </p>
      ) : null}
    </div>
  );
}

/** MCP tool servers a team can use: its own, those the platform extended to it,
 * and global ones — minus any it detached.
 *
 * Bearer tokens are write-only: the console can see that one exists, never what
 * it is. Effects are declared by an operator and discovery never overwrites them,
 * so the select below is the authoritative value rather than the server's hint. */
export function ToolsPage() {
  const queryClient = useQueryClient();
  const teams = useAccessibleTeams(canManageTools);
  const [teamId, setTeamId] = useState("");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [auth, setAuth] = useState("");
  const [reattachId, setReattachId] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const servers = useQuery({
    queryKey: ["teams", teamId, "mcp-servers"],
    queryFn: () => listMcpServers(teamId),
    enabled: teamId.length > 0,
  });

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);
  useEffect(() => {
    setNotice(null);
  }, [teamId]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["teams", teamId, "mcp-servers"] });

  const create = useMutation({
    mutationFn: () =>
      createMcpServer(teamId, {
        name,
        url,
        // An empty field means "no token", not "an empty token".
        auth: auth.trim() ? auth.trim() : null,
      }),
    onSuccess: () => {
      setName("");
      setUrl("");
      setAuth("");
      setNotice(null);
      return invalidate();
    },
  });
  const discover = useMutation({
    mutationFn: (server: McpServer) => discoverMcpTools(teamId, server.id, true),
    onSuccess: async (tools) => {
      setNotice(`discovery returned ${tools.length} tool${tools.length === 1 ? "" : "s"}`);
      await queryClient.invalidateQueries({ queryKey: ["teams", teamId, "mcp-servers"] });
    },
  });
  const remove = useMutation({
    mutationFn: (server: McpServer) => removeMcpServer(teamId, server.id),
    // The gateway says whether it deleted or detached; a console that assumed
    // "gone" would hide the fact that a shared server is still live elsewhere.
    onSuccess: async (outcome) => {
      setNotice(removalOutcome(outcome));
      await invalidate();
    },
  });
  const reattach = useMutation({
    mutationFn: (serverId: string) => reattachMcpServer(teamId, serverId),
    onSuccess: async (server) => {
      setNotice(`reattached ${server.name} to this team`);
      setReattachId("");
      await invalidate();
    },
  });
  const toggle = useMutation({
    mutationFn: (server: McpServer) =>
      updateMcpServer(teamId, server.id, { enabled: !server.enabled }),
    onSuccess: invalidate,
  });

  const busy =
    create.isPending ||
    discover.isPending ||
    remove.isPending ||
    reattach.isPending ||
    toggle.isPending;
  const mutationError =
    toError(create.error ?? discover.error ?? remove.error ?? reattach.error ?? toggle.error)
      ?.message ?? null;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <div>
      <PageHeader
        command="tools list"
        title="Tools"
        description="MCP tool servers this team can use. A server must point inside MCP_ALLOWED_HOSTS, which the gateway re-checks on every call, not only when it is registered. Tool effects are declared by an operator — a server's own hints only seed a tool nobody has classified yet, and an unclassified tool counts as destructive."
      />

      <div className="mb-4 flex items-center gap-3">
        <Label htmlFor="tools-team">team</Label>
        <select
          id="tools-team"
          className={SELECT_CLASS + " min-w-64"}
          value={teamId}
          onChange={(event) => setTeamId(event.target.value)}
          disabled={teams.isLoading || teams.isError}
        >
          <option value="" disabled>
            {teams.isLoading
              ? "loading…"
              : teams.isError
                ? "failed to load teams"
                : "select a team"}
          </option>
          {(teams.data ?? []).map((team) => (
            <option key={team.id} value={team.id}>
              {team.name}
            </option>
          ))}
        </select>
      </div>

      {servers.isError ? (
        <p className="mb-6 font-mono text-xs text-destructive">
          ! couldn&apos;t load the tool servers — {toError(servers.error)?.message}
        </p>
      ) : (servers.data ?? []).length === 0 && teamId && !servers.isLoading ? (
        <p className="mb-6 font-mono text-xs text-muted-foreground">
          // no tool servers — models called through this team can use no tools.
        </p>
      ) : (
        <div className="mb-6 flex flex-col gap-3">
          {(servers.data ?? []).map((server) => {
            const origin = server.origin as ServerOrigin;
            const removal = describeRemoval(origin);
            return (
              <div
                key={server.id}
                className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4"
              >
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                    {server.origin}
                  </span>
                  <div className="min-w-64 flex-1">
                    <p className="text-foreground">
                      {server.name}
                      {server.enabled ? null : (
                        <span className="ml-2 font-mono text-xs text-muted-foreground">
                          // disabled
                        </span>
                      )}
                    </p>
                    <p className="font-mono text-xs text-muted-foreground">
                      {server.url} — {describeOrigin(origin)}
                    </p>
                    <p className="font-mono text-xs text-muted-foreground">
                      last discovery: {describeLastDiscovery(server.last_discovered_at)}
                      {server.has_auth ? " — authenticated" : " — no token"}
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => discover.mutate(server)}
                  >
                    discover
                  </Button>
                  {origin === "own" ? (
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy}
                      onClick={() => toggle.mutate(server)}
                    >
                      {server.enabled ? "disable" : "enable"}
                    </Button>
                  ) : null}
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    title={removal.consequence}
                    onClick={() => remove.mutate(server)}
                  >
                    {removal.verb}
                  </Button>
                </div>
                {/* Named next to the button, not only in a tooltip: "remove"
                    meaning two different things is how a team admin discovers
                    they revoked a capability from every other tenant. */}
                <p className="font-mono text-xs text-muted-foreground">
                  {removal.verb} → {removal.consequence}
                </p>
                <Inventory teamId={teamId} server={server} canDeclare={origin === "own"} />
              </div>
            );
          })}
        </div>
      )}

      {notice ? <p className="mb-4 font-mono text-xs text-muted-foreground">// {notice}</p> : null}
      {mutationError ? (
        <p className="mb-4 font-mono text-xs text-destructive">! {mutationError}</p>
      ) : null}

      {/* A detach is reversible, and a console that could not undo it would make
          the detach read as a deletion. A detached server is by definition absent
          from the list above, so the only handle left is its id — which the
          gateway still reports in the audit trail. */}
      <div className="mb-6 flex flex-wrap items-end gap-3">
        <div className="grid gap-2">
          <Label htmlFor="tools-reattach">reattach a detached server (id)</Label>
          <Input
            id="tools-reattach"
            value={reattachId}
            onChange={(event) => setReattachId(event.target.value)}
            placeholder="uuid from the audit trail"
          />
        </div>
        <Button
          variant="outline"
          disabled={busy || !reattachId.trim()}
          onClick={() => reattach.mutate(reattachId.trim())}
        >
          reattach
        </Button>
      </div>

      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          // register a tool server
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-2">
            <Label htmlFor="tools-name">name</Label>
            <Input
              id="tools-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="github"
              required
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="tools-url">url (https)</Label>
            <Input
              id="tools-url"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://tools.internal:8443/mcp"
              required
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="tools-auth">bearer token (optional)</Label>
            <Input
              id="tools-auth"
              type="password"
              value={auth}
              onChange={(event) => setAuth(event.target.value)}
              placeholder="never returned once saved"
            />
          </div>
          <Button type="submit" disabled={busy || !teamId}>
            register
          </Button>
        </div>
      </form>

      {teamId ? <KeyToolPolicyPanel teamId={teamId} /> : null}
    </div>
  );
}
