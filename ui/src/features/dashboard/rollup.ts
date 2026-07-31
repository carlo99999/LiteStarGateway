/** Pure aggregation for the dashboard panels: the spend rollups are assembled
 * from one `/organizations/{id}/spend` call per organization, so the slicing is
 * kept here where it can be tested without React. */

export interface TeamSpend {
  team_id: string;
  name: string;
  cost: number;
}

export interface OrgSpend {
  organization_id: string;
  total_cost: number;
  teams: TeamSpend[];
}

export interface OrgTotal {
  organization_id: string;
  name: string;
  cost: number;
}

/** The priciest teams across every organization, highest cost first. */
export function topTeams(spends: readonly OrgSpend[], limit: number): TeamSpend[] {
  return spends
    .flatMap((spend) => spend.teams)
    .slice()
    .sort((a, b) => b.cost - a.cost)
    .slice(0, limit);
}

/** Per-organization totals, highest cost first. Organizations without a loaded
 * rollup are dropped rather than reported as $0.00. */
export function orgTotals(
  organizations: readonly { id: string; name: string }[],
  spends: readonly OrgSpend[],
  limit: number,
): OrgTotal[] {
  const byId = new Map(spends.map((spend) => [spend.organization_id, spend]));
  return organizations
    .flatMap((org) => {
      const spend = byId.get(org.id);
      return spend ? [{ organization_id: org.id, name: org.name, cost: spend.total_cost }] : [];
    })
    .sort((a, b) => b.cost - a.cost)
    .slice(0, limit);
}

export function totalCost(spends: readonly OrgSpend[]): number {
  return spends.reduce((sum, spend) => sum + spend.total_cost, 0);
}

/** Bar width as a percentage of the largest value in the same list. */
export function barPercent(cost: number, max: number): number {
  if (max <= 0) return 0;
  return Math.round((cost / max) * 100);
}

export function formatUsd(cost: number): string {
  return `$${cost.toFixed(2)}`;
}

export function formatCount(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

/** Hit rates arrive as a 0..1 fraction. */
export function formatPercent(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

export function formatWhen(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}
