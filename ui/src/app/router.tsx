import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  redirect,
} from "@tanstack/react-router";
import { AppShell } from "@/app/layout/AppShell";
import { PlannedPage } from "@/app/PlannedPage";
import { LoginPage } from "@/features/auth/LoginPage";
import { RequireAuth } from "@/features/auth/RequireAuth";
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
  beforeLoad: () => {
    throw redirect({ to: "/organizations" });
  },
});

const organizationsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/organizations",
  component: OrganizationsPage,
});

// Scaffolded-but-not-implemented feature areas (Phase 1 lands their views).
const planned: { path: string; command: string; title: string }[] = [
  { path: "/teams", command: "teams list", title: "Teams" },
  { path: "/models", command: "models list", title: "Models" },
  { path: "/credentials", command: "credentials list", title: "Credentials" },
  { path: "/api-keys", command: "api-keys list", title: "API Keys" },
  { path: "/budgets", command: "budgets list", title: "Budgets" },
  { path: "/usage", command: "usage report", title: "Usage & Cost" },
  { path: "/routing", command: "routing inspect", title: "Routing" },
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
  appRoute.addChildren([indexRoute, organizationsRoute, ...plannedRoutes]),
]);

export const router = createRouter({ routeTree, basepath: "/ui" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
