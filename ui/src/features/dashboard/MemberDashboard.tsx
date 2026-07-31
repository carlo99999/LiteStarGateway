import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { NAV_GROUPS } from "@/app/layout/nav";
import { Badge } from "@/components/ui/badge";
import { useAuth } from "@/features/auth/use-auth";
import { GatewayCard, PanelLabel, StatCard } from "@/features/dashboard/cards";
import { listMyTeams, teamSavings } from "@/features/dashboard/api";
import { formatPercent, formatUsd } from "@/features/dashboard/rollup";
import { teamCacheSavings } from "@/features/models/api";
import { canAccessConsoleSurface, TEAM_ROLES, type TeamRole } from "@/features/teams/access";

/** Nav destinations worth surfacing on the landing page — the dashboard itself
 * and the unimplemented placeholders are not. */
const SHORTCUTS = NAV_GROUPS.flatMap((group) => group.items).filter(
  (item) => item.ready && item.surface !== "dashboard",
);

function isTeamRole(role: string): role is TeamRole {
  return TEAM_ROLES.some((candidate) => candidate === role);
}

/** Non-admin landing: the caller's own teams with their routing and cache
 * savings where their role grants usage:read (team admin / billing-viewer — a
 * plain member's 403 renders as "not visible", per the deliberate role design),
 * plus shortcuts to the surfaces their roles can reach. */
export function MemberDashboard() {
  const { user } = useAuth();
  const myTeams = useQuery({ queryKey: ["me", "teams"], queryFn: listMyTeams });
  const teams = myTeams.data ?? [];

  const savingsQueries = useQueries({
    queries: teams.map((team) => ({
      queryKey: ["teams", team.team_id, "savings"],
      queryFn: () => teamSavings(team.team_id),
      retry: false,
    })),
  });
  const cacheQueries = useQueries({
    queries: teams.map((team) => ({
      queryKey: ["teams", team.team_id, "cache", "savings"],
      queryFn: () => teamCacheSavings(team.team_id),
      retry: false,
    })),
  });

  // Sums cover only the teams whose usage the caller may read; a role without
  // usage:read contributes nothing rather than a misleading zero.
  const visibleSavings = savingsQueries.flatMap((q) => (q.data ? [q.data] : []));
  const visibleCache = cacheQueries.flatMap((q) => (q.data ? [q.data] : []));
  const savedByRouting = visibleSavings.reduce((sum, s) => sum + s.estimated_savings, 0);
  const savedByCache = visibleCache.reduce((sum, c) => sum + c.estimated_cost_saved, 0);

  const teamRoles = teams.map((team) => team.role).filter((role): role is TeamRole => isTeamRole(role));
  const access = {
    isPlatformAdmin: false,
    isAuditor: Boolean(user?.is_auditor),
    teamRoles,
  };
  const shortcuts = SHORTCUTS.filter((item) => canAccessConsoleSurface(item.surface, access));

  return (
    <>
      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="your teams" value={myTeams.data?.length ?? "—"} />
        <StatCard
          label="routing saved"
          value={visibleSavings.length > 0 ? formatUsd(savedByRouting) : "—"}
          hint={
            visibleSavings.length > 0
              ? `across ${visibleSavings.length} team(s)`
              : "needs billing access"
          }
        />
        <StatCard
          label="cache saved"
          value={visibleCache.length > 0 ? formatUsd(savedByCache) : "—"}
          hint={
            visibleCache.length > 0 ? `across ${visibleCache.length} team(s)` : "needs billing access"
          }
        />
        <GatewayCard />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <div className="rounded-lg border border-border bg-card p-4">
          <PanelLabel>your teams</PanelLabel>
          <div className="mt-3">
            {myTeams.isLoading ? (
              <p className="font-mono text-xs text-muted-foreground">loading…</p>
            ) : teams.length === 0 ? (
              <p className="font-mono text-xs text-muted-foreground">
                you are not a member of any team yet.
              </p>
            ) : (
              <div className="space-y-3">
                {teams.map((team, index) => {
                  const savings = savingsQueries[index];
                  const cache = cacheQueries[index];
                  return (
                    <div key={team.team_id} className="space-y-1">
                      <div className="flex items-center gap-2 font-mono text-xs">
                        <span className="truncate text-foreground">{team.name}</span>
                        <Badge variant="muted">{team.role}</Badge>
                      </div>
                      <div className="flex flex-wrap gap-x-4 font-mono text-[11px] text-muted-foreground">
                        <span className="tabular">
                          {savings?.data
                            ? `routing saved ${formatUsd(savings.data.estimated_savings)}`
                            : savings?.isError
                              ? "savings not visible for your role"
                              : "…"}
                        </span>
                        {cache?.data ? (
                          <span className="tabular">
                            cache {formatPercent(cache.data.cache_hit_rate)} hit ·{" "}
                            {formatUsd(cache.data.estimated_cost_saved)} saved
                          </span>
                        ) : null}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        <div className="rounded-lg border border-border bg-card p-4">
          <PanelLabel>shortcuts</PanelLabel>
          {shortcuts.length === 0 ? (
            <p className="mt-3 font-mono text-xs text-muted-foreground">
              your roles do not open any other console surface yet.
            </p>
          ) : (
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {shortcuts.map((item) => {
                const Icon = item.icon;
                return (
                  <Link
                    key={item.to}
                    to={item.to}
                    className="flex items-center gap-2 rounded-md border border-border px-2 py-1.5 font-mono text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                  >
                    <Icon className="h-4 w-4" />
                    <span>
                      <span className="text-primary/70">/</span>
                      {item.label}
                    </span>
                  </Link>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
