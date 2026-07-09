import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";
import { cn } from "@/lib/utils";

// `[ bracketed ]` mono badges, thin border, color-coded by category.
const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-2 py-0.5 font-mono text-[11px] font-medium leading-tight tracking-tight",
  {
    variants: {
      variant: {
        default: "border-primary/30 bg-primary/10 text-primary",
        accent: "border-accent/30 bg-accent/10 text-accent",
        warning: "border-warning/30 bg-warning/10 text-warning",
        muted: "border-border bg-secondary text-muted-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {
  /** Wrap the content in `[ ... ]` brackets (terminal-tech signature). */
  bracketed?: boolean;
}

function Badge({ className, variant, bracketed = true, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props}>
      {bracketed ? <>[&nbsp;{children}&nbsp;]</> : children}
    </span>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export { Badge, badgeVariants };
