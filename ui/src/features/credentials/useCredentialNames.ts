import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { listAllCredentials } from "@/features/credentials/api";

/** Resolve credentials to an id → name map, so tables can label a model's
 * credential by name instead of an opaque id. Empty until loaded. */
export function useCredentialNames(): Map<string, string> {
  const query = useQuery({
    queryKey: ["credentials", "all"],
    queryFn: ({ signal }) => listAllCredentials(signal),
  });

  return useMemo(() => {
    const map = new Map<string, string>();
    for (const credential of query.data ?? []) map.set(credential.id, credential.name);
    return map;
  }, [query.data]);
}
