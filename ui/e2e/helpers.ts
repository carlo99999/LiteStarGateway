import { expect, type Page } from "@playwright/test";
import { E2E_ADMIN_EMAIL, E2E_ADMIN_PASSWORD } from "../playwright.config";

/** A short, run-unique suffix so repeated local runs against the same
 * long-lived backend (`reuseExistingServer` outside CI) never collide on a
 * unique name (team/org names, key names). */
export function unique(label: string): string {
  return `${label}-${Date.now()}-${Math.floor(Math.random() * 10_000)}`;
}

/** Log in through the real login form (the admin bootstrapped from
 * MASTER_KEY/ADMIN_EMAIL — see playwright.config.ts) and wait for the
 * authenticated shell to render. */
export async function loginAsAdmin(page: Page): Promise<void> {
  // Relative, no leading slash: baseURL already ends in "/ui/", and a
  // leading "/" would resolve against the origin instead, dropping it.
  await page.goto("login");
  await page.getByLabel("email").fill(E2E_ADMIN_EMAIL);
  await page.getByLabel("password").fill(E2E_ADMIN_PASSWORD);
  await page.getByRole("button", { name: "$ sign in" }).click();
  await expect(page).toHaveURL(/\/ui\/?$/);
  await expect(page.getByText("gateway v")).toBeVisible();
}

/** Create an organization, then a team inside it with the bootstrapped admin
 * as the team's admin. Shared by every flow that needs a team fixture — the
 * creation itself is also the "critical mutation" coverage for both forms. */
export async function createOrgAndTeam(
  page: Page,
  orgName: string,
  teamName: string,
): Promise<void> {
  await page.goto("organizations");
  await page.getByRole("button", { name: "New organization" }).click();
  const orgDialog = page.getByRole("dialog");
  await orgDialog.getByLabel("name", { exact: true }).fill(orgName);
  await orgDialog.getByRole("button", { name: "create" }).click();
  await expect(page.getByRole("link", { name: orgName })).toBeVisible();

  // Teams require an existing user as the team admin — the bootstrapped
  // admin qualifies (see UserService.ensure_admin / create_team).
  await page.goto("teams");
  await page.getByRole("button", { name: "New team" }).click();
  const teamDialog = page.getByRole("dialog");
  await teamDialog.getByLabel("organization").selectOption({ label: orgName });
  await teamDialog.getByLabel("name", { exact: true }).fill(teamName);
  await teamDialog.getByLabel("admin email").fill(E2E_ADMIN_EMAIL);
  await teamDialog.getByRole("button", { name: "create" }).click();
  await expect(page.getByRole("link", { name: teamName })).toBeVisible();
}
