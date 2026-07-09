import type { ReactNode } from "react";
import { EmptyState } from "@/components/common/EmptyState";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export interface Column<T> {
  /** Stable key, also used for the React list key. */
  key: string;
  header: ReactNode;
  /** Cell renderer. */
  cell: (row: T) => ReactNode;
  /** Right-align + mono tabular numbers for numeric columns. */
  numeric?: boolean;
  className?: string;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => string;
  isLoading?: boolean;
  error?: Error | null;
  emptyTitle?: string;
  emptyDescription?: string;
}

/** A dense, mono-tabular table with built-in loading / empty / error states. */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  isLoading = false,
  error = null,
  emptyTitle = "no rows",
  emptyDescription,
}: DataTableProps<T>) {
  if (error) {
    return (
      <EmptyState
        title="request failed"
        description={error.message}
        className="border-destructive/40"
      />
    );
  }

  if (isLoading) {
    return (
      <div className="space-y-2 rounded-lg border border-border bg-card p-4">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-8 w-full" />
        ))}
      </div>
    );
  }

  if (!rows || rows.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} />;
  }

  return (
    <div className="rounded-lg border border-border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            {columns.map((col) => (
              <TableHead
                key={col.key}
                className={cn(col.numeric && "text-right", col.className)}
              >
                {col.header}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={rowKey(row)}>
              {columns.map((col) => (
                <TableCell
                  key={col.key}
                  className={cn(col.numeric && "text-right tabular", col.className)}
                >
                  {col.cell(row)}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
