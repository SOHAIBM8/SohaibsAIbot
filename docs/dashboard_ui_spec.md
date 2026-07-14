# Dashboard / Web UI — implementation specification

Status: approved architecture. Read alongside `CLAUDE.md` and every
prior spec in `docs/`. This spec introduces one new backend service
(a FastAPI API layer) that did not previously exist — every UI
capability below depends on it, and it is designed as a thin,
strongly-typed wrapper over `core/`, never a second place where
trading logic could diverge from what `core/` already decided.

## Locked-in decisions

| # | Decision |
|---|----------|
| 1 | A new FastAPI service is required and in scope for this spec — the dashboard cannot exist without it. |
| 2 | Build order: read-only monitoring pages first; control surfaces (arm/disarm, kill switch, strategy enable, credential entry) last — same staged-risk discipline as the execution engine. |
| 3 | Current trading mode (paper / testnet / mainnet) is a persistent, always-visible UI element on every page. |
| 4 | No optimistic UI updates for any control action affecting trading state. |
| 5 | The frontend never receives decrypted credential material — masked display only. |
| 6 | All data access goes through the API layer; no direct Postgres access from the frontend, no bypass read path. |
| 7 | Single-operator session auth for V1; schema designed for multi-tenant extension later (many backend tables already carry `account_id`), not fully built now. |

## 1. Responsibilities

Owns: presenting the current state of every backend component, and
forwarding explicitly-confirmed operator actions to the API layer.
**Must never**: compute a signal, size a position, make a risk
decision, or become a second source of truth — it only displays what
`core/` already decided and relays user intent, gated by the exact
same backend checks that already exist.

## 2. Overall architecture

Three tiers, diagrammed above: React/TypeScript SPA → new FastAPI
service → existing `core/` + Postgres. A WebSocket gateway inside the
FastAPI service bridges the internal Postgres `LISTEN/NOTIFY` event
bus to authenticated, account-scoped browser connections — the
frontend never touches Postgres or the bus directly.

## 3. Frontend technology choice and justification

**React + TypeScript** — type safety matching the backend's typed-
dataclass discipline throughout this project; a financial control
surface is exactly the place to keep that consistency. **TanStack
Query** for server state (caching, refetch, avoids hand-rolled fetch
logic and the "two copies of the truth" bug class). Minimal local
state via React context/Zustand rather than Redux — avoiding the
premature-complexity trap this project has consistently avoided
elsewhere. **Tailwind + shadcn/ui** for components. **TradingView's
`lightweight-charts`** for candlestick/market visuals — purpose-built
for this exact use case rather than a generic charting library
stretched to fit. **Vite** for build tooling.

## 4. Backend API architecture

New `api/` FastAPI service. Pydantic schemas mirror existing `core/`
dataclasses field-for-field — trading semantics are never redefined
at this layer. REST endpoints for query/CRUD-style data (experiments,
strategy list, settings); one WebSocket endpoint for live streams
(orders, fills, risk decisions, regime changes, notifications). Every
mutating endpoint that touches trading state passes through the exact
same `RiskEngine`/`ArmingService`/`KillSwitch` checks `core/` already
enforces — the API layer adds authentication and transport, it does
not add or relax policy.

## 5. Component breakdown

**Frontend**: routing shell, auth module, WebSocket client/store, one
page component per section 9–18 below, a shared library (charts,
tables, status/mode badges, confirmation-dialog primitive used by
every control action). **Backend**: FastAPI app, one route module per
domain (`experiments`, `strategies`, `risk`, `execution`, `ai_assistant`,
`settings`), WebSocket connection manager, auth middleware.

## 6. Authentication flow

Single-operator session auth for V1: login issues a short-lived signed
session token, stored in an **httpOnly cookie** (never `localStorage`
— basic XSS mitigation), refreshed on activity. Session payload
already carries `account_id`, so extending to real multi-user RBAC
later is additive, not a rewrite — consistent with how most backend
tables were already designed with `account_id` in mind.

## 7. Navigation structure

