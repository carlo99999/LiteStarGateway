import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
} from "@tanstack/react-router";
import { AppShell } from "@/app/layout/AppShell";
import { PlannedPage } from "@/app/PlannedPage";
import { LoginPage } from "@/features/auth/LoginPage";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { OrganizationDetailPage } from "@/features/organizations/OrganizationDetailPage";
import { OrganizationsPage } from "@/features/organizations/OrganizationsPage";

const rootRoute = createRootRoute({ component: Outlet });

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
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

// Scaffolded-but-not-implemented feature areas (Phase 1 lands their views),
// grouped in the sidebar under gateway / governance / observability.
const planned: { path: string; command: string; title: string }[] = [
  // gateway
  { path: "/models", command: "models list", title: "Models" },
  { path: "/routing", command: "routing inspect", title: "Routing" },
  { path: "/credentials", command: "credentials list", title: "Credentials" },
  { path: "/api-keys", command: "api-keys list", title: "API Keys" },
  // governance
  { path: "/teams", command: "teams list", title: "Teams" },
  { path: "/users", command: "users list", title: "Users" },
  {
    path: "/service-principals",
    command: "service-principals list",
    title: "Service Principals",
  },
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
  appRoute.addChildren([
    indexRoute,
    organizationsRoute,
    organizationDetailRoute,
    ...plannedRoutes,
  ]),
]);

export const router = createRouter({ routeTree, basepath: "/ui" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
