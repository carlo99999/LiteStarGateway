import assert from "node:assert/strict";
import test from "node:test";
import {
  TEAM_ROLES,
  canAccessConsoleSurface,
  canManageTools,
  canProposeTools,
  canReadDecisions,
  canReadUsage,
  canManageModels,
  canReadModels,
  fromMembership,
  fromPlatformTeam,
} from "./access.ts";

test("normalizes a platform team without inventing a membership role", () => {
  const team = fromPlatformTeam({ id: "team-1", name: "Alpha" });

  assert.deepEqual(team, { id: "team-1", name: "Alpha", role: null });
});

test("normalizes and validates a self-scoped membership", () => {
  const team = fromMembership({ team_id: "team-2", name: "Beta", role: "model-manager" });

  assert.deepEqual(team, { id: "team-2", name: "Beta", role: "model-manager" });
  assert.equal(canReadModels(team.role), true);
  assert.equal(canManageModels(team.role), true);
});

test("plain members cannot trigger model or routing requests", () => {
  assert.equal(canReadModels("member"), false);
  assert.equal(canManageModels("member"), false);
  assert.equal(canReadModels("key-issuer"), false);
  assert.equal(canReadModels("billing-viewer"), false);
  assert.equal(canManageModels("admin"), true);
});

test("rejects an unknown role returned by the API", () => {
  assert.throws(
    () => fromMembership({ team_id: "team-3", name: "Gamma", role: "owner" }),
    /Unknown team role/,
  );
});

test("model managers can read decisions but never issue usage queries", () => {
  assert.equal(canReadDecisions("model-manager"), true);
  assert.equal(canReadUsage("model-manager"), false);
  assert.equal(canReadUsage("admin"), true);
  assert.equal(canReadDecisions("admin"), true);
  assert.equal(canReadUsage(null), true);
  assert.equal(canReadDecisions(null), true);
});

