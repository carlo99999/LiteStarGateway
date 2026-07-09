import { cn } from "@/lib/utils";

type Tone = "green" | "violet" | "amber" | "muted";

interface StatusDotProps {
  tone?: Tone;
  label?: string;
  /** Slow pulse — reserve for a "live" indicator. */
  pulse?: boolean;
  className?: string;
}

const TONE_CLASS: Record<Tone, string> = {
  green: "bg-primary",
  violet: "bg-accent",
  amber: "bg-warning",
  muted: "bg-muted-foreground",
};

/** A color-coded `● label` status indicator (green / violet / amber). */
export function StatusDot({ tone = "green", label, pulse = false, className }: StatusDotProps) {
  return (
    <span className={cn("inline-flex items-center gap-2 font-mono text-xs", className)}>
      <span
        className={cn("h-2 w-2 rounded-full", TONE_CLASS[tone], pulse && "animate-pulse-live")}
        aria-hidden
      />
      {label ? <span className="text-foreground">{label}</span> : null}
    </span>
  );
}
