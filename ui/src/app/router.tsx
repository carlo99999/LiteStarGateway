import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
} from "@tanstack/react-router";
import { AppShell } from "@/app/layout/AppShell";
import { PlannedPage } from "@/app/PlannedPage";
import { ApiKeysPage } from "@/features/api-keys/ApiKeysPage";
import { AuditPage } from "@/features/audit/AuditPage";
import { BudgetsPage } from "@/features/budgets/BudgetsPage";
import { CredentialsPage } from "@/features/credentials/CredentialsPage";
import { ModelsPage } from "@/features/models/ModelsPage";
import { RouterDetailPage } from "@/features/routing/RouterDetailPage";
import { RoutingPage } from "@/features/routing/RoutingPage";
import { LoginPage } from "@/features/auth/LoginPage";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { SignupPage } from "@/features/auth/SignupPage";
import { inviteTokenStore } from "@/features/auth/inviteToken";
import { OrganizationDetailPage } from "@/features/organizations/OrganizationDetailPage";
import { OrganizationsPage } from "@/features/organizations/OrganizationsPage";
import { ServicePrincipalsPage } from "@/features/service-principals/ServicePrincipalsPage";
import { TeamDetailPage } from "@/features/teams/TeamDetailPage";
import { TeamsPage } from "@/features/teams/TeamsPage";
import { UsagePage } from "@/features/usage/UsagePage";
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

const modelsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/models",
  component: ModelsPage,
});

const routingRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/routing",
  component: RoutingPage,
});

const routerDetailRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/routing/$teamId/$routerId",
  component: RouterDetailPage,
});

const usageRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/usage",
  component: UsagePage,
});

const budgetsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/budgets",
  component: BudgetsPage,
});

const auditRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/audit",
  component: AuditPage,
});

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
    modelsRoute,
    routingRoute,
    routerDetailRoute,
    usageRoute,
    budgetsRoute,
    auditRoute,
  ]),
]);

export const router = createRouter({ routeTree, basepath: "/ui" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
