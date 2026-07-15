# Gap audit report

Audit-only deliverable. Nothing in the codebase was changed to produce
this report. Every item below was extracted from a spec's "Open
Decisions" / "Non-goals" / "Deferred" / "Out of scope" language (or,
where a spec has no such labeled section, from prose stating something
is deferred/excluded), then cross-checked directly against the current
code — not against docstring claims taken at face value.

**Categories**

- **(a)** Still correctly unbuilt, and clearly documented as such
  somewhere findable (a module docstring, CLAUDE.md, or both).
- **(b)** Silently missing — genuinely unbuilt/unresolved, with **no**
  documentation anywhere flagging it. The dangerous category: a reader
  would wrongly assume it's handled.
- **(c)** Actually built, but CLAUDE.md doesn't reflect it.

Audited by four parallel research passes, one per spec group:
ingestion + risk engine; execution stages 1–2; execution stage 3 + AI
assistant; dashboard UI. This document merges their findings.

---

## Executive summary

**The single biggest finding is structural, not a missing feature:
`CLAUDE.md` has zero dashboard-related content of any kind.** Every
dashboard build session (Steps 1–13, covering the entire `api/` and
`frontend/` trees) happened without a single corresponding update to
`CLAUDE.md`. At least 9 route/schema docstrings across the dashboard
codebase explicitly point readers to "CLAUDE.md's known-limitations
section" for the full gap list — that section does not exist under
that name or any equivalent name anywhere in the file. This was
already in progress as "Step 14: Update CLAUDE.md" when this audit was
requested instead; it subsumes most of the dashboard's individual (c)
findings below.

**Real (b) silent gaps found (7 total), ranked by how surprising they'd be to a reader:**

1. **Notification email/webhook dispatch doesn't exist.** An operator can toggle "email enabled," enter an address, and save — nothing is ever sent. No sender code exists anywhere. The one docstring describing this is worded in a way that implies the capability exists "elsewhere."
2. **`RiskContext.data_quality_ok` is hardcoded `True`** in the only production call site (`core/backtest_engine.py`). `DataQualityService`'s findings never reach `RiskEngine` — the `DATA_QUALITY_FAILED` rejection path is dead code in practice, and nothing says so.
3. **`DataQualityService` silently implements 5 of the 7 checks the spec lists**, and its own module docstring *claims* the missing two ("cross-timeframe reconciliation," gap cross-referencing) are implemented when they are not.
4. **`GapDetected` event is defined but never published.** `GapDetectionService` doesn't even accept an `event_bus` parameter.
5. **The frontend ships with no Content-Security-Policy.** The one comment addressing this is stale (written before the frontend existed, in future tense) and open decision #3 (hosting/deployment target) was never revisited or closed.
6. **Strategy Management (spec section 13) is an entire missing page** with no trace in `CLAUDE.md` and no trace in the frontend nav — only one backend docstring (in an unrelated file) mentions it.
7. **Playwright E2E tests exist and are runnable, but nothing documents they've actually been run and passed** — no CI, an empty `test-results/` directory, fully manual setup required.

**One item is technically categorized (a) — documented — but severe enough to flag for priority attention regardless:** `OrderManager._apply_fill_to_account()` unconditionally writes every fill's cash delta to `paper_accounts.current_cash`, **including real live-mode fills** — live trading P&L is currently written into the paper ledger. This is flagged in `CLAUDE.md` twice, so it doesn't meet the bar for (b), but it's the most consequential single gap this audit found.

**No spec item was found secretly fully built without CLAUDE.md knowing** (i.e., no "false (a)" miscategorizations) outside of the dashboard's wholesale CLAUDE.md gap described above.

---

## 1. `docs/historical_data_ingestion_spec.md`

### Section 8 "Non-goals for this phase" — all correctly honored

