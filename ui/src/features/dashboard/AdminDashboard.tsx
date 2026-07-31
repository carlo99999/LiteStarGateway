import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Badge } from "@/components/ui/badge";
import { listAuditPage } from "@/features/audit/api";
import { BarRow, GatewayCard, PanelLabel, StatCard } from "@/features/dashboard/cards";
import { platformSavings } from "@/features/dashboard/api";
import {
  barPercent,
  formatCount,
  formatPercent,
  formatUsd,
  formatWhen,
  orgTotals,
  topTeams,
  totalCost,
  type OrgSpend,
} from "@/features/dashboard/rollup";
import { listAllGlobalModels, platformCacheSavings } from "@/features/models/api";
import { getOrganizationSpend, listAllOrganizations } from "@/features/organizations/api";
import { listGlobalRouters } from "@/features/routing/api";
import { listAllTeams } from "@/features/teams/api";
import { listAllUsers } from "@/features/users/api";
import { toError } from "@/lib/toError";

const SPEND_DAYS = 30;
const TOP_TEAMS = 6;
const TOP_ORGS = 6;
const RECENT_EVENTS = 8;

function CacheStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular mt-1 text-base text-foreground">{value}</dd>
    </div>
  );
}

/** Platform-admin landing: tenancy and catalog counts, gateway readiness,
 * routing and cache savings, 30-day spend broken down by organization and by
 * team, and the latest audit activity. */
