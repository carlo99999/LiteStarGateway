import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { Button } from "@/components/ui/button";
import { CreateCredentialDialog } from "@/features/credentials/CreateCredentialDialog";
import { CredentialsTable } from "@/features/credentials/CredentialsTable";
import { DeleteCredentialDialog } from "@/features/credentials/DeleteCredentialDialog";
import { listCredentialsPage, type Credential } from "@/features/credentials/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";
import { toError } from "@/lib/toError";

/** Provider credentials: list, add, and delete. Secret values are write-only —
 * entered on creation, encrypted at rest, and never returned. */
export function CredentialsPage() {
  const [offset, setOffset] = useState(0);
  const credentials = useQuery({
    queryKey: ["credentials", "page", offset],
    queryFn: () => listCredentialsPage(offset),
  });

  useEffect(() => {
    if (!credentials.isFetching && credentials.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [credentials.data, credentials.isFetching, offset]);

  const [createOpen, setCreateOpen] = useState(false);
  const [deleting, setDeleting] = useState<Credential | null>(null);

  return (
    <>
      <PageHeader
        command="credentials list"
        title="Credentials"
        description="Provider secrets used by models. Values are encrypted at rest and never shown after creation."
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            Add credential
          </Button>
        }
      />
      <CredentialsTable
        rows={credentials.data?.items}
        isLoading={credentials.isLoading}
        error={toError(credentials.error)}
        onDelete={setDeleting}
      />
      {credentials.data ? (
        <PaginationControls
          offset={credentials.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={credentials.data.items.length}
          hasNext={credentials.data.hasNext}
          isFetching={credentials.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}

      <CreateCredentialDialog open={createOpen} onOpenChange={setCreateOpen} />
      <DeleteCredentialDialog
        credential={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      />
    </>
  );
}
