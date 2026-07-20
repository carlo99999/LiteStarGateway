import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
} from "@tanstack/react-router";
import { AppShell } from "@/app/layout/AppShell";
import { PlannedPage } from "@/app/PlannedPage";
import { ApiKeysPage } from "@/features/api-keys/ApiKeysPage";
import { CredentialsPage } from "@/features/credentials/CredentialsPage";
import { LoginPage } from "@/features/auth/LoginPage";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { SignupPage } from "@/features/auth/SignupPage";
import { inviteTokenStore } from "@/features/auth/inviteToken";
import { OrganizationDetailPage } from "@/features/organizations/OrganizationDetailPage";
import { OrganizationsPage } from "@/features/organizations/OrganizationsPage";
import { ServicePrincipalsPage } from "@/features/service-principals/ServicePrincipalsPage";
import { TeamDetailPage } from "@/features/teams/TeamDetailPage";
import { TeamsPage } from "@/features/teams/TeamsPage";
import { UsersPage } from "@/features/users/UsersPage";

function captureInviteTokenFromWindow() {
  if (typeof window !== "undefined") {
    inviteTokenStore.capture(window.location, window.history);
  }
}

// Initial document load: scrub before TanStack Router observes the location.
captureInviteTokenFromWindow();

const rootRoute = createRootRoute({ component: Outlet });

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
});

// Public invite-redemption page (consumes the token from the URL fragment).
const signupRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/signup",
  // Also covers a client-side transition from another public route.
  beforeLoad: captureInviteTokenFromWindow,
  component: SignupPage,
});

// Pathless layout route: everything under it requires an authenticated session.
const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  component: () => (
    <RequireAuth>
      <AppShell />
    </RequireAuth>
  ),
});

const indexRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/",
  component: () => <PlannedPage command="dashboard" title="Dashboard" />,
});

const organizationsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/organizations",
  component: OrganizationsPage,
});

const organizationDetailRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/organizations/$organizationId",
  component: OrganizationDetailPage,
});

const teamsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/teams",
  component: TeamsPage,
});

const teamDetailRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/teams/$teamId",
  component: TeamDetailPage,
});

const apiKeysRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/api-keys",
  component: ApiKeysPage,
});

const usersRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/users",
  component: UsersPage,
});

const servicePrincipalsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/service-principals",
  component: ServicePrincipalsPage,
});

const credentialsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/credentials",
  component: CredentialsPage,
});

// Scaffolded-but-not-implemented feature areas (Phase 1 lands their views),
// grouped in the sidebar under gateway / governance / observability.
const planned: { path: string; command: string; title: string }[] = [
  // gateway
  { path: "/models", command: "models list", title: "Models" },
  { path: "/routing", command: "routing inspect", title: "Routing" },
  // observability
  { path: "/usage", command: "usage report", title: "Usage & Cost" },
  { path: "/budgets", command: "budgets list", title: "Budgets" },
  { path: "/audit", command: "audit log", title: "Audit Log" },
];

const plannedRoutes = planned.map((p) =>
  createRoute({
    getParentRoute: () => appRoute,
    path: p.path,
    component: () => <PlannedPage command={p.command} title={p.title} />,
  }),
);

const routeTree = rootRoute.addChildren([
  loginRoute,
  signupRoute,
  appRoute.addChildren([
    indexRoute,
    organizationsRoute,
    organizationDetailRoute,
    teamsRoute,
    teamDetailRoute,
    apiKeysRoute,
    usersRoute,
    servicePrincipalsRoute,
    credentialsRoute,
    ...plannedRoutes,
  ]),
]);

export const router = createRouter({ routeTree, basepath: "/ui" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
