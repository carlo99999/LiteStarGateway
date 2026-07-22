import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/features/auth/use-auth";
import {
  canReadModels,
  fromMembership,
  fromPlatformTeam,
  type AccessibleTeam,
} from "@/features/teams/access";
import { listAllTeams, listMyTeamMemberships } from "@/features/teams/api";

async function loadAccessibleTeams(
  isPlatformAdmin: boolean,
  signal: AbortSignal,
): Promise<AccessibleTeam[]> {
  const teams = isPlatformAdmin
    ? (await listAllTeams(signal)).map(fromPlatformTeam)
    : (await listMyTeamMemberships(signal)).map(fromMembership);
  return teams.filter((team) => canReadModels(team.role));
}

/** Teams whose model/routing catalog the current user may read. */
export function useAccessibleTeams() {
  const { user } = useAuth();
  const isPlatformAdmin = Boolean(user?.is_admin);
  const query = useQuery({
    queryKey: [isPlatformAdmin ? "teams" : "me", "model-access"],
    queryFn: ({ signal }) => loadAccessibleTeams(isPlatformAdmin, signal),
    enabled: user !== null,
    retry: false,
  });
  return { ...query, isPlatformAdmin };
}
