import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { listAllServicePrincipals } from "@/features/api-keys/api";

/** Resolve a team's service principals to an id → name map, so key tables can
 * label service-principal keys by name. Returns an empty map until loaded (or
 * when no team is selected). Shares the `["team-service-principals", id, "all"]`
 * cache so it's fetched once per team. */
export function useServicePrincipalNames(teamId: string | undefined): Map<string, string> {
  const query = useQuery({
    queryKey: ["team-service-principals", teamId, "all"],
    queryFn: ({ signal }) => listAllServicePrincipals(teamId as string, signal),
    enabled: Boolean(teamId),
  });

  return useMemo(() => {
    const map = new Map<string, string>();
    for (const sp of query.data ?? []) map.set(sp.id, sp.name);
    return map;
  }, [query.data]);
}
