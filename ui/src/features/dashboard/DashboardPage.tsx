import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { PageHeader } from "@/components/common/PageHeader";
import { StatusDot } from "@/components/common/StatusDot";
import { Badge } from "@/components/ui/badge";
import { listAuditPage } from "@/features/audit/api";
import { useAuth } from "@/features/auth/use-auth";
import {
  listMyTeams,
  platformSavings,
  teamSavings,
} from "@/features/dashboard/api";
import { getOrganizationSpend, listAllOrganizations } from "@/features/organizations/api";
import { listAllTeams } from "@/features/teams/api";
import { listAllUsers } from "@/features/users/api";
import { api } from "@/lib/api/client";

const SPEND_DAYS = 30;
const TOP_TEAMS = 6;
const RECENT_EVENTS = 8;

function formatUsd(cost: number): string {
  return `$${cost.toFixed(2)}`;
}

function formatWhen(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

async function gatewayReady(): Promise<boolean> {
  const { error } = await api.GET("/health/ready");
  return !error;
}

function StatCard({
  label,
  value,
  to,
}: {
  label: string;
  value: string | number;
  to?: string;
}) {
  const body = (
    <div className="rounded-lg border border-border bg-card p-4 transition-colors hover:border-primary/40">
      <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // {label}
      </p>
      <p className="tabular mt-2 text-2xl text-foreground">{value}</p>
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

function GatewayCard() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: gatewayReady,
    refetchInterval: 30_000,
    retry: false,
  });
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // gateway
      </p>
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

/** Platform-admin landing: counts, gateway readiness, smart-routing savings,
 * 30-day spend with the top teams, and the latest audit activity. */
function AdminDashboard() {
  const orgs = useQuery({
    queryKey: ["organizations", "all"],
    queryFn: ({ signal }) => listAllOrganizations(signal),
    retry: false,
  });
  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    retry: false,
  });
  const users = useQuery({
    queryKey: ["users", "all"],
    queryFn: ({ signal }) => listAllUsers(signal),
    retry: false,
  });
  const savings = useQuery({
    queryKey: ["routing", "savings"],
    queryFn: platformSavings,
    retry: false,
  });
  const audit = useQuery({
    queryKey: ["audit", "page", 0],
    queryFn: () => listAuditPage(0),
  });

  // Per-org spend rollups (orgs are the top-level tenancy — a small set).
  const spendQueries = useQueries({
    queries: (orgs.data ?? []).map((org) => ({
      queryKey: ["organizations", org.id, "spend", SPEND_DAYS],
      queryFn: () => getOrganizationSpend(org.id, SPEND_DAYS),
      retry: false,
    })),
  });
  const spendLoaded = spendQueries.length > 0 && spendQueries.every((q) => q.data);
  const totalSpend = spendQueries.reduce((sum, q) => sum + (q.data?.total_cost ?? 0), 0);
  const teamSpend = spendQueries
    .flatMap((q) => q.data?.teams ?? [])
    .sort((a, b) => b.cost - a.cost)
    .slice(0, TOP_TEAMS);
  const maxTeamCost = teamSpend[0]?.cost ?? 0;

  const events = (audit.data?.items ?? []).slice(0, RECENT_EVENTS);

  return (
    <>
      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <StatCard label="organizations" value={orgs.data?.length ?? "—"} to="/organizations" />
        <StatCard label="teams" value={teams.data?.length ?? "—"} to="/teams" />
        <StatCard label="users" value={users.data?.length ?? "—"} to="/users" />
        <StatCard
          label="routing saved"
          value={savings.data ? formatUsd(savings.data.estimated_savings) : "—"}
          to="/routing"
        />
        <GatewayCard />
      </div>

      <div className="mb-6 grid gap-3 lg:grid-cols-2">
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex items-baseline justify-between">
            <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              // spend · last {SPEND_DAYS} days
            </p>
            <p className="tabular text-xl text-foreground">
              {spendLoaded || spendQueries.length === 0 ? formatUsd(totalSpend) : "—"}
            </p>
          </div>
          {teamSpend.length > 0 ? (
            <div className="space-y-2">
              {teamSpend.map((team) => (
                <div key={team.team_id} className="flex items-center gap-2 font-mono text-xs">
                  <Link
                    to="/teams/$teamId"
                    params={{ teamId: team.team_id }}
                    className="w-40 truncate text-foreground hover:text-primary hover:underline"
                  >
                    {team.name}
                  </Link>
                  <span className="h-2 flex-1 overflow-hidden rounded bg-secondary">
                    <span
                      className="block h-full rounded bg-primary/60"
                      style={{
                        width:
                          maxTeamCost > 0 ? `${Math.round((team.cost / maxTeamCost) * 100)}%` : 0,
                      }}
                    />
                  </span>
                  <span className="tabular w-20 text-right text-muted-foreground">
                    {formatUsd(team.cost)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="font-mono text-xs text-muted-foreground">
              {orgs.isLoading || (!spendLoaded && spendQueries.length > 0)
                ? "loading…"
                : "no spend recorded yet."}
            </p>
          )}
          {savings.data && savings.data.decisions_counted > 0 ? (
            <p className="mt-3 font-mono text-xs text-muted-foreground">
              smart routing saved{" "}
              <span className="text-primary">{formatUsd(savings.data.estimated_savings)}</span> vs
              the priciest capable model ({savings.data.decisions_counted} decisions, all time)
            </p>
          ) : null}
        </div>

        <div className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex items-baseline justify-between">
            <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              // recent activity
            </p>
            <Link to="/audit" className="font-mono text-xs text-primary hover:underline">
              audit log →
            </Link>
          </div>
          {events.length > 0 ? (
            <div className="space-y-2">
              {events.map((event) => (
                <div key={event.id} className="flex items-center gap-2 font-mono text-xs">
                  <span className="w-36 shrink-0 text-muted-foreground">
                    {formatWhen(event.created_at)}
                  </span>
                  <Badge variant="muted">{event.action}</Badge>
                  <span className="truncate text-muted-foreground">
                    {event.actor_email ?? event.actor_type ?? "—"}
                    {event.detail ? ` · ${event.detail}` : ""}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="font-mono text-xs text-muted-foreground">
              {audit.isLoading ? "loading…" : "no audit events yet."}
            </p>
          )}
        </div>
      </div>
    </>
  );
}

/** Non-admin landing: the caller's own teams, with smart-routing savings where
 * their role grants usage:read (team admin / billing-viewer — a plain member's
 * 403 renders as "not visible", per the deliberate role design). */
function MemberDashboard() {
  const myTeams = useQuery({ queryKey: ["me", "teams"], queryFn: listMyTeams });
  const savingsQueries = useQueries({
    queries: (myTeams.data ?? []).map((team) => ({
      queryKey: ["teams", team.team_id, "savings"],
      queryFn: () => teamSavings(team.team_id),
      retry: false,
    })),
  });

  return (
    <>
      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="your teams" value={myTeams.data?.length ?? "—"} />
        <GatewayCard />
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <p className="mb-3 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          // your teams
        </p>
        {myTeams.isLoading ? (
          <p className="font-mono text-xs text-muted-foreground">loading…</p>
        ) : (myTeams.data ?? []).length === 0 ? (
          <p className="font-mono text-xs text-muted-foreground">
            you are not a member of any team yet.
          </p>
        ) : (
          <div className="space-y-2">
            {(myTeams.data ?? []).map((team, index) => {
              const savings = savingsQueries[index];
              return (
                <div key={team.team_id} className="flex items-center gap-3 font-mono text-xs">
                  <span className="w-48 truncate text-foreground">{team.name}</span>
                  <Badge variant="muted">{team.role}</Badge>
                  <span className="ml-auto tabular text-muted-foreground">
                    {savings?.data
                      ? `routing saved ${formatUsd(savings.data.estimated_savings)}`
                      : savings?.isError
                        ? "savings not visible for your role"
                        : "…"}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </>
  );
}

/** Landing overview. Platform admins get the whole platform; everyone else
 * sees only the teams they belong to. */
export function DashboardPage() {
  const { user } = useAuth();
  return (
    <>
      <PageHeader
        command="status"
        title="Dashboard"
        description={
          user?.is_admin
            ? "Platform at a glance — tenancy, gateway readiness, spend, routing savings and activity."
            : "Your teams at a glance."
        }
      />
      {user?.is_admin ? <AdminDashboard /> : <MemberDashboard />}
    </>
  );
}