test("non-admin navigation exposes only surfaces backed by caller capabilities", () => {
  const modelManager = {
    isPlatformAdmin: false,
    isAuditor: false,
    teamRoles: ["model-manager" as const],
  };
  const teamAdmin = {
    isPlatformAdmin: false,
    isAuditor: false,
    teamRoles: ["admin" as const],
  };
  const billingViewer = {
    isPlatformAdmin: false,
    isAuditor: false,
    teamRoles: ["billing-viewer" as const],
  };
  const auditor = { isPlatformAdmin: false, isAuditor: true, teamRoles: [] };

  assert.equal(canAccessConsoleSurface("dashboard", modelManager), true);
  assert.equal(canAccessConsoleSurface("models", modelManager), true);
  assert.equal(canAccessConsoleSurface("routing", modelManager), true);
  assert.equal(canAccessConsoleSurface("playground", modelManager), true);
  assert.equal(canAccessConsoleSurface("credentials", modelManager), false);
  assert.equal(canAccessConsoleSurface("teams", modelManager), false);
  assert.equal(canAccessConsoleSurface("sso-settings", modelManager), false);
  assert.equal(canAccessConsoleSurface("usage", modelManager), false);
  assert.equal(canAccessConsoleSurface("budgets", modelManager), false);
  assert.equal(canAccessConsoleSurface("audit", modelManager), false);
  // A content control the model owner can switch off is not a control, so the
  // guardrails surface is not theirs — matching `guardrails:manage` on the
  // backend, which `model-manager` deliberately does not hold.
  assert.equal(canAccessConsoleSurface("guardrails", modelManager), false);
  // Tool *servers* are still not theirs — attaching one is an egress decision, and
  // an earlier draft of the design gave `model-manager` `tools:read` purely for
  // convenience, which the backend's RBAC tests refused. The surface opened in
  // Plan 20 S5 anyway, for a different permission: `tools:propose`, which every
  // team role holds. The page shows this role the proposal queue and none of the
  // registry, so navigation opening is not the widening it looks like.
  assert.equal(canAccessConsoleSurface("tools", modelManager), true);
  assert.equal(canManageTools("model-manager"), false);
  assert.equal(canProposeTools("model-manager"), true);

  assert.equal(canAccessConsoleSurface("models", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("routing", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("playground", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("credentials", teamAdmin), false);
  assert.equal(canAccessConsoleSurface("usage", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("budgets", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("guardrails", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("tools", teamAdmin), true);

  // ISSUE-021 (Round 12): billing-viewer holds usage:read/budget:read and
  // must see those two surfaces, but nothing model-related.
  assert.equal(canAccessConsoleSurface("usage", billingViewer), true);
  assert.equal(canAccessConsoleSurface("budgets", billingViewer), true);
  assert.equal(canAccessConsoleSurface("guardrails", billingViewer), false);
  // Same as `model-manager` above: reachable for the proposal queue, not the
  // registry. `tools:propose` is the one permission with no role exceptions.
  assert.equal(canAccessConsoleSurface("tools", billingViewer), true);
  assert.equal(canManageTools("billing-viewer"), false);
  assert.equal(canAccessConsoleSurface("models", billingViewer), false);
  assert.equal(canAccessConsoleSurface("routing", billingViewer), false);

  assert.equal(canAccessConsoleSurface("dashboard", auditor), true);
  assert.equal(canAccessConsoleSurface("audit", auditor), true);
  assert.equal(canAccessConsoleSurface("models", auditor), false);
  // The platform auditor holds usage:read/budget:read in every team
  // (AUDITOR_TEAM_PERMISSIONS), independent of membership.
  assert.equal(canAccessConsoleSurface("usage", auditor), true);
  assert.equal(canAccessConsoleSurface("budgets", auditor), true);
  // Read-only billing visibility does not extend to the guardrail policy: the
  // auditor's grant is `usage:read`/`budget:read`, and nothing else.
  assert.equal(canAccessConsoleSurface("guardrails", auditor), false);
  // Nor to the tool inventory: an inventory is not billing, which is why the
  // corrected permission table gives the auditor no `tools:read` either.
  assert.equal(canAccessConsoleSurface("tools", auditor), false);
});

test("every team role may propose a tool server, and only the admin may register one", () => {
  // `ROLE_PERMISSIONS` does not inherit on the backend, so "any member of the team
  // may ask" has to be spelled out per role there. The console has to agree, or a
  // member holding the permission cannot reach the page that uses it — and so
  // cannot read why their proposal was refused.
  for (const role of TEAM_ROLES) {
    assert.equal(canProposeTools(role), true, `${role} could not propose`);
    assert.equal(
      canAccessConsoleSurface("tools", {
        isPlatformAdmin: false,
        isAuditor: false,
        teamRoles: [role],
      }),
      true,
      `${role} could not reach the Tools page`,
    );
  }
  assert.equal(canManageTools("member"), false);
  // A platform admin has no role in the team and both hold.
  assert.equal(canProposeTools(null), true);
  assert.equal(canManageTools(null), true);
});

test("a caller with no team membership reaches no tool surface at all", () => {
  // Including the platform auditor: its cross-team grant is read-only billing
  // visibility, and filing a proposal is a write an admin may act on.
  const outsider = { isPlatformAdmin: false, isAuditor: true, teamRoles: [] };

  assert.equal(canAccessConsoleSurface("tools", outsider), false);
});

test("platform admins retain every console surface", () => {
  const platformAdmin = { isPlatformAdmin: true, isAuditor: false, teamRoles: [] };
  const surfaces = [
    "dashboard",
    "models",
    "routing",
    "playground",
    "credentials",
    "api-keys",
    "organizations",
    "teams",
    "users",
    "service-principals",
    "usage",
    "budgets",
    "guardrails",
    "tools",
    "audit",
    "sso-settings",
  ] as const;

  assert.equal(surfaces.every((surface) => canAccessConsoleSurface(surface, platformAdmin)), true);
});
