/** Narrow an unknown rejection (e.g. `useQuery().error`) to `Error | null`.
 *
 * Our query functions always reject with a real `Error`, but the query
 * libraries type `.error` as `unknown`. This narrows without an unchecked
 * `as Error` cast, and wraps a stray non-Error rejection instead of
 * mis-typing it. */
export function toError(value: unknown): Error | null {
  if (value == null) return null;
  if (value instanceof Error) return value;
  return new Error(typeof value === "string" ? value : "Unexpected error");
}
