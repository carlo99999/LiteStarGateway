/** Pure view logic for the Tools page.
 *
 * It lives outside the component for one reason: the states below are the part
 * this console has repeatedly got wrong, and they are worth testing without a
 * DOM. Three separate rounds of review in this project found a page rendering a
 * failed query as an *empty* result — "nothing here" and "we could not ask" look
 * identical to a user, and only one of them is their problem to fix.
 *
 * MCP adds a third case that the other pages do not have: a registered server
 * whose inventory has never been fetched. So an empty tool list means one of two
 * legitimately different things, distinguished by `last_discovered_at`:
 *
 *  - `null`  → nobody has run discovery yet. The action is "discover".
 *  - a date  → we asked, and the server offers nothing. The action is to look at
 *              the server, not at this page.
 *
 * Collapsing those two would show a working server as unconfigured forever.
 */

export type ToolEffect = "read" | "write" | "destructive";
export type ServerOrigin = "own" | "extended" | "global";

export interface InventoryInput {
  isLoading: boolean;
  isError: boolean;
  errorMessage?: string | null;
  /** Tools as returned by the API; `undefined` while nothing has loaded. */
  tools?: readonly unknown[];
  /** `mcp_server.last_discovered_at` — null when discovery never ran. */
  lastDiscoveredAt?: string | null;
}

export type InventoryView =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "never-discovered" }
  | { kind: "offers-nothing"; discoveredAt: string }
  | { kind: "tools"; count: number };

/** Which of the five inventory states to render. */
export function resolveInventory(input: InventoryInput): InventoryView {
  // Error first and unconditionally: a stale `tools` array from a previous
  // successful fetch must not mask a query that has since failed.
  if (input.isError) {
    return {
      kind: "error",
      message: input.errorMessage?.trim() || "the inventory could not be loaded",
    };
  }
  if (input.isLoading || input.tools === undefined) return { kind: "loading" };
  if (input.tools.length > 0) return { kind: "tools", count: input.tools.length };
  if (!input.lastDiscoveredAt) return { kind: "never-discovered" };
  return { kind: "offers-nothing", discoveredAt: input.lastDiscoveredAt };
}

/** What the user should read for each empty-ish state. Kept next to the resolver
 * so a new state cannot be added without deciding what it says. */
export function describeInventory(view: InventoryView): string {
  switch (view.kind) {
    case "loading":
      return "loading the inventory…";
    case "error":
      return `! ${view.message}`;
    case "never-discovered":
      return "no discovery has run yet — this server has not been asked what it offers";
    case "offers-nothing":
      return "this server advertises no tools";
    case "tools":
      return `${view.count} tool${view.count === 1 ? "" : "s"}`;
  }
}

/** Provenance, in the same words the models and routing pages use. */
export function describeOrigin(origin: ServerOrigin): string {
  switch (origin) {
    case "own":
      return "this team registered it";
    case "extended":
      return "shared with this team by the platform";
    case "global":
      return "available to every team";
  }
}

/** Whether removing this server deletes it or only detaches it *here*.
 *
 * The API answers with the outcome for exactly this reason, and the console has
 * to say which one it will be **before** the click: "remove" meaning two
 * different things silently is how a team admin discovers they revoked a
 * capability from every other tenant. */
export function describeRemoval(origin: ServerOrigin): {
  verb: string;
  consequence: string;
} {
  if (origin === "own") {
    return {
      verb: "delete",
      consequence: "deletes this server and its inventory",
    };
  }
  return {
    verb: "detach",
    consequence: "hides it from this team only — every other team keeps it",
  };
}

/** The message shown after a removal, from the API's own `outcome`.
 * Anything unexpected is reported rather than assumed to be a success. */
export function removalOutcome(outcome: string): string {
  if (outcome === "deleted") return "server deleted";
  if (outcome === "detached") return "detached from this team — still live for the others";
  return `unexpected outcome from the gateway: ${outcome}`;
}

/** Effects are declared by an operator, so the label says what the *gateway*
 * will do with the classification rather than restating the word. */
export function describeEffect(effect: ToolEffect): string {
  switch (effect) {
    case "read":
      return "reads only";
    case "write":
      return "changes something";
    case "destructive":
      return "deletes or irreversibly changes something";
  }
}

/** Whether a tool needs a key to opt in explicitly before it can be invoked. */
export function needsExplicitKeyGrant(effect: ToolEffect): boolean {
  return effect === "destructive";
}

/** The label on the per-key destructive switch.
 *
 * Deliberately not "enable destructive tools": that restates the setting instead
 * of saying what it permits, and it omits the part an operator most needs — that
 * a tool nobody classified counts as destructive, so this switch also covers
 * every tool an operator has not looked at yet.
 */
export const DESTRUCTIVE_TOGGLE_LABEL =
  "let this key invoke tools that delete or irreversibly change something";

export const DESTRUCTIVE_TOGGLE_HELP =
  "Off by default. A tool nobody has classified counts as destructive, so this " +
  "also permits every tool on this server that no operator has reviewed yet.";

/** Human summary of a key's policy, for the list row. */
export function describePolicy(policy: {
  restricted: boolean;
  destructive_enabled: boolean;
  allowed_tools: readonly string[];
}): string {
  if (!policy.restricted) return "unrestricted — every tool except destructive ones";
  const scope =
    policy.allowed_tools.length === 0
      ? "every tool"
      : `${policy.allowed_tools.length} named tool${policy.allowed_tools.length === 1 ? "" : "s"}`;
  return policy.destructive_enabled
    ? `${scope}, destructive included`
    : `${scope}, destructive excluded`;
}

/** `null` renders as "never", not as an empty cell that reads like a bug. */
export function describeLastDiscovery(lastDiscoveredAt: string | null | undefined): string {
  if (!lastDiscoveredAt) return "never";
  const parsed = new Date(lastDiscoveredAt);
  if (Number.isNaN(parsed.getTime())) return "unreadable timestamp";
  return parsed.toLocaleString();
}
