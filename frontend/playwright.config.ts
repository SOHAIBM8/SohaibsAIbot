import { defineConfig, devices } from "@playwright/test";

/**
 * E2E coverage per spec section 25's explicit call-out ("these two get
 * dedicated E2E coverage, not just unit tests, given what they
 * control") plus the confirmed decision to extend that to credential
 * entry too. Runs against the real dev stack (Vite dev server + real
 * FastAPI + real local Postgres) — no mocked backend, matching this
 * project's standing "no mocks for anything DB-adjacent" discipline
 * applied to E2E as well.
 *
 * Requires, already running before `npx playwright test`:
 *   - `docker compose up -d` (Postgres)
 *   - `uvicorn api.main:app --port 8000` with DASHBOARD_OPERATOR_USERNAME/
 *     DASHBOARD_OPERATOR_PASSWORD_HASH set to a real bcrypt hash
 *   - `npm run dev` (Vite on :5173)
 * webServer is deliberately NOT configured to auto-start these —
 * the backend needs env vars/Postgres this config has no business
 * owning; see e2e/README.md.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // control-surface tests share global backend state (kill switch)
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
