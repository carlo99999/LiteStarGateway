import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { listAuditPage, type AuditEvent } from "@/features/audit/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";

function formatWhen(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

const COLUMNS: Column<AuditEvent>[] = [
  {
    key: "when",
    header: "when",
    className: "w-44",
    cell: (e) => <span className="text-xs text-muted-foreground">{formatWhen(e.created_at)}</span>,
  },
  {
    key: "action",
    header: "action",
    cell: (e) => <Badge variant="muted">{e.action}</Badge>,
  },
  {
    key: "actor",
    header: "actor",
    cell: (e) => (
      <span className="flex flex-col">
        <span className="text-xs text-foreground">{e.actor_email ?? e.actor_id ?? "—"}</span>
        {e.actor_type ? (
          <span className="text-[10px] text-muted-foreground">{e.actor_type}</span>
        ) : null}
      </span>
    ),
  },
  {
    key: "target",
    header: "target",
    cell: (e) =>
      e.target_type ? (
        <span className="text-xs text-muted-foreground">
          {e.target_type}
          {e.target_id ? ` · ${e.target_id.slice(0, 8)}…` : ""}
        </span>
      ) : (
        <span className="text-xs text-muted-foreground">—</span>
      ),
  },
  {
    key: "detail",
    header: "detail",
    cell: (e) => (
      <span className="block max-w-72 truncate text-xs text-muted-foreground" title={e.detail ?? ""}>
        {e.detail ?? "—"}
      </span>
    ),
  },
  {
    key: "ip",
    header: "ip",
    numeric: true,
    cell: (e) => <span className="tabular text-xs text-muted-foreground">{e.ip ?? "—"}</span>,
  },
];

/** The platform audit trail: privileged actions, most recent first. Read-only. */
export function AuditPage() {
  const [offset, setOffset] = useState(0);
  const events = useQuery({
    queryKey: ["audit", "page", offset],
    queryFn: () => listAuditPage(offset),
  });

  useEffect(() => {
    if (!events.isFetching && events.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [events.data, events.isFetching, offset]);

  return (
    <>
      <PageHeader
        command="audit log"
        title="Audit Log"
        description="Every privileged action — who did what, to what, from where. Most recent first."
      />
      <DataTable
        columns={COLUMNS}
        rows={events.data?.items}
        rowKey={(e) => e.id}
        isLoading={events.isLoading}
        error={events.error as Error | null}
        emptyTitle="no audit events"
        emptyDescription="Privileged actions are recorded here as they happen."
      />
      {events.data ? (
        <PaginationControls
          offset={events.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={events.data.items.length}
          hasNext={events.data.hasNext}
          isFetching={events.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}
    </>
  );
}
