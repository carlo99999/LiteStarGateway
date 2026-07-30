import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  approveMcpProposal,
  listMcpProposals,
  proposeMcpServer,
  rejectMcpProposal,
  type McpServerProposal,
} from "@/features/tools/api";
import {
  APPROVE_CONSEQUENCE,
  canDecide,
  describeQueue,
  describeReason,
  describeStatus,
  resolveQueue,
} from "@/features/tools/proposals";
import { toError } from "@/lib/toError";

/** The queue between a member who wants a tool server and an admin who may
 * register one (design §2.4).
 *
 * Both halves live in one panel on purpose. The filing form is reachable by every
 * team role and the decide buttons are not, and putting them on separate pages
 * would leave a member with no way to see what happened to their request — which is
 * the "it disappeared" the rejection reason exists to prevent.
 *
 * The decide buttons are rendered for everyone and refused by the gateway for
 * anyone without `tools:manage`. That is deliberate rather than lazy: the console
 * does not know the caller's role in this team, and hiding a button is not a
 * permission check. The error surfaces as a 403 the same way every other
 * over-reach on this page does.
 */
export function ProposalQueuePanel({ teamId }: { teamId: string }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [auth, setAuth] = useState("");
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const proposals = useQuery({
    queryKey: ["teams", teamId, "mcp-server-proposals"],
    queryFn: () => listMcpProposals(teamId),
    enabled: teamId.length > 0,
  });

  useEffect(() => {
    setNotice(null);
    setReasons({});
  }, [teamId]);

  const invalidate = async () => {
    await queryClient.invalidateQueries({
      queryKey: ["teams", teamId, "mcp-server-proposals"],
    });
    // An approval registers a server, so the list above this panel is stale too.
    await queryClient.invalidateQueries({ queryKey: ["teams", teamId, "mcp-servers"] });
  };

  const propose = useMutation({
    mutationFn: () =>
      proposeMcpServer(teamId, {
        name,
        url,
        // An empty field means "no token", not "an empty token".
        auth: auth.trim() ? auth.trim() : null,
      }),
    onSuccess: async () => {
      setName("");
      setUrl("");
      setAuth("");
      setNotice("proposal filed — a team admin decides whether to register it");
      await invalidate();
    },
  });
  const approve = useMutation({
    mutationFn: (proposal: McpServerProposal) => approveMcpProposal(teamId, proposal.id),
    onSuccess: async (server) => {
      setNotice(`registered ${server.name} — ${server.url}`);
      await invalidate();
    },
  });
  const reject = useMutation({
    mutationFn: (proposal: McpServerProposal) =>
      rejectMcpProposal(teamId, proposal.id, reasons[proposal.id] ?? ""),
    onSuccess: async (proposal) => {
      setNotice(`refused ${proposal.name}`);
      setReasons((current) => ({ ...current, [proposal.id]: "" }));
      await invalidate();
    },
  });

  const busy = propose.isPending || approve.isPending || reject.isPending;
  const mutationError =
    toError(propose.error ?? approve.error ?? reject.error)?.message ?? null;

  const view = resolveQueue({
    isLoading: proposals.isLoading,
    isError: proposals.isError,
    errorMessage: toError(proposals.error)?.message ?? null,
    proposals: proposals.data,
  });

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    propose.mutate();
  }

  return (
    <div className="mt-10 border-t border-border pt-6">
      <p className="mb-1 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // proposals
      </p>
      <p className="mb-4 font-mono text-xs text-muted-foreground">
        Any member of this team may ask for a tool server; a team admin approves or
        refuses it. Filing one changes nothing and contacts nobody — the gateway only
        connects to the server once it is approved.
      </p>

      <p
        className={
          view.kind === "error"
            ? "mb-4 font-mono text-xs text-destructive"
            : "mb-4 font-mono text-xs text-muted-foreground"
        }
      >
        {view.kind === "error" ? describeQueue(view) : `// ${describeQueue(view)}`}
      </p>

      {view.kind === "proposals" ? (
        <div className="mb-6 flex flex-col gap-3">
          {(proposals.data ?? []).map((proposal) => {
            const decidable = canDecide(proposal.status);
            const refusal = describeReason(proposal.status, proposal.reason);
            return (
              <div
                key={proposal.id}
                className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4"
              >
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
                    {proposal.status}
                  </span>
                  <div className="min-w-64 flex-1">
                    <p className="text-foreground">{proposal.name}</p>
                    <p className="font-mono text-xs text-muted-foreground">
                      {proposal.url} — {describeStatus(proposal.status)}
                      {proposal.has_auth ? " — a token was supplied" : " — no token"}
                    </p>
                    {refusal ? (
                      <p className="font-mono text-xs text-muted-foreground">
                        reason: {refusal}
                      </p>
                    ) : null}
                  </div>
                </div>
                {decidable ? (
                  <div className="flex flex-wrap items-end gap-3">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy}
                      title={APPROVE_CONSEQUENCE}
                      onClick={() => approve.mutate(proposal)}
                    >
                      approve
                    </Button>
                    <div className="grid gap-2">
                      <Label htmlFor={`reason-${proposal.id}`}>reason (required to refuse)</Label>
                      <Input
                        id={`reason-${proposal.id}`}
                        value={reasons[proposal.id] ?? ""}
                        onChange={(event) =>
                          setReasons((current) => ({
                            ...current,
                            [proposal.id]: event.target.value,
                          }))
                        }
                        placeholder="what the person who asked will read"
                      />
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy || !(reasons[proposal.id] ?? "").trim()}
                      onClick={() => reject.mutate(proposal)}
                    >
                      refuse
                    </Button>
                  </div>
                ) : null}
                {/* Named rather than left in the tooltip: approving is the moment
                    the gateway first connects somewhere, and the re-check can
                    refuse a proposal that was legal when it was filed. */}
                {decidable ? (
                  <p className="font-mono text-xs text-muted-foreground">
                    approve → {APPROVE_CONSEQUENCE}
                  </p>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}

      {notice ? <p className="mb-4 font-mono text-xs text-muted-foreground">// {notice}</p> : null}
      {mutationError ? (
        <p className="mb-4 font-mono text-xs text-destructive">! {mutationError}</p>
      ) : null}

      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          // propose a tool server
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-2">
            <Label htmlFor="propose-name">name</Label>
            <Input
              id="propose-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="github"
              required
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="propose-url">url (https)</Label>
            <Input
              id="propose-url"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://tools.internal:8443/mcp"
              required
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="propose-auth">bearer token (optional)</Label>
            <Input
              id="propose-auth"
              type="password"
              value={auth}
              onChange={(event) => setAuth(event.target.value)}
              placeholder="never shown again, not even to the approver"
            />
          </div>
          <Button type="submit" disabled={busy || !teamId}>
            propose
          </Button>
        </div>
      </form>
    </div>
  );
}
