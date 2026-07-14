import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { previousPageOffset } from "@/lib/api/pagination";

interface PaginationControlsProps {
  offset: number;
  pageSize: number;
  itemCount: number;
  hasNext: boolean;
  isFetching: boolean;
  onOffsetChange: (offset: number) => void;
}

/** Controls for offset endpoints that return bare arrays and no total count. */
export function PaginationControls({
  offset,
  pageSize,
  itemCount,
  hasNext,
  isFetching,
  onOffsetChange,
}: PaginationControlsProps) {
  const first = itemCount === 0 ? 0 : offset + 1;
  const last = offset + itemCount;

  return (
    <nav className="mt-3 flex items-center justify-end gap-3" aria-label="Table pagination">
      <span className="font-mono text-xs text-muted-foreground">
        {first}–{last}
        {hasNext ? " · more" : ""}
      </span>
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-label="Previous page"
        disabled={offset === 0 || isFetching}
        onClick={() => onOffsetChange(previousPageOffset(offset, pageSize))}
      >
        <ChevronLeft className="h-4 w-4" />
        Prev
      </Button>
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-label="Next page"
        disabled={!hasNext || isFetching}
        onClick={() => onOffsetChange(offset + pageSize)}
      >
        Next
        <ChevronRight className="h-4 w-4" />
      </Button>
    </nav>
  );
}
