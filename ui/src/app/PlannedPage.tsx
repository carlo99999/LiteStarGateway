import { PageHeader } from "@/components/common/PageHeader";
import { EmptyState } from "@/components/common/EmptyState";

interface PlannedPageProps {
  command: string;
  title: string;
}

/** Placeholder for feature areas scaffolded but not implemented in Phase 0. */
export function PlannedPage({ command, title }: PlannedPageProps) {
  return (
    <>
      <PageHeader command={command} title={title} />
      <EmptyState
        title="not yet implemented"
        description="This resource area is scaffolded. Read-only views land in Plan 03, Phase 1."
      />
    </>
  );
}
