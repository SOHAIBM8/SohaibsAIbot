import { test, expect } from "@playwright/test";
import { login } from "./helpers";

/**
 * Dedicated E2E coverage per spec section 25 — the kill switch is
 * explicitly called out for E2E, not just unit tests, given what it
 * controls. Runs against the real backend/Postgres; asserts the
 * disengage happens at the end regardless of pass/fail so this suite
 * never leaves the shared global kill switch engaged for a later test
 * run.
 */
test.describe("kill switch", () => {
  test.afterEach(async ({ page }) => {
    // Best-effort cleanup: leave the switch disengaged no matter what
    // happened in the test body above. isVisible() alone is a snapshot
    // check — called right after goto(), before the kill-switch query
    // has resolved, it would always report false and silently skip
    // cleanup. Wait for the status badge to actually render first.
    await page.goto("/risk");
    await page.getByTestId("kill-switch-status").waitFor({ state: "visible" });
    const disengageButton = page.getByTestId("kill-switch-disengage-open");
    if (await disengageButton.isVisible()) {
      await disengageButton.click();
      await page.getByTestId("confirm-dialog-confirm").click();
      await expect(page.getByTestId("confirm-dialog")).toBeHidden();
    }
  });

  test("engage requires a reason and updates the visible status", async ({ page }) => {
    await login(page);
    await page.goto("/risk");

    await expect(page.getByTestId("kill-switch-status")).toHaveText("Disengaged");

    await page.getByTestId("kill-switch-engage-open").click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();

    // Confirm button proceeds even with an empty reason at the UI
    // layer — the backend itself is the real enforcement boundary
    // (min_length=1 on KillSwitchEngageIn) and must reject it. This
    // proves the frontend surfaces that rejection rather than hiding it.
    await page.getByTestId("confirm-dialog-confirm").click();
    await expect(page.getByTestId("confirm-dialog-error")).toBeVisible();

    await page.getByTestId("kill-switch-engage-reason").fill("E2E test engage");
    await page.getByTestId("confirm-dialog-confirm").click();

    await expect(page.getByTestId("confirm-dialog")).toBeHidden();
    await expect(page.getByTestId("kill-switch-status")).toHaveText("Engaged");
    await expect(page.getByText("E2E test engage")).toBeVisible();
  });

  test("disengage returns the switch to disengaged", async ({ page }) => {
    await login(page);
    await page.goto("/risk");

    await page.getByTestId("kill-switch-engage-open").click();
    await page.getByTestId("kill-switch-engage-reason").fill("E2E test engage before disengage");
    await page.getByTestId("confirm-dialog-confirm").click();
    await expect(page.getByTestId("kill-switch-status")).toHaveText("Engaged");

    await page.getByTestId("kill-switch-disengage-open").click();
    await page.getByTestId("confirm-dialog-confirm").click();

    await expect(page.getByTestId("confirm-dialog")).toBeHidden();
    await expect(page.getByTestId("kill-switch-status")).toHaveText("Disengaged");
  });

  test("cancelling the dialog does not change the kill switch state", async ({ page }) => {
    await login(page);
    await page.goto("/risk");

    await page.getByTestId("kill-switch-engage-open").click();
    await page.getByText("Cancel").click();

    await expect(page.getByTestId("confirm-dialog")).toBeHidden();
    await expect(page.getByTestId("kill-switch-status")).toHaveText("Disengaged");
  });
});
