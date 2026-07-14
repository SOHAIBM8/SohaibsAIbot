# E2E tests (Playwright)

Covers arm/disarm, kill-switch, and credential entry end to end against
the real dev stack — no mocked backend, matching this project's
standing "no mocks for anything DB-adjacent" discipline.

## Before running

1. `docker compose up -d` (Postgres)
2. Start the backend with a real bcrypt operator password hash:
   ```bash
   export DASHBOARD_OPERATOR_USERNAME="devops"
   export DASHBOARD_OPERATOR_PASSWORD_HASH='<bcrypt hash>'
   export DASHBOARD_ACCOUNT_ID="default"
   python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
   ```
   Generate a hash with:
   ```bash
   python -c "import bcrypt; print(bcrypt.hashpw(b'<password>', bcrypt.gensalt()).decode())"
   ```
3. `npm run dev` (Vite on :5173)
4. If your username/password differ from the defaults
   (`devops` / `dev-password-123`), set `E2E_OPERATOR_USERNAME` /
   `E2E_OPERATOR_PASSWORD` before running the tests.

## Run

```bash
npm run test:e2e
```

## Notes

- Tests seed their own data with timestamp-suffixed identifiers
  (`e2e_strategy_<ts>`, `e2e_exchange_<ts>`) to avoid colliding with
  real data or other test runs — nothing is cleaned up from Postgres
  afterward except the kill switch (global, shared state), which
  `kill-switch.spec.ts`'s `afterEach` always leaves disengaged.
- `playwright.config.ts` does not auto-start the dev servers
  (`webServer`) — the backend needs env vars and a running Postgres
  this config has no business owning.
