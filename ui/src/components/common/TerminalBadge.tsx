import { Badge, type BadgeProps } from "@/components/ui/badge";

/**
 * A command-line flavored badge. Renders `> label` (or a custom prefix) in the
 * bracketed mono style — the terminal-tech voice for inline tags.
 */
interface TerminalBadgeProps extends Omit<BadgeProps, "bracketed"> {
  prefix?: string;
}

export function TerminalBadge({ prefix = ">", children, ...props }: TerminalBadgeProps) {
  return (
    <Badge bracketed={false} {...props}>
      <span className="text-primary">{prefix}&nbsp;</span>
      {children}
    </Badge>
  );
}
