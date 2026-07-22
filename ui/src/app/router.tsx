import { lazy } from "react";
import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";
import { AppShell } from "@/app/layout/AppShell";
import { LoginPage } from "@/features/auth/LoginPage";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { SignupPage } from "@/features/auth/SignupPage";
import { inviteTokenStore } from "@/features/auth/inviteToken";

// Route-based code splitting: each authenticated page loads from its own chunk
// on first navigation, so the initial bundle stays small (was one ~535kB chunk
// holding every page). Login/signup stay eager — they're the first paint for an
// unauthenticated visitor. The Suspense boundary around <Outlet /> in AppShell
// shows the pending fallback while a page chunk is in flight.
const DashboardPage = lazy(() =>
  import("@/features/dashboard/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const OrganizationsPage = lazy(() =>
  import("@/features/organizations/OrganizationsPage").then((m) => ({ default: m.OrganizationsPage })),
);
const OrganizationDetailPage = lazy(() =>
  import("@/features/organizations/OrganizationDetailPage").then((m) => ({
    default: m.OrganizationDetailPage,
  })),
);
const TeamsPage = lazy(() =>
  import("@/features/teams/TeamsPage").then((m) => ({ default: m.TeamsPage })),
);
const TeamDetailPage = lazy(() =>
  import("@/features/teams/TeamDetailPage").then((m) => ({ default: m.TeamDetailPage })),
);
const ApiKeysPage = lazy(() =>
  import("@/features/api-keys/ApiKeysPage").then((m) => ({ default: m.ApiKeysPage })),
);
const UsersPage = lazy(() =>
  import("@/features/users/UsersPage").then((m) => ({ default: m.UsersPage })),
);
const ServicePrincipalsPage = lazy(() =>
  import("@/features/service-principals/ServicePrincipalsPage").then((m) => ({
    default: m.ServicePrincipalsPage,
  })),
);
const CredentialsPage = lazy(() =>
  import("@/features/credentials/CredentialsPage").then((m) => ({ default: m.CredentialsPage })),
);
const ModelsPage = lazy(() =>
  import("@/features/models/ModelsPage").then((m) => ({ default: m.ModelsPage })),
);
const RoutingPage = lazy(() =>
  import("@/features/routing/RoutingPage").then((m) => ({ default: m.RoutingPage })),
);
const PlaygroundPage = lazy(() =>
  import("@/features/playground/PlaygroundPage").then((m) => ({ default: m.PlaygroundPage })),
);
const RouterDetailPage = lazy(() =>
  import("@/features/routing/RouterDetailPage").then((m) => ({ default: m.RouterDetailPage })),
);
const UsagePage = lazy(() =>
  import("@/features/usage/UsagePage").then((m) => ({ default: m.UsagePage })),
);
const BudgetsPage = lazy(() =>
  import("@/features/budgets/BudgetsPage").then((m) => ({ default: m.BudgetsPage })),
);
const AuditPage = lazy(() =>
  import("@/features/audit/AuditPage").then((m) => ({ default: m.AuditPage })),
);

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
  component: DashboardPage,
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

const playgroundRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/playground",
  component: PlaygroundPage,
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
    playgroundRoute,
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
