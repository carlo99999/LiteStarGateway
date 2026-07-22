export const TEAM_ROLES = [
  "admin",
  "member",
  "model-manager",
  "key-issuer",
  "billing-viewer",
] as const;

export type TeamRole = (typeof TEAM_ROLES)[number];

export interface AccessibleTeam {
  id: string;
  name: string;
  /** Null means the caller is a platform admin, so membership is irrelevant. */
  role: TeamRole | null;
}

export type ConsoleSurface =
  | "dashboard"
  | "models"
  | "routing"
  | "playground"
  | "credentials"
  | "api-keys"
  | "organizations"
  | "teams"
  | "users"
  | "service-principals"
  | "usage"
  | "budgets"
  | "audit"
  | "sso-settings";

export interface ConsoleAccess {
  isPlatformAdmin: boolean;
  isAuditor: boolean;
  teamRoles: readonly TeamRole[];
}

interface PlatformTeamSummary {
  id: string;
  name: string;
}

interface MembershipSummary {
  team_id: string;
  name: string;
  role: string;
}

function isTeamRole(role: string): role is TeamRole {
  return TEAM_ROLES.some((candidate) => candidate === role);
}

export function fromPlatformTeam(team: PlatformTeamSummary): AccessibleTeam {
  return { id: team.id, name: team.name, role: null };
}

export function fromMembership(membership: MembershipSummary): AccessibleTeam {
  if (!isTeamRole(membership.role)) {
    throw new Error(`Unknown team role: ${membership.role}`);
  }
  return {
    id: membership.team_id,
    name: membership.name,
    role: membership.role,
  };
}

export function canReadModels(role: TeamRole | null): boolean {
  return role === null || role === "admin" || role === "model-manager";
}

export function canManageModels(role: TeamRole | null): boolean {
  return canReadModels(role);
}

export function canReadUsage(role: TeamRole | null): boolean {
  return role === null || role === "admin" || role === "billing-viewer";
}

export function canReadDecisions(role: TeamRole | null): boolean {
  return role === null || role === "admin" || role === "model-manager";
}

const MODEL_SURFACES: readonly ConsoleSurface[] = ["models", "routing", "playground"];

/** Navigation is convenience, not authorization. Keep it aligned with the
 * backend capabilities so users do not land on pages guaranteed to return 403. */
export function canAccessConsoleSurface(
  surface: ConsoleSurface,
  access: ConsoleAccess,
): boolean {
  if (access.isPlatformAdmin) return true;
  if (surface === "dashboard") return true;
  if (surface === "audit") return access.isAuditor;
  if (MODEL_SURFACES.includes(surface)) {
    return access.teamRoles.some((role) => canReadModels(role));
  }
  return false;
}
