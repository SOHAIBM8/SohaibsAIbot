import { test, expect } from "@playwright/test";
import { login } from "./helpers";

const STRATEGY_ID = `e2e_strategy_${Date.now()}`;
const EXCHANGE = "binance";

test.describe("strategy arming", () => {
  test("arm then disarm a strategy, status updates without page reload", async ({ page }) => {
    await login(page);
    await page.goto("/risk");

    await page.getByTestId("arming-strategy-id").fill(STRATEGY_ID);
    await page.getByTestId("arming-exchange").fill(EXCHANGE);
    await page.getByTestId("arming-lookup").click();

    // No arming record exists yet for this fresh strategy_id.
    await expect(page.getByText("No arming record for this strategy/exchange.")).toBeVisible();

    await page.getByTestId("arming-arm-open").click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await page.getByTestId("confirm-dialog-confirm").click();

    await expect(page.getByTestId("confirm-dialog")).toBeHidden();
    await expect(page.getByTestId("arming-status")).toHaveText("Armed");

    await page.getByTestId("arming-disarm-open").click();
    await page.getByTestId("arming-disarm-reason").fill("E2E test disarm");
    await page.getByTestId("confirm-dialog-confirm").click();

    await expect(page.getByTestId("confirm-dialog")).toBeHidden();
    await expect(page.getByTestId("arming-status")).toHaveText("Not armed");
  });

  test("the arm button is disabled until a strategy_id is entered", async ({ page }) => {
    await login(page);
    await page.goto("/risk");

    await expect(page.getByTestId("arming-arm-open")).toBeDisabled();
    await page.getByTestId("arming-strategy-id").fill(`${STRATEGY_ID}_disabled_check`);
    await expect(page.getByTestId("arming-arm-open")).toBeEnabled();
  });
});
