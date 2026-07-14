import type { Page } from "@playwright/test";

/**
 * Operator credentials for the dev stack these E2E tests run against
 * (see playwright.config.ts's module docstring for the exact env vars
 * the backend must be started with). Overridable via env vars so this
 * isn't hardcoded to one developer's local bcrypt hash.
 */
export const E2E_USERNAME = process.env.E2E_OPERATOR_USERNAME ?? "devops";
export const E2E_PASSWORD = process.env.E2E_OPERATOR_PASSWORD ?? "dev-password-123";

export async function login(page: Page) {
  await page.goto("/login");
  await page.getByTestId("login-username").fill(E2E_USERNAME);
  await page.getByTestId("login-password").fill(E2E_PASSWORD);
  await page.getByTestId("login-submit").click();
  await page.waitForURL("/");
}
