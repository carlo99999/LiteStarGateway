import { Trash2 } from "lucide-react";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { Credential } from "@/features/credentials/api";
import { PROVIDER_LABELS } from "@/features/credentials/providerFields";

interface CredentialsTableProps {
  rows: Credential[] | undefined;
  isLoading: boolean;
  error: Error | null;
  onDelete: (credential: Credential) => void;
}

function formatWhen(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

export function CredentialsTable({ rows, isLoading, error, onDelete }: CredentialsTableProps) {
  const columns: Column<Credential>[] = [
    { key: "name", header: "name", cell: (c) => <span className="text-foreground">{c.name}</span> },
    {
      key: "provider",
      header: "provider",
      cell: (c) => <Badge variant="muted">{PROVIDER_LABELS[c.provider] ?? c.provider}</Badge>,
    },
    {
      key: "created",
      header: "created",
      cell: (c) => <span className="text-xs text-muted-foreground">{formatWhen(c.created_at)}</span>,
    },
    {
      key: "actions",
      header: "",
      className: "w-12",
      numeric: true,
      cell: (c) => (
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 text-muted-foreground hover:text-destructive"
          aria-label={`Delete ${c.name}`}
          onClick={() => onDelete(c)}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      ),
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(c) => c.id}
      isLoading={isLoading}
      error={error}
      emptyTitle="no credentials"
      emptyDescription="Add a provider credential with the button above."
    />
  );
}