| Item | Category | Evidence |
|---|---|---|
| Real-time websocket/tick-level ingestion | (a) | `core/ingestion/` is REST-only. A *different* component, `core/marketdata/` (Stage 1 execution's paper market data), does have a websocket client — that's a separate spec/component boundary, not a violation. |
| Order book data | (a) | No order-book fetch/storage code anywhere in `core/ingestion/` or `schema.sql`. |
| Multi-region storage/replication | (a) | No replication config anywhere. |
| Full job orchestrator (Airflow/Prefect/Dagster) | (a) | `core/ingestion/scheduler.py:5` — "Deliberately an in-process loop, not Airflow/Prefect"; `CLAUDE.md:317`. |

### Additional gaps found during cross-check (spec §4.9 / §4.11 vs. actual code)

#### `GapDetected` event defined but never published — **(b)**
- Spec §4.9 (line 237) lists `GapDetected` among the events this component publishes.
- `core/ingestion/events.py:30-36` defines the dataclass, but `grep -rn "GapDetected(" core/ingestion tests/ingestion` returns zero instantiations anywhere outside the class definition.
- `core/ingestion/gap_detection_service.py.__init__(self, db: Session)` doesn't even accept an `event_bus` parameter — structurally can't publish it.
- Contrast: the other four listed events (`CandlesIngested`, `GapRepaired`, `BackfillCompleted`, `DataQualityIssueFound`) are all genuinely published, from `incremental_update_service.py:145`, `gap_repair_service.py:115`, `backfill_service.py:185`, `data_quality_service.py:101` respectively.
- Not flagged in the service's own docstring, not in CLAUDE.md, not in its test file.

#### `DataQualityService` implements 5 of 7 spec'd checks, and its own docstring claims otherwise — **(b)**
- Spec §4.11 (lines 255–268) lists 7 checks, including "missing candles (cross-reference with `GapDetectionService`'s findings)" and "timeframe consistency (summing 1m candles for an hour should reconcile with the stored 1h candle, within tolerance)."
- `core/ingestion/data_quality_service.py`'s `run()` (lines 75–110) only calls `_check_duplicates`, `_check_ohlc_validity`, `_check_timestamp_alignment`, `_check_volume_anomalies`, and (conditionally) `_check_cross_exchange` — 5 checks. No cross-timeframe reconciliation method exists; no cross-reference to `GapDetectionService`'s output exists.
- **The module's own docstring (lines 1–7) actively claims otherwise**: it lists "cross-timeframe reconciliation" as one of the things this service checks. `CLAUDE.md:313-315` repeats the same (correctly-truncated-to-5, but silently-truncated) list without flagging the 2 dropped items.
- `GapDetectionService` and `DataQualityService` are both wired independently into `core/ingestion/scheduler.py` (lines 41, 126) but never call each other.

---

## 2. `docs/risk_engine_spec.md`

No labeled "Non-goals" section; deferred items are in the Locked-in Decisions table (§1) and in `risk_engine.py`'s own "design notes — gaps in the spec, filled in and flagged here" docstring, which exists specifically to catalog this class of gap.

### Correctly deferred and documented — (a)

| Item | Evidence |
|---|---|
| Correlation Phase B (cross-symbol correlation) — Phase A (same-symbol only) ships now | `core/risk/exposure_tracker.py:1-15` |
| KillSwitch "N circuit breaker trips" auto-engage trigger — no N/window given by spec | `core/risk/risk_engine.py:28-34`; `CLAUDE.md:358-362` |
| Circuit breakers are single-dimensional (all read `atr_percentile_90`) | `core/risk/risk_engine.py:21-26`; `CLAUDE.md:358-360` |
| "Hard per-trade cap" reuses `max_same_symbol_directional_exposure_pct` — no dedicated field exists | `core/risk/risk_engine.py:36-42`; `CLAUDE.md:362-364` |

### Documented in code only, not in CLAUDE.md — still (a), but worth tightening

- **Kill-switch auto-flatten flag is stored but inert.** `schema.sql:426`, `core/risk/risk_config.py`, `api/schemas/risk.py` all carry `auto_flatten_positions`/`kill_switch_auto_flatten`, but nothing in `core/risk/kill_switch.py` or `risk_engine.py` ever reads or acts on it. `kill_switch.py:11-16` explicitly documents this ("a separate, opt-in RiskConfig flag this class doesn't implement"). Zero mentions in `CLAUDE.md`.
- **`FractionalKellySizer`'s `PerformanceStore` dependency has no concrete implementation anywhere in the repo.** `core/risk/position_sizing_strategies.py:23-38` flags this explicitly; `core/confidence_engine.py:28-36` confirms `performance_store` is only ever an untyped constructor param. `grep -rn "class.*PerformanceStore"` across the whole repo returns nothing but the `Protocol` definition. The sizer is real, tested, and correct — but cannot function in a real run today, since `RiskConfig.sizing_method` has no factory wiring it in either. `CLAUDE.md:347-350` mentions `FractionalKellySizer` as built without this caveat — reads as more usable than it currently is.

### Additional gap found during cross-check — **(b)**

#### `RiskContext.data_quality_ok` hardcoded `True` in the only production call site
- `core/backtest_engine.py:234-242` (`_build_risk_context()`):
  ```python
  return RiskContext(
      ...
      data_quality_ok=True,
      data_quality_reason=None,
      ...
  )
  ```
- `RiskEngine._evaluate_gate` (`risk_engine.py:231-237`) correctly *consumes* this field and would reject with `RejectionReason.DATA_QUALITY_FAILED` if it were ever `False` — but nothing anywhere ever sets it to `False`. `DataQualityService`'s findings (from the ingestion component) never reach `RiskContext`. This is a dead branch in practice.
- Not flagged in `risk_engine.py`'s own "design notes" docstring (which exists precisely to catalog this class of gap), not in `CLAUDE.md` (`grep -n -i "data_quality_ok"` → 0 hits). A reader would reasonably assume, from the presence of the enum value and the field, that these two already-built components are connected. They aren't.

---

## 3. `docs/execution_engine_stage1_spec.md`

Most non-goals here (real exchange order placement, live key custody) were later superseded by Stage 2/3, which were built — CLAUDE.md correctly narrates this transition, not a gap.

### Correctly deferred and documented — (a)

| Item | Evidence |
|---|---|
| `LatencySimulator` is not a queueing-theoretic model (deliberate) | `core/execution/latency_simulator.py` — self-documented, unchanged |
| External/manual trade detection (order on exchange with no local `client_order_id`) | Deferred to Stage 3 explicitly; still absent — see Stage 2 finding below |

### `is_trading_permitted()` not called from any production order-submission path — (a), documented, but worth flagging prominently

- `core/security/arming_service.py:210` defines the combined `KillSwitch` + `ArmingService` gate. `grep` confirms it is called **only** from `tests/test_security/test_arming_service.py` — zero production call sites in `core/execution/order_manager.py`, `core/risk/*`, or `core/execution/binance_execution_adapter.py`.
- In fact, `OrderManager.submit()` itself has **no production caller anywhere** yet — grep shows it's only ever invoked from tests. No strategy runner/live loop wires it in yet, and `api/routes/orders.py` exposes no order-submission endpoint.
- Explicitly documented: `CLAUDE.md:707-711` and `:585, 609`. This is the most-repeated documented gap across the whole codebase — it surfaced independently in three of the four audit passes (Stage 1, Stage 3, and dashboard), each confirming zero production wiring.

---

## 4. `docs/execution_engine_stage2_spec.md`

### Section 11 "Open decisions" — resolved or correctly deferred

| Item | Category | Evidence |
|---|---|---|
| External/manual trade detection deferred to Stage 3 | (a) | `core/execution/reconciliation_job.py:9-12` docstring; `reconciliation_job.py:104-113` only ever queries locally-tracked orders |
| Reconciliation polling interval = 60s, configurable | not a gap | `reconciliation_job.py:79`; built exactly as decided |
| Testnet data staleness = correctness check only, not a perf benchmark | not a gap | Testing-philosophy decision, honored |
| Mainnet out of scope until Stage 3 | (a) | `MainnetGate` still structurally blocks mainnet even with Stage 3 built |

### Additional gaps found during cross-check — all (a), documented, but two worth escalating

- **`SymbolFilterCache` doesn't model `PERCENT_PRICE_BY_SIDE`.** Confirmed absent from the dataclass and parsing logic. Documented: `core/execution/binance_execution_adapter.py:50-53`; `CLAUDE.md:509-513, 712-716`.
- **`STOP`/`STOP_LIMIT`/`OCO` order types raise `FatalIngestionError` rather than being implemented** for live Binance submission. Documented: `binance_execution_adapter.py:50-53, 490`; `CLAUDE.md:502-504`.
- **`symbol_filters_cache` table exists in `schema.sql` but is never written** — `SymbolFilterCache` keeps its cache purely in memory. Documented in `CLAUDE.md:498-501` and the adapter docstring, but **`schema.sql` itself carries no comment flagging this** — a reader going DB-first would have no way to know the table is dead.
- **`OrderManager._apply_fill_to_account()` unconditionally applies fill cash deltas to `paper_accounts.current_cash`, including for real `mode='live'` fills.** This was unreachable in Stage 1 (`LiveExecutionAdapter` always raised `NotImplementedError`) and became live-reachable once Stage 2's `BinanceExecutionAdapter`/`ReconciliationJob`/`BinanceOrderStreamConsumer` started calling `handle_fill()` for genuine live fills — meaning **live trading P&L is currently written into the paper account ledger.** Documented: `CLAUDE.md:514-522, 717-718` ("Known gap, not fixed here... needs a real live-account-balance model"). Technically (a) since it's flagged twice in CLAUDE.md, but this is the single highest-severity finding in this entire audit and merits attention regardless of its documentation status.

---

## 5. `docs/execution_engine_stage3_spec.md`

Unusually clean — every deferred item traced back to code is explicitly flagged in both a module docstring and CLAUDE.md. **Zero (b) or (c) items found.**

| Item | Category | Evidence |
|---|---|---|
| Real cloud `KMSClient` (AWS/Vault) | (a) | `core/security/kms_client.py:98-115` — `AWSKMSClient` every method raises `NotImplementedError`; `CLAUDE.md:99-110, 703-706` |
| Soak period before real `mainnet=True` use | (a) | Explicit process commitment, not a code deliverable; `CLAUDE.md:186-190, 725-727` |
| `is_trading_permitted()` dual-gate not wired into a real order path | (a) | Same finding as Stage 1/dashboard sections above; `CLAUDE.md:707-711` |

Confirmed built-and-correctly-documented, not gaps: 48h arming expiry, 90-day rotation cadence, `MainnetGate`'s `isinstance()` structural check, `EmergencyCredentialRevocation`.

---

## 6. `docs/ai_assistant_spec.md`

Also clean — **zero (b) or (c) items found.**

| Item | Category | Evidence |
|---|---|---|
| Real-API (non-pytest) `LLMClient` integration tests (spec §5) | (a) | No such script exists anywhere in the repo (checked `scripts/` and repo-wide for `ANTHROPIC_API_KEY` usage outside the two pytest files); `CLAUDE.md:722-724` |
| `ChatQueryService` "at most one tool-call round trip" (no multi-turn loop) | (a) | `core/ai_assistant/chat_query_service.py:6-22, 50-68` — still accurate today, unchanged since built; `CLAUDE.md:683-686` |
| `DailySummaryContext.equity_start`/`equity_end` — no `account_snapshots` writer | (a) | `core/ai_assistant/context_builder.py:30-43, 331-345` raises `LookupError` rather than fabricating; `CLAUDE.md:677-682, 719-721` |

Confirmed fully built and correctly documented: `llm_readonly` role with zero write grants (verified at the DB level by a real test), prompt-injection resistance test, tool-registry account-id stripping.

---

## 7. `docs/dashboard_ui_spec.md`

### 0. Meta-finding: CLAUDE.md has no dashboard section at all — **(c)**

`grep -n -i "dashboard"` and `grep -n -i "known.limitation"` against `CLAUDE.md` (747 lines) both return **zero matches**. Yet at least 9 separate docstrings across `api/routes/positions.py`, `api/routes/dashboard.py`, `api/routes/market.py`, `api/routes/orders.py`, `api/routes/settings.py`, `api/routes/risk.py`, and `frontend/src/components/layout/ModeBanner.tsx` explicitly cite "CLAUDE.md's known-limitations section" as the place a reader should go for the full picture. It doesn't exist. This was mid-flight as "Step 14: Update CLAUDE.md" when this audit was requested instead — it is the single largest documentation debt this audit found, and it subsumes most of the individual dashboard (c) items below (session-storage decision, credential-entry E2E addition, add-credential endpoint, control-surface scope decision — all real, all correctly decided/built, all absent from CLAUDE.md).

### Section 28 "Open design decisions"

| # | Decision | Category | Evidence |
|---|---|---|---|
| 1 | Session storage: server-side table (not JWT) | (a) resolved | `api/auth/session_store.py:1-4` — decided and documented in code; absent from CLAUDE.md |
| 2 | E2E scope extended to credential entry | (a) resolved | `frontend/e2e/credential-entry.spec.ts` exists; no explicit "we decided yes" narration anywhere, just the file's existence |
| 3 | Hosting/deployment target (affects CSP/CORS) | **(b) — never resolved, not flagged as still-open** | See below |
| 4 | Control-surface scope: one step | (a) resolved | `api/routes/risk.py:29-31`, `settings.py:6-9`, `orders.py:1-4` — cross-referenced across three route modules |

#### Open decision #3 detail — frontend ships with no CSP, and nobody says so
- `api/security_headers.py:3-5`'s docstring says the frontend "should set an equally strict CSP itself once built" — written in future tense, before the frontend existed.
- The frontend **is** now built (full page tree, e2e tests) but `frontend/index.html` has no CSP `<meta>` tag, and `grep` for "CSP"/"content-security-policy" across the entire `frontend/src` tree and `vite.config.ts` returns zero matches.
- The stale docstring is now inaccurate, and nothing anywhere re-raises open decision #3 as still-outstanding. This is a real, currently-true security gap with no active documentation pointing at it.

### Scan of sections 1–27 for deferred/gap language

| Item | Category | Evidence |
|---|---|---|
| Mode banner (paper/testnet/mainnet) | (a) honest stub | `api/routes/dashboard.py:11-22,105`; `ModeBanner.tsx:6-35` — deliberately fails loudly rather than fabricating a value |
| Open position count / Positions page | (a) honest stub | `api/routes/positions.py:1-22,43`; frontend renders `UnavailableNotice`, no fabricated table |
| **Strategy Management page (spec §13)** | **(b) — real gap, essentially untraced** | See below |
| Credential last-4-character masking (spec §17) | (a) documented | `api/routes/settings.py:24-37`; no fingerprint column exists in schema |
| Live-order cancellation | (a) paper-only, documented | `api/routes/orders.py:1-24,109-117` — 400 for live orders, explicit reasoning |
| Real WS market-data ticker bridge | (a) documented | `api/routes/market.py:1-35` — only historical REST candles built; `LiveMarketDataSource` is a Stage-1 fake feed with no subscribe hook |
| Regime badge overlay (Live Market page) | (a) documented | `frontend/src/pages/LiveMarketPage.tsx:11-13`; no `regime_state` table exists anywhere |
| `is_trading_permitted()` not called by dashboard's own arm/kill-switch endpoints | (a) — not a bug | Architecturally correct: those endpoints ARE the authorization boundary for the mutations themselves; the real gap (not wired into order *submission*) is the Stage 1/3 finding above, already documented |
| Add new credential (spec §17) | **(c)** | Fully built: `api/routes/settings.py:103-139`, `frontend/e2e/credential-entry.spec.ts` — absent from CLAUDE.md (part of the meta-finding) |
| Risk config editing | (a) still deferred per spec | No PUT/PATCH endpoint exists; spec itself frames this as "gated to a later build step" |
| **Notification email/webhook dispatch** | **(b) — silently missing, misleadingly worded** | See below |
| Playwright E2E "passing" status | **(b) — present but unverified** | See below |

#### Strategy Management page — essentially a real, untraced gap
- No `StrategyRegistry` construction anywhere in `api/` (grep confirms the only hit is a comment in `api/routes/risk.py`).
- No `StrategiesPage.tsx` exists in `frontend/src/pages/`. No "Strategies" entry in `frontend/src/components/layout/Nav.tsx` — the nav list silently omits it with zero comment.
- The only trace anywhere is `api/routes/risk.py:10-15`, a backend docstring in a file that isn't even the missing surface. Not in CLAUDE.md, not in the frontend. A reader auditing the frontend or CLAUDE.md alone would find no acknowledgment this page doesn't exist.

#### Notification email/webhook dispatch — toggles that do nothing
- `api/routes/notifications.py:1-9`'s docstring: "Optional external channels (email/webhook) are Step 9's `notification_preferences` settings, not built here — this endpoint is the in-app feed only." This phrasing implies the capability exists *somewhere*.
- It does not. `core/notifications/preferences_store.py` only stores toggle/address values — `email_enabled`, `webhook_url`, etc. Repo-wide search for `smtplib`, `requests.post`, `httpx.post`, `EmailSender`, `WebhookSender`, `send_notification` in `core/` or `api/` returns nothing.
- Net effect: an operator enables email notifications, enters an address, saves successfully — and nothing is ever sent, silently, forever. No docstring, CLAUDE.md entry, or frontend comment states this plainly.

#### Playwright E2E — real but unverified as "passing"
- `frontend/e2e/` contains real spec files (`arming.spec.ts`, `kill-switch.spec.ts`, `credential-entry.spec.ts`) and they were in fact run and passed during this session's Step 13 (twice, for stability) — but no CI workflow exists (`.github/` is entirely absent from the repo) and `frontend/test-results/` is empty, so nothing in the *repository itself* substantiates a "passing" claim independent of this conversation's transcript. A future reader with only the repo, not this conversation, would have no artifact proving the suite currently passes.

---

## Full summary table

| Spec | Item | Category |
|---|---|---|
| Ingestion | Real-time websocket ingestion, order book data, multi-region storage, job orchestrator (4 non-goals) | (a) × 4 |
| Ingestion | `GapDetected` event defined, never published | **(b)** |
| Ingestion | `DataQualityService` missing 2/7 spec'd checks; docstring claims otherwise | **(b)** |
| Risk | Correlation Phase B, KillSwitch N-trips trigger, single-dimension circuit breakers, reused hard-cap field | (a) × 4 |
| Risk | Auto-flatten flag stored, never acted on | (a), CLAUDE.md silent |
| Risk | `FractionalKellySizer`'s `PerformanceStore` has no implementation | (a), CLAUDE.md silent |
| Risk | `RiskContext.data_quality_ok` hardcoded `True`; DataQualityService never wired in | **(b)** |
| Stage 1 | `LatencySimulator` non-queueing-theoretic; external-trade detection deferred | (a) × 2 |
| Stage 1 | `is_trading_permitted()` — zero production callers anywhere | (a), high-visibility repeated finding |
| Stage 2 | External-trade detection, mainnet gating, reconciliation cadence | (a) / not-a-gap |
| Stage 2 | `SymbolFilterCache` no `PERCENT_PRICE_BY_SIDE`; STOP/STOP_LIMIT/OCO unimplemented | (a) × 2 |
| Stage 2 | `symbol_filters_cache` table unpopulated | (a), not flagged in `schema.sql` itself |
| Stage 2 | **`OrderManager` mixes live fills into paper ledger** | (a), but highest-severity finding overall |
| Stage 3 | Real cloud KMS, soak period, `is_trading_permitted()` wiring | (a) × 3, zero (b)/(c) |
| AI Assistant | Real-API LLM tests, single tool round-trip, no `account_snapshots` writer | (a) × 3, zero (b)/(c) |
| Dashboard | **CLAUDE.md has no dashboard section at all** | **(c)**, largest finding |
| Dashboard | Session storage, control-surface scope, add-credential — all resolved/built | (a)/(c), undocumented in CLAUDE.md only |
| Dashboard | Hosting/deployment decision unresolved; frontend has no CSP | **(b)** |
| Dashboard | Mode banner, positions, credential masking, live-cancel, WS ticker, regime badge | (a) × 6, honest stubs |
| Dashboard | **Strategy Management page — entirely missing, untraced** | **(b)** |
| Dashboard | **Email/webhook notifications — silently non-functional** | **(b)** |
| Dashboard | E2E tests present but no CI/artifact proving they pass | **(b)**, minor |

**Totals: 7 category-(b) items, 1 major category-(c) finding (CLAUDE.md's missing dashboard section, which subsumes several smaller (c) items), one severity-flagged (a) item worth priority attention (live/paper ledger mixing).**