export function AdminDashboard() {
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
  const models = useQuery({
    queryKey: ["platform", "models"],
    queryFn: ({ signal }) => listAllGlobalModels(signal),
    retry: false,
  });
  const routers = useQuery({
    queryKey: ["platform", "routers"],
    queryFn: listGlobalRouters,
    retry: false,
  });
  const savings = useQuery({
    queryKey: ["routing", "savings"],
    queryFn: platformSavings,
    retry: false,
  });
  const cache = useQuery({
    queryKey: ["cache", "savings"],
    queryFn: platformCacheSavings,
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
  // Any failure in the org list or a per-org rollup means the total is a
  // partial sum, not a real zero — never present it as "$0.00 spend".
  const spendError = toError(orgs.error) ?? toError(spendQueries.find((q) => q.error)?.error);
  const spendLoaded = spendQueries.length > 0 && spendQueries.every((q) => q.data);
  const spends = spendQueries.flatMap((q) => (q.data ? [q.data as OrgSpend] : []));
  const totalSpend = totalCost(spends);
  const teamSpend = topTeams(spends, TOP_TEAMS);
  const orgSpend = orgTotals(orgs.data ?? [], spends, TOP_ORGS);
  const maxTeamCost = teamSpend[0]?.cost ?? 0;
  const maxOrgCost = orgSpend[0]?.cost ?? 0;
  // A true zero only when the org list loaded and reported no organizations.
  const spendIsRealZero = !spendError && !orgs.isLoading && (orgs.data?.length ?? 0) === 0;
  const spendPending = orgs.isLoading || (!spendLoaded && spendQueries.length > 0);

  const enabledModels = (models.data ?? []).filter((model) => model.enabled).length;
  const enabledRouters = (routers.data ?? []).filter((router) => router.enabled).length;
  const events = (audit.data?.items ?? []).slice(0, RECENT_EVENTS);

  return (
    <>
      <div className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="organizations" value={orgs.data?.length ?? "—"} to="/organizations" />
        <StatCard label="teams" value={teams.data?.length ?? "—"} to="/teams" />
        <StatCard label="users" value={users.data?.length ?? "—"} to="/users" />
        <GatewayCard />
      </div>

      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="global models"
          value={models.data?.length ?? "—"}
          hint={models.data ? `${enabledModels} enabled` : undefined}
          to="/models"
        />
        <StatCard
          label="global routers"
          value={routers.data?.length ?? "—"}
          hint={routers.data ? `${enabledRouters} enabled` : undefined}
          to="/routing"
        />
        <StatCard
          label="routing saved"
          value={savings.data ? formatUsd(savings.data.estimated_savings) : "—"}
          hint={
            savings.data ? `${formatCount(savings.data.decisions_counted)} decisions` : undefined
          }
          to="/routing"
        />
        <StatCard
          label="cache hit rate"
          value={cache.data ? formatPercent(cache.data.cache_hit_rate) : "—"}
          hint={cache.data ? `${formatUsd(cache.data.estimated_cost_saved)} saved` : undefined}
        />
      </div>

      <div className="mb-3 grid gap-3 lg:grid-cols-2">
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex items-baseline justify-between">
            <PanelLabel>spend · last {SPEND_DAYS} days</PanelLabel>
            <p className="tabular text-xl text-foreground">
              {spendError ? "—" : spendLoaded || spendIsRealZero ? formatUsd(totalSpend) : "—"}
            </p>
          </div>
          {spendError ? (
            <p className="font-mono text-xs text-destructive">
              ! couldn&apos;t load spend — {spendError.message}
            </p>
          ) : teamSpend.length > 0 ? (
            <div className="space-y-2">
              {teamSpend.map((team) => (
                <BarRow
                  key={team.team_id}
                  label={
                    <Link
                      to="/teams/$teamId"
                      params={{ teamId: team.team_id }}
                      className="hover:text-primary hover:underline"
                    >
                      {team.name}
                    </Link>
                  }
                  percent={barPercent(team.cost, maxTeamCost)}
                  value={formatUsd(team.cost)}
                />
              ))}
            </div>
          ) : (
            <p className="font-mono text-xs text-muted-foreground">
              {spendPending ? "loading…" : "no spend recorded yet."}
            </p>
          )}
          {!spendError && savings.data && savings.data.decisions_counted > 0 ? (
            <p className="mt-3 font-mono text-xs text-muted-foreground">
              smart routing saved{" "}
              <span className="text-primary">{formatUsd(savings.data.estimated_savings)}</span> vs
              the priciest capable model ({savings.data.decisions_counted} decisions, all time)
            </p>
          ) : null}
        </div>

        <div className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex items-baseline justify-between">
            <PanelLabel>spend by organization</PanelLabel>
            <Link to="/organizations" className="font-mono text-xs text-primary hover:underline">
              organizations →
            </Link>
          </div>
          {spendError ? (
            <p className="font-mono text-xs text-destructive">
              ! couldn&apos;t load spend — {spendError.message}
            </p>
          ) : orgSpend.length > 0 ? (
            <div className="space-y-2">
              {orgSpend.map((org) => (
                <BarRow
                  key={org.organization_id}
                  label={
                    <Link
                      to="/organizations/$organizationId"
                      params={{ organizationId: org.organization_id }}
                      className="hover:text-primary hover:underline"
                    >
                      {org.name}
                    </Link>
                  }
                  percent={barPercent(org.cost, maxOrgCost)}
                  value={formatUsd(org.cost)}
                />
              ))}
            </div>
          ) : (
            <p className="font-mono text-xs text-muted-foreground">
              {spendPending ? "loading…" : "no organizations yet."}
            </p>
          )}
        </div>
      </div>

      <div className="mb-6 grid gap-3 lg:grid-cols-2">
        <div className="rounded-lg border border-border bg-card p-4">
          <PanelLabel>response cache</PanelLabel>
          {cache.isError ? (
            <p className="mt-3 font-mono text-xs text-destructive">
              ! couldn&apos;t load cache stats — {toError(cache.error)?.message}
            </p>
          ) : cache.data ? (
            <dl className="mt-3 grid grid-cols-2 gap-3 font-mono text-xs">
              <CacheStat label="hit rate" value={formatPercent(cache.data.cache_hit_rate)} />
              <CacheStat label="cost saved" value={formatUsd(cache.data.estimated_cost_saved)} />
              <CacheStat label="hits" value={formatCount(cache.data.cache_hits)} />
              <CacheStat label="requests" value={formatCount(cache.data.total_requests)} />
              {cache.data.cache_hits_without_price > 0 ? (
                <div className="col-span-2 text-muted-foreground">
                  {formatCount(cache.data.cache_hits_without_price)} hits had no price on record —
                  the saving above is a lower bound.
                </div>
              ) : null}
            </dl>
          ) : (
            <p className="mt-3 font-mono text-xs text-muted-foreground">loading…</p>
          )}
        </div>

        <div className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex items-baseline justify-between">
            <PanelLabel>recent activity</PanelLabel>
            <Link to="/audit" className="font-mono text-xs text-primary hover:underline">
              audit log →
            </Link>
          </div>
          {audit.isError ? (
            <p className="font-mono text-xs text-destructive">
              ! couldn&apos;t load activity — {toError(audit.error)?.message}
            </p>
          ) : events.length > 0 ? (
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
