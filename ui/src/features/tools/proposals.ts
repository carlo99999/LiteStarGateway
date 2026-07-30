/** Pure view logic for the proposal queue (Plan 20 S5).
 *
 * Separate from `inventory.ts` because it answers a different question. That file
 * is about a server the team already has; this one is about a request nobody has
 * decided yet — and the states that matter here are the ones an operator acts on:
 *
 *  - `pending`  → somebody is waiting for an answer. The only actionable state.
 *  - `approved` → a server exists; the queue row is now history.
 *  - `rejected` → history too, but it carries the reason the member reads.
 *
 * The failure mode this module exists to prevent is the same one three rounds of
 * review found on other pages: rendering a failed query as an empty queue. "Nobody
 * has asked for anything" and "we could not find out" look identical, and only one
 * of them means an admin can stop looking.
 */

export type ProposalStatus = "pending" | "approved" | "rejected";

export interface QueueInput {
  isLoading: boolean;
  isError: boolean;
  errorMessage?: string | null;
  /** Proposals as returned by the API; `undefined` while nothing has loaded. */
  proposals?: readonly { status: string }[];
}

export type QueueView =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "empty" }
  | { kind: "proposals"; total: number; pending: number };

/** Which of the four queue states to render. */
export function resolveQueue(input: QueueInput): QueueView {
  // Error first and unconditionally: a stale list from an earlier successful fetch
  // must not make a query that has since failed look like a quiet queue.
  if (input.isError) {
    return {
      kind: "error",
      message: input.errorMessage?.trim() || "the proposal queue could not be loaded",
    };
  }
  if (input.isLoading || input.proposals === undefined) return { kind: "loading" };
  if (input.proposals.length === 0) return { kind: "empty" };
  return {
    kind: "proposals",
    total: input.proposals.length,
    pending: input.proposals.filter((proposal) => proposal.status === "pending").length,
  };
}

/** What the user reads for each state. Kept next to the resolver so a new state
 * cannot be added without deciding what it says. */
export function describeQueue(view: QueueView): string {
  switch (view.kind) {
    case "loading":
      return "loading the proposal queue…";
    case "error":
      return `! ${view.message}`;
    case "empty":
      return "no proposals — nobody has asked this team to register a tool server";
    case "proposals":
      return view.pending === 0
        ? `${view.total} proposal${view.total === 1 ? "" : "s"}, none waiting`
        : `${view.pending} waiting for a decision, of ${view.total}`;
  }
}

/** What a status means for the person reading the row, rather than the word again.
 *
 * `approved` deliberately does not say "a server was created": the server may
 * since have been deleted, which the API reports by leaving `server_id` null on an
 * approved proposal. Saying it unconditionally would be a claim the row cannot
 * back up. */
export function describeStatus(status: string): string {
  switch (status) {
    case "pending":
      return "waiting for a team admin";
    case "approved":
      return "approved — registered as a tool server";
    case "rejected":
      return "refused";
    default:
      // An unrecognised status is surfaced, not silently rendered as pending: the
      // permissive reading would offer approve/reject buttons for a state the
      // gateway would refuse.
      return `unrecognised status from the gateway: ${status}`;
  }
}

/** Only a pending proposal can be decided.
 *
 * The gateway enforces this — a second decision is a 409 — and the console must
 * agree with it, because a live approve button on a decided row invites a click
 * whose only outcome is an error. */
export function canDecide(status: string): boolean {
  return status === "pending";
}

/** The reason cell. A rejected proposal without one would mean the gateway let a
 * blank reason through, so it is reported rather than shown as an empty cell. */
export function describeReason(status: string, reason: string | null | undefined): string | null {
  if (status !== "rejected") return null;
  if (!reason?.trim()) return "refused without a reason — this should not happen";
  return reason;
}

/** The warning next to the approve button.
 *
 * Approving is the moment the gateway first connects to somebody else's endpoint,
 * and the allowlist is re-checked right then — so an approval can fail for a
 * proposal that was perfectly legal when it was filed. Saying so up front is what
 * makes that 400 legible instead of surprising. */
export const APPROVE_CONSEQUENCE =
  "registers the server and contacts it for the first time — the url is re-checked " +
  "against MCP_ALLOWED_HOSTS now, so an approval can be refused for a proposal " +
  "that was valid when it was filed";
