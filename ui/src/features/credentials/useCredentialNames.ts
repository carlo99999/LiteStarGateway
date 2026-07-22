import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { listAllCredentials } from "@/features/credentials/api";
import { listModelCredentials } from "@/features/models/api";

/** Resolve credentials to an id → name map, so tables can label a model's
 * credential by name instead of an opaque id. Empty until loaded. */
export function useCredentialNames(enabled = true, teamId?: string): Map<string, string> {
  const query = useQuery({
    queryKey: teamId ? ["teams", teamId, "model-credentials"] : ["credentials", "all"],
    queryFn: ({ signal }) =>
      teamId ? listModelCredentials(teamId, signal) : listAllCredentials(signal),
    enabled: enabled || Boolean(teamId),
  });

  return useMemo(() => {
    const map = new Map<string, string>();
    for (const credential of query.data ?? []) map.set(credential.id, credential.name);
    return map;
  }, [query.data]);
}
