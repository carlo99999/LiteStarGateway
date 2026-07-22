import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/features/auth/use-auth";
import {
  canReadModels,
  fromMembership,
  fromPlatformTeam,
  type AccessibleTeam,
  type TeamRole,
} from "@/features/teams/access";
import { listAllTeams, listMyTeamMemberships } from "@/features/teams/api";

async function loadAccessibleTeams(
  isPlatformAdmin: boolean,
  signal: AbortSignal,
): Promise<AccessibleTeam[]> {
  return isPlatformAdmin
    ? (await listAllTeams(signal)).map(fromPlatformTeam)
    : (await listMyTeamMemberships(signal)).map(fromMembership);
}

/** The caller's teams, filtered to the given capability (default: teams whose
 * model/routing catalog the caller may read). One shared query per session —
 * every caller sees the same self-scoped or platform-wide team list, and only
 * the client-side filter differs, so different pages can ask for different
 * capabilities (e.g. billing) without an extra network round trip. */
export function useAccessibleTeams(filter: (role: TeamRole | null) => boolean = canReadModels) {
  const { user } = useAuth();
  const isPlatformAdmin = Boolean(user?.is_admin);
  const query = useQuery({
    queryKey: [isPlatformAdmin ? "teams" : "me", "accessible-teams"],
    queryFn: ({ signal }) => loadAccessibleTeams(isPlatformAdmin, signal),
    enabled: user !== null,
    retry: false,
  });
  return { ...query, data: query.data?.filter((team) => filter(team.role)), isPlatformAdmin };
}
