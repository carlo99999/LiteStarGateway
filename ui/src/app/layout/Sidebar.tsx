import { Link } from "@tanstack/react-router";
import { NAV_GROUPS } from "@/app/layout/nav";
import { useAuth } from "@/features/auth/use-auth";
import { canAccessConsoleSurface, type TeamRole } from "@/features/teams/access";
import { useAccessibleTeams } from "@/features/teams/useAccessibleTeams";

export function Sidebar() {
  const { user } = useAuth();
  const teams = useAccessibleTeams();
  const teamRoles = (teams.data ?? [])
    .map((team) => team.role)
    .filter((role): role is TeamRole => role !== null);
  const access = {
    isPlatformAdmin: Boolean(user?.is_admin),
    isAuditor: Boolean(user?.is_auditor),
    teamRoles,
  };
  const visibleGroups = NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => canAccessConsoleSurface(item.surface, access)),
  })).filter((group) => group.items.length > 0);

  return (
    <aside className="hidden w-56 shrink-0 flex-col border-r border-border bg-card/60 md:flex">
      <nav className="flex-1 space-y-4 overflow-y-auto p-3">
        {visibleGroups.map((group) => (
          <div key={group.label} className="space-y-0.5">
            <p className="px-2 pb-1 pt-1 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              // {group.label}
            </p>
            {group.items.map((item) => {
              const Icon = item.icon;
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  className="group flex items-center justify-between rounded-md px-2 py-1.5 font-mono text-sm text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground [&.active]:bg-primary/10 [&.active]:text-primary"
                  // `/` (dashboard) must match exactly, or it stays active on
                  // every route; the rest match on prefix.
                  activeOptions={{ exact: item.to === "/" }}
                >
                  <span className="flex items-center gap-2">
                    <Icon className="h-4 w-4" />
                    <span>
                      <span className="text-primary/70 group-hover:text-primary">/</span>
                      {item.label}
                    </span>
                  </span>
                  {!item.ready ? (
                    <span className="font-mono text-[9px] uppercase text-muted-foreground/60">
                      soon
                    </span>
                  ) : null}
                </Link>
              );
            })}
          </div>
        ))}
      </nav>
      <div className="border-t border-border p-3 font-mono text-[10px] text-muted-foreground">
        <span className="text-primary">●</span> gateway v1.1.0
      </div>
    </aside>
  );
}
