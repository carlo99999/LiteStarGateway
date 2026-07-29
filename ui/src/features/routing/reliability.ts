/** Pure presentation logic for the reliability panel.
 *
 * Split out of the component so the two judgement calls are testable: what an
 * unknown state must look like, and how a rate is worded.
 */

import type { RouterReliability } from "@/features/routing/api";

export const BREAKER_LABEL: Record<string, string> = {
  closed: "taking traffic",
  open: "shut out",
  half_open: "one trial pending",
};

/** How many candidates are not currently taking traffic.
 *
 * `—` while the query is in flight, deliberately: on a reliability panel "zero
 * breakers tripped" and "we do not know yet" must not render the same, because
 * the first is reassurance and the second is not. */
export function trippedCount(reliability: RouterReliability | undefined): string | number {
  if (!reliability) return "—";
  return reliability.candidates.filter((candidate) => candidate.breaker !== "closed").length;
}

/** A breaker state as an operator sentence, falling back to the raw value so a
 * state added on the backend shows up as itself rather than disappearing. */
export function breakerLabel(state: string): string {
  return BREAKER_LABEL[state] ?? state;
}

/** The failover rate as a percentage string. */
export function formatFailoverRate(reliability: RouterReliability | undefined): string {
  if (!reliability) return "—";
  return `${(reliability.failover_rate * 100).toFixed(1)}%`;
}
