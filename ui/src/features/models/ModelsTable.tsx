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
import { setGlobalModelEnabled, setModelEnabled, type Model } from "@/features/models/api";

interface ModelsTableProps {
  rows: Model[] | undefined;
  isLoading: boolean;
  error: Error | null;
  /** Credential id → display name, so the credential column shows a name. */
  credentialNames: Map<string, string>;
  onEdit: (model: Model) => void;
  onDelete: (model: Model) => void;
  /** Extend a team-owned model to other teams. Omit to hide the action (e.g.
   * for the global-models table, where extension does not apply). */
  onExtend?: (model: Model) => void;
  /** Show a scope column (Global vs Team) — used on the mixed admin view. */
  showScope?: boolean;
}

function formatCost(perToken: number | null): string {
  if (perToken === null) return "—";
  // Costs are per-token; show the friendlier per-1M-tokens figure.
  return `$${(perToken * 1_000_000).toFixed(2)}/M`;
}

export function ModelsTable({
  rows,
  isLoading,
  error,
  credentialNames,
  onEdit,
  onDelete,
  onExtend,
  showScope = false,
}: ModelsTableProps) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    // A global model (team_id === null) toggles through the platform endpoint.
    mutationFn: (model: Model) =>
      model.team_id === null
        ? setGlobalModelEnabled(model.id, !model.enabled)
        : setModelEnabled(model.team_id, model.id, !model.enabled),
    onSettled: (_data, _err, model) => {
      queryClient.invalidateQueries({ queryKey: ["team-models", model.team_id] });
      queryClient.invalidateQueries({ queryKey: ["global-models"] });
    },
  });

  const columns: Column<Model>[] = [
    {
      key: "status",
      header: "",
      className: "w-8",
      cell: (m) =>
        m.enabled ? (
          <StatusDot tone="green" />
        ) : (
          <span className="font-mono text-[9px] uppercase text-muted-foreground/60">off</span>
        ),
    },
    {
      key: "name",
      header: "name",
      cell: (m) => (
        <span className="flex flex-col">
          <span className="text-foreground">{m.name}</span>
          <span className="text-[10px] text-muted-foreground">{m.provider_model_id}</span>
        </span>
      ),
    },
    { key: "type", header: "type", cell: (m) => <Badge variant="muted">{m.type}</Badge> },
    ...(showScope
      ? [
          {
            key: "scope",
            header: "scope",
            cell: (m: Model) =>
              m.team_id === null ? (
                <Badge variant="accent">global</Badge>
              ) : (
                <Badge variant="muted">team</Badge>
              ),
          } satisfies Column<Model>,
        ]
      : []),
    {
      key: "provider",
      header: "provider",
      cell: (m) => (
        <span className="text-xs text-muted-foreground">
          {PROVIDER_LABELS[m.provider] ?? m.provider}
        </span>
      ),
    },
    {
      key: "credential",
      header: "credential",
      cell: (m) => (
        <span className="text-xs text-muted-foreground">
          {credentialNames.get(m.credential_id) ?? `${m.credential_id.slice(0, 8)}…`}
        </span>
      ),
    },
    {
      key: "cost",
      header: "cost (in/out)",
      numeric: true,
      cell: (m) => (
        <span className="tabular text-xs text-muted-foreground">
          {formatCost(m.input_cost_per_token)} / {formatCost(m.output_cost_per_token)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      className: "w-12",
      numeric: true,
      cell: (m) => (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              aria-label={`Actions for ${m.name}`}
              disabled={toggle.isPending}
            >
              <MoreHorizontal className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => onEdit(m)}>edit</DropdownMenuItem>
            {onExtend && m.team_id !== null ? (
              <DropdownMenuItem onSelect={() => onExtend(m)}>extend…</DropdownMenuItem>
            ) : null}
            <DropdownMenuItem onSelect={() => toggle.mutate(m)}>
              {m.enabled ? "disable" : "enable"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem className="text-destructive" onSelect={() => onDelete(m)}>
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
      rowKey={(m) => m.id}
      isLoading={isLoading}
      error={error}
      emptyTitle="no models"
      emptyDescription="Add a model with the button above."
    />
  );
}
