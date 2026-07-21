import { useMutation, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal } from "lucide-react";
import { StatusDot } from "@/components/common/StatusDot";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { PROVIDER_LABELS } from "@/features/credentials/providerFields";
import { setModelEnabled, type CallableModel, type Model } from "@/features/models/api";

interface CallableModelsTableProps {
  rows: CallableModel[] | undefined;
  isLoading: boolean;
  error: Error | null;
  credentialNames: Map<string, string>;
  /** Team id → name, to label an extended model with its source team. */
  teamNames: Map<string, string>;
  onEdit: (model: Model) => void;
  onDelete: (model: Model) => void;
  onExtend: (model: Model) => void;
}

function formatCost(perToken: number | null): string {
  if (perToken === null) return "—";
  return `$${(perToken * 1_000_000).toFixed(2)}/M`;
}

/** The models a team can call — its own, plus the ones extended to it and the
 * global ones (read-only here; managed from their source or the global list).
 * Only own models carry the edit/enable/extend/delete actions. */
export function CallableModelsTable({
  rows,
  isLoading,
  error,
  credentialNames,
  teamNames,
  onEdit,
  onDelete,
  onExtend,
}: CallableModelsTableProps) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: (model: Model) => setModelEnabled(model.team_id as string, model.id, !model.enabled),
    onSettled: (_data, _err, model) =>
      queryClient.invalidateQueries({ queryKey: ["team-models", model.team_id] }),
  });

  function originBadge(c: CallableModel) {
    if (c.origin === "own") return <Badge variant="muted">team</Badge>;
    if (c.origin === "global") return <Badge variant="accent">global</Badge>;
    const source = c.source_team_id ? teamNames.get(c.source_team_id) : undefined;
    return <Badge variant="accent">extended · {source ?? "another team"}</Badge>;
  }

  const columns: Column<CallableModel>[] = [
    {
      key: "status",
      header: "",
      className: "w-8",
      cell: (c) =>
        c.model.enabled ? (
          <StatusDot tone="green" />
        ) : (
          <span className="font-mono text-[9px] uppercase text-muted-foreground/60">off</span>
        ),
    },
    {
      key: "name",
      header: "name",
      cell: (c) => (
        <span className="flex flex-col">
          <span className="text-foreground">{c.alias}</span>
          <span className="text-[10px] text-muted-foreground">{c.model.provider_model_id}</span>
        </span>
      ),
    },
    { key: "type", header: "type", cell: (c) => <Badge variant="muted">{c.model.type}</Badge> },
    { key: "origin", header: "origin", cell: originBadge },
    {
      key: "provider",
      header: "provider",
      cell: (c) => (
        <span className="text-xs text-muted-foreground">
          {PROVIDER_LABELS[c.model.provider] ?? c.model.provider}
        </span>
      ),
    },
    {
      key: "credential",
      header: "credential",
      cell: (c) => (
        <span className="text-xs text-muted-foreground">
          {credentialNames.get(c.model.credential_id) ?? `${c.model.credential_id.slice(0, 8)}…`}
        </span>
      ),
    },
    {
      key: "cost",
      header: "cost (in/out)",
      numeric: true,
      cell: (c) => (
        <span className="tabular text-xs text-muted-foreground">
          {formatCost(c.model.input_cost_per_token)} / {formatCost(c.model.output_cost_per_token)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      className: "w-12",
      numeric: true,
      cell: (c) =>
        // Extended/global models are managed from their source, not here.
        c.origin !== "own" ? (
          <span className="pr-2 text-[10px] text-muted-foreground/60">read-only</span>
        ) : (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                aria-label={`Actions for ${c.alias}`}
                disabled={toggle.isPending}
              >
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => onEdit(c.model)}>edit</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => onExtend(c.model)}>extend…</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => toggle.mutate(c.model)}>
                {c.model.enabled ? "disable" : "enable"}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem className="text-destructive" onSelect={() => onDelete(c.model)}>
                delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        ),
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(c) => `${c.origin}:${c.alias}`}
      isLoading={isLoading}
      error={error}
      emptyTitle="no models"
      emptyDescription="This team can't call any model yet."
    />
  );
}