Dashboard (overview) · Live Market · Portfolio · Orders · Positions ·
Strategies · Risk · Experiments · AI Assistant · Settings ·
Notifications. The mode indicator (decision #3) lives in the persistent
header, present on every one of these, not buried in Settings.

## 8. Dashboard layout

Equity curve (`account_snapshots`), mode banner, open position count,
today's PnL sourced directly from `LossLimitTracker`'s own figure
(never recomputed independently in the frontend), recent risk
decisions, and the latest AI-generated daily summary if one exists.

## 9. Live market screen

Candlestick chart per symbol/timeframe from the WebSocket market data
bridge (reusing the execution engine's normalized feed), with a
regime badge overlay from the current `RegimeState`. Strictly
read-only — no manual order entry that could bypass the risk pipeline
lives here.

## 10. Portfolio page

Equity curve, open position summary, exposure figures from
`ExposureTracker`, daily/weekly PnL and limit status from
`LossLimitTracker` — entirely display, no controls.

## 11. Orders page

Order history and live state table (`orders`/`fills`), filterable by
strategy/symbol/mode/status. Cancel-order is the one control action
here, and it goes through the same confirmation-friction pattern as
every other control action (section 24).

## 12. Positions page

Open positions across strategies, unrealized PnL, stop/target levels,
regime at entry, with a direct link into the AI explanation for that
position's originating trade. Read-only.

## 13. Strategy management

Registered strategies (`StrategyRegistry`), per-strategy version
history and regime affinity display. Enabling a strategy for **live**
trading (as opposed to backtest/paper) routes through `ArmingService`
— this page concentrates most of the control-surface build risk in
the whole dashboard and is sequenced accordingly (section 26).

## 14. Risk monitoring

Live view of active `RiskConfig`, current drawdown tier, circuit
breaker states, and kill switch status with an explicit
engage/disengage control. Disengage requires the same manual re-arm
confirmation already enforced backend-side — the UI is not permitted
to make this easier than the backend allows, by design.

## 15. Experiment tracker UI

Browse and compare experiments, metrics side-by-side, equity curve
overlays. Entirely read-only — a natural first page to build, given
zero control-surface risk.

## 16. AI explanations UI

Chat interface wired to `ChatQueryService`; a trade/risk-decision
explanation viewer wired to `ExplanationCache`. Must visually
distinguish "the deterministic system decided X" from "the AI is
describing X" — a clear visual language (e.g., distinct styling for
narrated vs. structured-fact content) so no user ever mistakes
narration for a decision.

## 17. Settings pages

Credential management: masked display only (last 4 characters, status
badge), "add new credential" submits directly to `CredentialVault` via
the API and is never stored or logged client-side. Risk config
viewing is available in V1; *editing* is a control-surface capability,
gated to a later build step. Notification preferences.

## 18. Notifications UI

In-app feed plus optional external channel (email/webhook,
config-driven) for `KillSwitchEngaged`, `CredentialValidationFailed`,
drawdown-breach-class events. Severity is inherited directly from the
backend's own event definitions — the frontend does not invent its
own severity taxonomy.

## 19. WebSocket architecture

One gateway process subscribing to the internal `EventBus`,
republishing a filtered, account-scoped subset to authenticated
browser connections. Client-side reconnect/backoff mirrors the same
pattern already built for exchange WebSocket connections server-side
— a proven pattern, not reinvented for the frontend.

## 20. State management

Server state lives exclusively in TanStack Query's cache — never
duplicated into a separate store, which is how "the UI shows something
different from what the backend actually says" bugs happen. Local-only
UI state (modal open/closed, form drafts) stays in component state or
a minimal store.

## 21. Error handling

Every API error surfaces the backend's actual structured reason (a
`RejectionReason` enum value, a validation failure detail) rather than
a generic message — this system already produces precise, typed
reasons everywhere upstream; the UI must not discard that precision.

## 22. Loading states

Skeleton states for data views. Control actions get a distinct
"pending confirmation" state, not a generic spinner — these calls
legitimately take longer, since they pass through the full risk/arming
gate chain, and that's a feature of the system working correctly, not
latency to be hidden.

## 23. Performance considerations

Paginate/virtualize large tables (`orders`, `fills`, `signal_log` grow
without bound). WebSocket subscriptions scoped to the current page's
symbols/strategies — never a firehose of every system-wide event to
every connected client.

## 24. Security considerations

httpOnly session cookies, CSRF protection on all mutating endpoints,
a strict CSP (no inline scripts — this is a financial control
surface), rate limiting on auth endpoints, and restated because it
cannot be restated enough: **no code path in the frontend ever
receives decrypted credential material.**

## 25. Testing strategy

Frontend component tests; backend API integration tests against a
real test database (consistent with this project's standing "no
mocks for DB-touching tests" practice); E2E tests (Playwright)
specifically covering the arm/disarm and kill-switch flows end to
end — these two get dedicated E2E coverage, not just unit tests,
given what they control.

## 26. Integration points with existing backend

| Page | Backend component |
|---|---|
| Experiments | `ExperimentTracker` |
| Risk | `RiskEngine`, `KillSwitch`, `ArmingService` |
| AI Assistant | `ChatQueryService`, `ExplanationCache` |
| Orders/Positions | `OrderManager`, `Portfolio` |
| Live Market | market data WebSocket bridge (execution engine) |
| Settings — Credentials | `CredentialVault`, `PermissionValidator` |

The API layer's job is exposure, not reimplementation, everywhere in
this table.

## 27. Database / API changes if needed

Mostly none — existing tables already carry what's needed. New: a
session-storage table (or a stateless JWT approach, see Open Decisions)
and a `notification_preferences` table.

## 28. Open design decisions

1. **Session storage**: server-side session table vs. stateless JWT — confirm before building auth middleware.
2. **E2E coverage beyond arm/disarm and kill switch**: worth adding for credential entry too, given its stakes — confirm.
3. **Hosting/deployment target**: affects CSP and CORS specifics concretely — confirm before finalizing security config.
4. **Control-surface build scope**: ship all of Strategy Management's live-enable, Risk's kill-switch control, and Orders' cancel action as one build step, or split further by page — my recommendation is one step, reviewed together, since they share the same confirmation-dialog primitive and gate logic.
