import type { ReactNode } from "react";

interface PageHeaderProps {
  /** The command-line style path shown above the title, e.g. `organizations`. */
  command: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}

/** Page title block with a `$ cmd` terminal prompt line. */
export function PageHeader({ command, title, description, actions }: PageHeaderProps) {
  return (
    <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="space-y-1">
        <p className="font-mono text-xs text-muted-foreground">
          <span className="text-primary">$</span> gateway {command}
        </p>
        <h1 className="text-2xl text-foreground">{title}</h1>
        {description ? (
          <p className="max-w-2xl text-sm text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}
