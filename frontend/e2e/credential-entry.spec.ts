import { test, expect } from "@playwright/test";
import { login } from "./helpers";

const EXCHANGE = `e2e_exchange_${Date.now()}`;

/**
 * E2E coverage for credential entry, per the confirmed decision to
 * extend dedicated E2E coverage beyond arm/disarm and kill-switch to
 * this flow too, given its stakes (spec section 25's own reasoning,
 * applied identically here — a security-consequential control action,
 * not a data-view page).
 */
test.describe("credential entry", () => {
  test("registers a testnet credential and never displays the plaintext back", async ({
    page,
  }) => {
    await login(page);
    await page.goto("/settings");

    await page.getByTestId("add-credential-open").click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();

    await page.getByTestId("credential-exchange").fill(EXCHANGE);
    await page.getByTestId("credential-api-key").fill("e2e-plaintext-api-key");
    await page.getByTestId("credential-api-secret").fill("e2e-plaintext-api-secret");
    await page.getByTestId("confirm-dialog-confirm").click();

    await expect(page.getByTestId("confirm-dialog")).toBeHidden();

    const row = page.getByTestId("credential-row").filter({ hasText: EXCHANGE });
    await expect(row).toBeVisible();
    await expect(row).toContainText("Testnet");
    await expect(row).toContainText("pending_validation");

    // The raw plaintext must never appear anywhere on the page after
    // submission — the response only ever carries metadata.
    await expect(page.locator("body")).not.toContainText("e2e-plaintext-api-key");
    await expect(page.locator("body")).not.toContainText("e2e-plaintext-api-secret");
  });

  test("the API secret field masks input", async ({ page }) => {
    await login(page);
    await page.goto("/settings");

    await page.getByTestId("add-credential-open").click();
    const secretField = page.getByTestId("credential-api-secret");
    await expect(secretField).toHaveAttribute("type", "password");

    await page.getByText("Cancel").click();
    await expect(page.getByTestId("confirm-dialog")).toBeHidden();
  });
});
