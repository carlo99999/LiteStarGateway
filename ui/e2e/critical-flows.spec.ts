import { expect, test } from "@playwright/test";
import { createOrgAndTeam, loginAsAdmin, unique } from "./helpers";

test.describe("critical admin-console mutations", () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test("admin creates an organization, then a team inside it", async ({ page }) => {
    await createOrgAndTeam(page, unique("E2E Org"), unique("E2E Team"));
  });

  test("admin issues a personal API key for a team", async ({ page }) => {
    const teamName = unique("E2E Team");
    await createOrgAndTeam(page, unique("E2E Org"), teamName);

    // Act: issue a personal key for that team. The plaintext is generated
    // fresh by the backend for this run — a disposable test fixture, never a
    // real credential — and only ever appears on a passing run's DOM, not in
    // any captured artifact (screenshots/traces are only-on-failure).
    await page.goto("api-keys");
    await page.getByRole("button", { name: "Issue key" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("team", { exact: true }).selectOption({ label: teamName });
    await dialog.getByLabel("name", { exact: true }).fill(unique("ci-key"));
    await dialog.getByRole("button", { name: "issue" }).click();

    await expect(dialog.getByText("API key created")).toBeVisible();
    await expect(dialog.getByText(/^lsk_/)).toBeVisible();
    await dialog.getByRole("button", { name: "done" }).click();
  });

  test("usage and budgets pages render for the admin", async ({ page }) => {
    await page.goto("usage");
    await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();

    await page.goto("budgets");
    await expect(page.getByRole("heading", { name: "Budgets" })).toBeVisible();
  });
});
