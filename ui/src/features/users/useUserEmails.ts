import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { listAllUsers } from "@/features/users/api";

/** Resolve user ids to emails, so key tables can attribute personal keys to a
 * readable owner instead of an opaque id. `GET /users` is platform-admin only,
 * so this is gated on `enabled` and returns an empty map for everyone else
 * (the table then falls back to the truncated id). Shares the
 * `["users", "all"]` cache so the roster is fetched once. */
export function useUserEmails(enabled: boolean): Map<string, string> {
  const query = useQuery({
    queryKey: ["users", "all"],
    queryFn: ({ signal }) => listAllUsers(signal),
    enabled,
    retry: false,
  });

  return useMemo(() => {
    const map = new Map<string, string>();
    for (const user of query.data ?? []) map.set(user.id, user.email);
    return map;
  }, [query.data]);
}
