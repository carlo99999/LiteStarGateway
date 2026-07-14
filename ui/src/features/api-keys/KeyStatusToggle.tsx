import { cn } from "@/lib/utils";

export type KeyStatus = "active" | "revoked";

interface KeyStatusToggleProps {
  value: KeyStatus;
  onChange: (value: KeyStatus) => void;
  activeCount: number;
  revokedCount: number;
}

const OPTIONS: { value: KeyStatus; label: string }[] = [
  { value: "active", label: "active" },
  { value: "revoked", label: "revoked" },
];

/** Segmented control switching the keys table between active and revoked. */
export function KeyStatusToggle({
  value,
  onChange,
  activeCount,
  revokedCount,
}: KeyStatusToggleProps) {
  const counts: Record<KeyStatus, number> = { active: activeCount, revoked: revokedCount };
  return (
    <div className="inline-flex items-center gap-1 rounded-md border border-border bg-card/60 p-0.5">
      {OPTIONS.map((opt) => {
        const selected = value === opt.value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            aria-pressed={selected}
            className={cn(
              "rounded px-3 py-1 font-mono text-xs transition-colors",
              selected
                ? "bg-primary/15 text-primary"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {opt.label}
            <span className="ml-1.5 text-muted-foreground/70">{counts[opt.value]}</span>
          </button>
        );
      })}
    </div>
  );
}
