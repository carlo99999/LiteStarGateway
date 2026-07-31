import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { StatusDot } from "@/components/common/StatusDot";
import { api } from "@/lib/api/client";

async function gatewayReady(): Promise<boolean> {
  const { error } = await api.GET("/health/ready");
  return !error;
}

/** The `// label` header shared by every dashboard card and panel. */
export function PanelLabel({ children }: { children: ReactNode }) {
  return (
    <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
      // {children}
    </p>
  );
}

export function StatCard({
  label,
  value,
  hint,
  to,
}: {
  label: string;
  value: string | number;
  hint?: string;
  to?: string;
}) {
  const body = (
    <div className="h-full rounded-lg border border-border bg-card p-4 transition-colors hover:border-primary/40">
      <PanelLabel>{label}</PanelLabel>
      <p className="tabular mt-2 text-2xl text-foreground">{value}</p>
      {hint ? <p className="mt-1 font-mono text-[10px] text-muted-foreground">{hint}</p> : null}
    </div>
  );
  return to ? (
    <Link to={to} className="block h-full">
      {body}
    </Link>
  ) : (
    body
  );
}

export function GatewayCard() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: gatewayReady,
    refetchInterval: 30_000,
    retry: false,
  });
  return (
    <div className="h-full rounded-lg border border-border bg-card p-4">
      <PanelLabel>gateway</PanelLabel>
      <p className="mt-3">
        {health.isLoading ? (
          <span className="font-mono text-xs text-muted-foreground">checking…</span>
        ) : (
          <StatusDot
            tone={health.data ? "green" : "amber"}
            label={health.data ? "ready" : "not ready"}
            pulse={health.data === true}
          />
        )}
      </p>
    </div>
  );
}

/** A `name ▁▁▁▁ $cost` row — the shared shape of the spend breakdowns. */
export function BarRow({
  label,
  percent,
  value,
}: {
  label: ReactNode;
  percent: number;
  value: string;
}) {
  return (
    <div className="flex items-center gap-2 font-mono text-xs">
      <span className="w-40 truncate text-foreground">{label}</span>
      <span className="h-2 flex-1 overflow-hidden rounded bg-secondary">
        <span className="block h-full rounded bg-primary/60" style={{ width: `${percent}%` }} />
      </span>
      <span className="tabular w-20 text-right text-muted-foreground">{value}</span>
    </div>
  );
}
