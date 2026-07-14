/** Build a signup link whose bearer token is never part of the HTTP request. */
export function buildInviteSignupLink(origin: string, token: string): string {
  return `${origin}/ui/signup#token=${encodeURIComponent(token)}`;
}

/** Read an invite token exclusively from the client-only URL fragment. */
export function inviteTokenFromFragment(hash: string): string {
  const fragment = hash.startsWith("#") ? hash.slice(1) : hash;
  return new URLSearchParams(fragment).get("token") ?? "";
}

/** Address used with history.replaceState after the token is in memory.
 * The fragment is intentionally omitted, and legacy query tokens are removed. */
export function redactedSignupAddress(pathname: string, search: string): string {
  const params = new URLSearchParams(search);
  params.delete("token");
  const safeSearch = params.toString();
  return `${pathname}${safeSearch ? `?${safeSearch}` : ""}`;
}

interface InviteLocation {
  pathname: string;
  search: string;
  hash: string;
}

interface InviteHistory {
  readonly state: unknown;
  replaceState(state: unknown, unused: string, url?: string | URL | null): void;
}

/** In-memory handoff captured before the client router observes the location. */
export function createInviteTokenStore() {
  let initialized = false;
  let token = "";

  return Object.freeze({
    capture(location: InviteLocation, history: InviteHistory): void {
      if (location.pathname !== "/ui/signup" && location.pathname !== "/ui/signup/") return;
      if (initialized) return;
      initialized = true;

      const fragment = location.hash.startsWith("#")
        ? location.hash.slice(1)
        : location.hash;
      const fragmentParams = new URLSearchParams(fragment);
      const searchParams = new URLSearchParams(location.search);
      token = fragmentParams.get("token") ?? "";

      if (fragmentParams.has("token") || searchParams.has("token")) {
        history.replaceState(
          history.state,
          "",
          redactedSignupAddress(location.pathname, location.search),
        );
      }
    },
    get(): string {
      return token;
    },
    clear(): void {
      token = "";
    },
  });
}

export const inviteTokenStore = createInviteTokenStore();
