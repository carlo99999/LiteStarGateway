import { expect, test } from "@playwright/test";
import { loginAsAdmin } from "./helpers";

test.describe("authentication and RBAC", () => {
  test("an unauthenticated visitor is redirected to the login page", async ({ page }) => {
    // RequireAuth (ui/src/features/auth/RequireAuth.tsx) gates every route
    // under the app shell — hitting one directly with no session must bounce
    // to /login rather than rendering anything.
    await page.goto("teams");
    await expect(page).toHaveURL(/\/ui\/login$/);
  });

  test("the bootstrapped admin can log in and sees platform-admin navigation", async ({
    page,
  }) => {
    await loginAsAdmin(page);

    // RBAC: the sidebar (ui/src/app/layout/Sidebar.tsx) filters nav items by
    // role via canAccessConsoleSurface. Users/organizations are platform-admin
    // only surfaces (see ui/src/app/layout/nav.ts) — their presence confirms
    // the admin session carries is_admin, not just "some" authenticated one.
    await expect(page.getByRole("link", { name: /users$/ })).toBeVisible();
    await expect(page.getByRole("link", { name: /organizations$/ })).toBeVisible();
  });

  test("an invalid password is rejected with an error, not a session", async ({ page }) => {
    await page.goto("login");
    await page.getByLabel("email").fill("e2e-admin@example.com");
    await page.getByLabel("password").fill("definitely-the-wrong-password");
    await page.getByRole("button", { name: "$ sign in" }).click();

    await expect(page.getByRole("alert")).toContainText(/invalid/i);
    await expect(page).toHaveURL(/\/ui\/login$/);
  });
});
