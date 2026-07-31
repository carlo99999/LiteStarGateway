import assert from "node:assert/strict";
import test from "node:test";
import {
  barPercent,
  formatPercent,
  formatUsd,
  orgTotals,
  topTeams,
  totalCost,
  type OrgSpend,
} from "./rollup.ts";

function spend(overrides: Partial<OrgSpend>): OrgSpend {
  return { organization_id: "org-1", total_cost: 0, teams: [], ...overrides };
}

test("topTeams sorts across organizations and truncates to the limit", () => {
  const spends = [
    spend({
      organization_id: "org-1",
      teams: [
        { team_id: "t1", name: "alpha", cost: 1 },
        { team_id: "t2", name: "beta", cost: 9 },
      ],
    }),
    spend({
      organization_id: "org-2",
      teams: [{ team_id: "t3", name: "gamma", cost: 5 }],
    }),
  ];
  assert.deepEqual(
    topTeams(spends, 2).map((team) => team.name),
    ["beta", "gamma"],
  );
});

test("topTeams does not mutate the caller's team arrays", () => {
  const teams = [
    { team_id: "t1", name: "alpha", cost: 1 },
    { team_id: "t2", name: "beta", cost: 9 },
  ];
  topTeams([spend({ teams })], 2);
  assert.deepEqual(
    teams.map((team) => team.name),
    ["alpha", "beta"],
  );
});

test("orgTotals pairs organizations with their rollup, priciest first", () => {
  const organizations = [
    { id: "org-1", name: "acme" },
    { id: "org-2", name: "globex" },
  ];
  const spends = [
    spend({ organization_id: "org-1", total_cost: 2 }),
    spend({ organization_id: "org-2", total_cost: 7 }),
  ];
  assert.deepEqual(orgTotals(organizations, spends, 10), [
    { organization_id: "org-2", name: "globex", cost: 7 },
    { organization_id: "org-1", name: "acme", cost: 2 },
  ]);
});

test("orgTotals drops organizations whose rollup has not loaded", () => {
  const organizations = [
    { id: "org-1", name: "acme" },
    { id: "org-2", name: "globex" },
  ];
  const totals = orgTotals(organizations, [spend({ organization_id: "org-1", total_cost: 2 })], 10);
  assert.deepEqual(
    totals.map((total) => total.name),
    ["acme"],
  );
});

test("totalCost sums organization rollups", () => {
  assert.equal(totalCost([spend({ total_cost: 1.5 }), spend({ total_cost: 2.25 })]), 3.75);
});

test("barPercent is zero when there is nothing to scale against", () => {
  assert.equal(barPercent(5, 0), 0);
  assert.equal(barPercent(0, 0), 0);
  assert.equal(barPercent(5, 10), 50);
});

test("formatUsd and formatPercent render fixed-precision values", () => {
  assert.equal(formatUsd(12.3456), "$12.35");
  assert.equal(formatPercent(0.8342), "83.4%");
});
