import assert from "node:assert/strict";
import test from "node:test";
import {
  canAccessConsoleSurface,
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
  assert.equal(canAccessConsoleSurface("usage", modelManager), false);
  assert.equal(canAccessConsoleSurface("budgets", modelManager), false);
  assert.equal(canAccessConsoleSurface("audit", modelManager), false);

  assert.equal(canAccessConsoleSurface("models", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("routing", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("playground", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("credentials", teamAdmin), false);
  assert.equal(canAccessConsoleSurface("usage", teamAdmin), true);
  assert.equal(canAccessConsoleSurface("budgets", teamAdmin), true);

  // ISSUE-021 (Round 12): billing-viewer holds usage:read/budget:read and
  // must see those two surfaces, but nothing model-related.
  assert.equal(canAccessConsoleSurface("usage", billingViewer), true);
  assert.equal(canAccessConsoleSurface("budgets", billingViewer), true);
  assert.equal(canAccessConsoleSurface("models", billingViewer), false);
  assert.equal(canAccessConsoleSurface("routing", billingViewer), false);

  assert.equal(canAccessConsoleSurface("dashboard", auditor), true);
  assert.equal(canAccessConsoleSurface("audit", auditor), true);
  assert.equal(canAccessConsoleSurface("models", auditor), false);
  // The platform auditor holds usage:read/budget:read in every team
  // (AUDITOR_TEAM_PERMISSIONS), independent of membership.
  assert.equal(canAccessConsoleSurface("usage", auditor), true);
  assert.equal(canAccessConsoleSurface("budgets", auditor), true);
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
    "audit",
  ] as const;

  assert.equal(surfaces.every((surface) => canAccessConsoleSurface(surface, platformAdmin)), true);
});
