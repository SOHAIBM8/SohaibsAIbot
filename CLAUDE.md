# Trading platform — project context

## What this is
A non-custodial, multi-user crypto trading research platform, built as a
quantitative research platform first, trading bot second. Built
incrementally across a design conversation in Claude.ai; this file exists
so Claude Code can continue that work without needing that conversation.

## Project rules (non-negotiable, apply to every change)
1. Every module ships with unit tests.
2. Follow SOLID principles and clean architecture — no shortcuts that
   couple unrelated modules.
3. Modular, reusable, well-documented code.
4. Every major design decision gets a brief written explanation and
   trade-offs (in code comments/docstrings, and summarized in chat).
5. Type hints, docstrings, and structured logging (via `structlog`,
   see `core/logging_config.py`) throughout — no bare `print()`.
6. Consistent formatting/linting: `black`, `ruff`, `mypy` (config in
   `pyproject.toml`).
7. New features are not "done" until they have passing tests.
8. Avoid technical debt and premature optimization — build the simple
   correct version first, optimize only after profiling shows a need.
9. If a better architectural decision becomes apparent mid-implementation,
   explain it (in code comments and to the user) before changing the
   design — don't silently deviate from what was agreed.
10. Keep the project production-ready at every stage, not just at the end.

## Key architectural decisions already made (don't re-litigate without reason)
- **Non-custodial only.** Users bring their own exchange API keys; the
  platform never holds funds. This was a deliberate regulatory choice.
- **Strategies are pure functions.** `StrategyBase.generate_signal()` must
  have zero I/O, zero hidden state, zero wall-clock reads — this is what
  makes backtest and live execution trustworthy.
- **Confidence is NOT computed by strategies.** `Signal` has no confidence
  field. `core/confidence_engine.py` computes it downstream from historical
  performance, regime context, and sample size — separation of concerns is
  intentional, don't collapse it back into strategy code.
- **Trend and volatility are independent regime axes**, not one combined
  label (`core/regime_detector.py`, `Regime` + `VolRegime` enums in
  `core/strategy_base.py`).
- **Regime detection is stateful and must be called in chronological
  order**, with `reset()` between backtest runs/symbols — unlike
  strategies, which must be pure.
- **Raw vs. derived data split.** Raw market data is immutable/append-only.
  Every indicator is a versioned function in `core/indicators/`, wrapped
  behind `FeatureRegistry` — nothing else in the codebase imports
  `pandas_ta` directly except `core/indicators/pandas_ta_adapter.py`.
- **Postgres from day one** for metadata/experiments/strategy versions
  (`schema.sql`). Parquet for historical OHLCV/feature data at backtest
  scale. No SQLite, no Supabase.
- **TimescaleDB enabled on `raw_ohlcv` from day one**, not deferred —
  see `docs/historical_data_ingestion_spec.md` decision #1. Hypertable
  partitioned on `open_time`, 7-day chunks, compression after 30 days,
  a `raw_ohlcv_daily` continuous aggregate. `docker-compose.yml` runs
  `timescale/timescaledb:latest-pg16`, not plain `postgres:16`.
- **Ingestion event bus is Postgres LISTEN/NOTIFY behind an `EventBus`
  interface** (`core/ingestion/event_bus.py`), swappable for Kafka/
  Redis Streams/NATS later without touching publishers or subscribers.
- **Every ingestion service is idempotent and every run is logged.**
  Closed candles are immutable (`ON CONFLICT DO NOTHING`, never
  overwritten); every backfill/incremental/gap-repair/data-quality
  invocation writes exactly one `ingestion_run_log` row, including
  no-op runs — nothing is fire-and-forget.
- **Signals execute at the NEXT bar's open, never the signal bar's own
  close.** A strategy decides using bar T's completed data, but can only
  act starting at bar T+1 — filling at bar T's close assumes zero-latency
  execution. `core/backtest_engine.py` queues entries and fills them one
  bar later. Don't "simplify" this back to same-bar fills.
- **Risk engine has final authority** over position size and stops — a
  strategy's `entry_price`/`stop_loss`/`take_profit` are proposals, not
  orders. `PositionSizer.size()` takes a `RiskContext` (equity, feature
  window, regime state, a read-only `PortfolioView`, data-quality
  status, timestamp) and returns a `SizingDecision` (approved quantity
  + full per-layer audit trail), not a bare float — a breaking change
  from the original stub, made deliberately (see
  `docs/risk_engine_spec.md` section 2).
- **Kill switch state is persisted to Postgres**, never held only in
  memory — a process restart must never silently clear an emergency
  stop. It never auto-clears; `engage()`/`disengage()` are both
  explicit. Circuit breakers are in-memory per process (auto-recovering
  by nature) but every trip/clear transition is still logged for audit.
- **Correlation management ships in two phases.** Phase A (built) tracks
  net directional exposure across strategies on the *same* symbol —
  real pairwise correlation across *different* symbols (Phase B) waits
  for multi-symbol execution to actually exist.
- **Binance for development**, but the exchange abstraction must support
  Kraken/Coinbase early — Binance.com is unavailable to US users, and this
  is headed toward a multi-user SaaS.
- **Live Execution & Paper Trading ships in three stages; all three are
  now built, but Stage 3 being built is NOT the same thing as being
  safe to use with real funds — see the explicit caveat at the end of
  this section.** `OrderManager` and the order state machine are
  IDENTICAL for paper and live — only `ExecutionAdapter` differs;
  Stage 2's `BinanceExecutionAdapter` required ZERO changes to
  `OrderManager`, confirming the Stage 1 interface really was
  adapter-agnostic. Every order, paper or live, must originate from an
  approved `SizingDecision` — no code path places an order without Risk
  Engine approval.
- **Envelope encryption for exchange credentials, KEK never
  co-located with the ciphertext (Stage 3 decision #1).**
  `core/security/credential_vault.py`'s `CredentialVault` generates a
  fresh per-credential DEK, encrypts the API key/secret with it, and
  wraps the DEK under a KEK held by a separate `KMSClient`. Only
  ciphertext is ever persisted (`encrypted_credentials` table).
  `LocalDevKMSClient` (testnet-only, a real local-key stand-in, never a
  real cloud KMS) is the only functional `KMSClient` implementation
  built so far — `AWSKMSClient` is an explicit `NotImplementedError`
  stub, confirmed with the user, since this project has no cloud
  infrastructure configured yet and building against untested
  credentials would be scope creep, not a real capability.
- **Mainnet credentials structurally cannot use the dev KMS path — a
  raise, not a warning (Stage 3 decision #6).** `MainnetGate.check()`
  does an `isinstance()` check against the concrete `LocalDevKMSClient`
  class (not a spoofable string/flag) and is wired into
  `CredentialVault.encrypt()` itself — the lowest possible layer — so
  even a caller that forgot to gate a mainnet request is still refused
  at the point of key generation. This was the first thing built to
  touch a `mainnet` flag anywhere in the system, before anything else
  was allowed to.
- **Every credential decrypt is audited BEFORE the caller ever sees the
  plaintext, via an INSERT-only Postgres role (Stage 3 decision #4).**
  `CredentialProvider.get_credentials()` writes to `credential_audit_log`
  through the dedicated `credential_audit_writer` role
  (`core/security/audit_db.py`), which has no `UPDATE`/`DELETE` grant —
  proven by `test_audit_log_immutability.py` connecting to Postgres
  *as* that role. **Dev-environment caveat, not glossed over**: this
  project's bootstrap app role (`trading`) is a Postgres superuser in
  local docker-compose, and superusers bypass every ACL unconditionally
  — so "the default app role can't write either" is only fully true in
  a real deployment where the app's connection role isn't a superuser
  (which any real production Postgres setup should be anyway). The
  `credential_audit_writer` boundary itself is real and fully enforced
  regardless.
- **No plaintext credential value may appear in a log line, at any
  level, anywhere — treated as absolute (Stage 3 decision #8), not
  best-effort.** `test_no_plaintext_in_logs.py` captures the ENTIRE
  structlog output of the full decrypt-and-audit path (via
  `structlog.testing.capture_logs()`, which isn't fooled by a log-level
  filter hiding something at DEBUG) and asserts a known sentinel
  plaintext value never appears anywhere in it, including on the error
  path.
- **`EmergencyCredentialRevocation` is a distinct, more severe gate
  than `KillSwitch` — its own table, not a reuse of the credential
  lifecycle state machine's terminal `REVOKED` value (Stage 3 decision
  #5).** Mirrors `KillSwitch`'s "never auto-clears" posture:
  `re_grant()` is always explicit and logged, never automatic or
  time-based. Because nothing in this codebase caches decrypted
  plaintext beyond a single call (a deliberate Stage 3 step 4 design),
  "invalidates cached decrypted material" is achieved by refusing every
  FUTURE decrypt rather than clearing a cache that doesn't exist —
  documented as an interpretive choice, not a silent reinterpretation.
- **Arming is a second, independent gate alongside `KillSwitch`, never
  merged into it (Stage 3 decision #3).** `ArmingService` is scoped per
  (account, strategy, exchange), expires after 48 hours (confirmed with
  the user), and reverts to unarmed on any config change, requiring
  fresh re-confirmation. `is_armed()` computes expiry at READ time — no
  sweep job needed for correctness. `is_trading_permitted()`
  (`core/security/arming_service.py`) is the one function that combines
  both gates; neither `KillSwitch` nor `ArmingService` imports or knows
  about the other.
- **`BinanceExecutionAdapter` fetches credentials fresh on every
  `submit_order()`/`cancel_order()`/`get_order_status()` call, never
  caching them for the adapter's lifetime — a design change confirmed
  explicitly with the user before implementing, since it touches the
  constructor and every public method, not a one-line swap.** A
  construction-time-only fetch (the more literal reading of decision #7)
  would let an already-running adapter keep signing requests with a
  stale credential straight through an `EmergencyCredentialRevocation`,
  silently defeating decision #5's guarantee.
  `test_binance_adapter_revocation_integration.py` proves this
  concretely: a real adapter instance that already placed one order
  successfully is stopped on its very next call the moment its
  credential is revoked — no reconstruction needed. None of the
  idempotency/retry/error-classification/state-machine logic changed;
  only where the credential value comes from at the moment of signing
  did (decision #7).
- **Stage 2 targets Binance TESTNET only; Stage 3's real KMS path is
  built but has no cloud provider wired in yet.** Mainnet credentials,
  a real (non-stub) `KMSClient`, and any live-trading enablement
  decision remain out of scope. Testnet credentials, once registered
  through `KeyLifecycleManager`, are encrypted at rest — never a plain
  environment-variable read at the adapter's call site anymore, though
  the value used to REGISTER a testnet credential still originates from
  `BINANCE_TESTNET_API_KEY`/`BINANCE_TESTNET_API_SECRET` env vars in
  tests and scripts, per decision #2 (never persisted unencrypted).
- **This build being complete and its tests passing is explicitly NOT
  the same thing as being safe to trade real funds.** Per the user's
  own instruction, stated here verbatim rather than paraphrased: a
  deliberate, separate testnet soak period under this full security
  path must run before any real `mainnet=True` credential is used —
  that decision belongs to the user, made separately, later, and is
  not something this codebase or this build decides.
- **An ambiguous order-submission failure (timeout, connection drop)
  is never assumed to be a failure.** `BinanceExecutionAdapter` queries
  the exchange for the existing `client_order_id` before ever retrying
  with a fresh submission — recovering the real outcome if the order
  actually went through, and only resubmitting once genuinely
  confirmed absent. A clean HTTP error response (429/503/etc.) is a
  different case entirely — Binance definitely never created that
  order, so it's just retried via the existing `RetryPolicy`, no
  existence check needed. See `core/execution/binance_execution_adapter.py`'s
  module docstring for the full reasoning.
- **The REST reconciliation poll is authoritative over the WebSocket
  stream whenever they disagree.** `BinanceOrderStreamConsumer` is a
  low-latency notifier only — it forwards `executionReport` fill
  events straight to `OrderManager.handle_fill()`, the same single
  fill-handling path paper trading uses, but it is never treated as
  the source of truth. `ReconciliationJob` polls exchange state on a
  fixed cadence (60s default) regardless of whether anything seemed
  wrong, and is the actual arbiter: a mismatch it can correct via a
  legal state transition is corrected and logged; a mismatch that
  would require an illegal transition (a genuine anomaly, not
  staleness) is surfaced and left for a human, never forced.
- **Binance's listenKey-based user data stream is deprecated — the
  Stage 2 build discovered this against real testnet, not from docs.**
  `POST /api/v3/userDataStream` (the original design for obtaining a
  WebSocket auth key) returned a confirmed `410 Gone` on real testnet;
  Binance deprecated it in April 2025 with full removal across all
  environments scheduled for 2026-02-20. The fix, confirmed against
  Binance's current WebSocket API docs and re-verified end-to-end
  against real testnet: `BinanceOrderStreamConsumer` now connects
  directly to the WebSocket API
  (`wss://ws-api.testnet.binance.vision/ws-api/v3`) and authenticates
  the stream itself by sending a signed `userDataStream.subscribe.signature`
  request as the first message — no separate key to obtain, renew, or
  expire. `core/marketdata/websocket_connection.py`'s `WebSocketConnection`
  gained a generic `on_open` hook (fires on every connection, including
  reconnects) to support this — a small, domain-agnostic extension to
  an already-complete, tested Stage 1 component, not Binance-specific.
  `ListenKeyManager` (the original Step 4 REST-based key manager) was
  removed outright rather than kept as unused dead code implementing an
  API flow that no longer exists.
- **The AI Analysis & Signal Explanation Engine has zero write access to
  any trading table, enforced at the Postgres role level, not just in
  application code.** `ContextBuilder` and every `ChatTool` connect via
  the dedicated `llm_readonly` Postgres role
  (`core/ai_assistant/readonly_db.py`), which has `SELECT`-only grants
  on `signal_log`, `risk_decision_log`, `orders`, `fills`, `experiments`,
  `paper_accounts`, `account_snapshots`, `news_articles` — no
  INSERT/UPDATE/DELETE grant exists for it on any table, now or in the
  future. This is proven, not just asserted: `test_readonly_role_enforcement.py`
  connects to Postgres *as* `llm_readonly` and confirms the database
  itself refuses every write, independent of what application code
  does or doesn't do. `ChatToolRegistry.execute_tool_call()` also
  strips any account/user identifier an LLM supplies and injects the
  real authenticated session's `account_id` instead — proven by
  `test_prompt_injection_resistance.py`. Explanation generation is
  on-demand or nightly-scheduled only, never triggered synchronously
  from a trading event.

## Build order (don't skip ahead)
Foundations → backtesting engine → execution layer → risk engine → SaaS
multi-tenancy → AI signal research. AI is deliberately last — infra and
risk discipline come first. Risk engine is built. Execution layer
Stage 1 (paper trading), Stage 2 (real Binance TESTNET order
placement), and Stage 3 (live trading security: encrypted credentials,
arming, audit) are all built. **Built and tested is not the same as
"cleared for real money"** — see the explicit soak-period caveat in
the architectural-decisions section above; that call is the user's to
make separately. The AI Analysis & Signal Explanation Engine (all 9
build-order steps) is built, strictly downstream/read-only of
everything above it — see the dedicated write-up below. SaaS
multi-tenancy is next, not yet started.

## What's built so far
- `core/strategy_base.py` — `Signal`, `StrategyMeta`, `StrategyBase`, `Regime`, `VolRegime`
- `core/strategy_registry.py` — plugin discovery, regime-based filtering
- `core/confidence_engine.py` — confidence scoring, separate from strategies
- `core/feature_store.py` — `FeatureRegistry`, `FeatureWindow`, dependency resolution
- `core/indicators/` — `pandas_ta_adapter.py` (library wrap), `derived.py` (hand-written), `register.py` (default registry)
- `core/regime_detector.py` + `core/regime_config.py` — rule-based trend/vol regime detection with hysteresis
- `core/execution_model.py` — fee + slippage simulation
- `core/position_sizing.py` — `PositionSizer` interface (`size(signal, RiskContext) ->
  SizingDecision`) + `FixedFractionSizer`, the deliberately naive baseline sizer
- `core/portfolio.py` — cash/position/trade tracking, long & short, plus
  `PositionView`/`PortfolioView`/`Portfolio.snapshot()` — the Risk Engine's only
  read-only window into portfolio state
- `core/backtest_engine.py` — event-driven loop, next-bar-open execution, warmup
  handling, multi-strategy; builds a `RiskContext` per queued entry and calls the
  widened `PositionSizer` interface (incidentally fixed a latent bug: sizing now
  uses portfolio *equity*, not raw cash)
- `core/metrics.py` — win rate, profit factor, Sharpe, Sortino, max drawdown, CAGR, expectancy, avg R multiple, exposure
- `core/walk_forward.py` — sequential window splitting and per-window evaluation
- `core/experiment.py` — `ExperimentTracker` (`start`/`finish`/`compare`) wired to real
  Postgres via `core/db.py`'s `SessionLocal`; `ComparisonTable` for side-by-side results.
  Tested against a live local Postgres (`docker compose up -d` + `schema.sql` applied),
  not mocks.
- `core/db.py`, `core/logging_config.py` — infra plumbing
- `strategies/ema_cross.py`, `strategies/rsi_mean_reversion.py` — reference strategies
- `schema.sql` — Postgres schema, now including the TimescaleDB `raw_ohlcv`
  hypertable and the ingestion tables (see below)
- **Historical data ingestion** (`core/ingestion/`,
  `docs/historical_data_ingestion_spec.md`) — Binance backfill,
  incremental updates, gap detection/repair, and nightly data quality
  auditing, all tested end-to-end against real Postgres and (via a
  manual smoke test) real Binance data:
  - `exchange_adapter.py` (`ExchangeAdapter` interface) +
    `binance_adapter.py` (`BinanceAdapter`) + `testing.py`
    (`FakeExchangeAdapter`/`AlwaysFatalAdapter` test doubles)
  - `rate_limiter.py`, `retry_policy.py` — per-exchange rate limiting,
    exponential backoff with jitter, retryable-vs-fatal error taxonomy
    (`errors.py`)
  - `candle_validator.py` — pure OHLC/alignment/closed-candle validation,
    shared by ingestion-time and after-the-fact (data quality) checks
  - `backfill_service.py`, `incremental_update_service.py` — idempotent
    by construction (`ON CONFLICT DO NOTHING`); a completed backfill or
    an up-to-date incremental run is a logged no-op, not a re-fetch
  - `gap_detection_service.py`, `gap_repair_service.py` — bounded gap
    repair (max 3 attempts, ≥24h apart, then `confirmed_absent` —
    terminal, never re-flagged)
  - `event_bus.py` (`EventBus` interface, `PostgresEventBus` via
    LISTEN/NOTIFY), `events.py`
  - `data_quality_service.py` — duplicates, invalid OHLC, timestamp
    alignment, volume anomalies, and a live cross-check against the
    exchange, each reported independently
  - `scheduler.py` — in-process sweep coordinating all of the above per
    tracked instrument (no Airflow/Prefect at this stage)
  - `observability.py` — `/health` and `/metrics` (Prometheus text
    format) HTTP endpoints; metric names are a stable interface
  - `config.py` + `config/ingestion.yaml` — backfill window, gap-repair
    attempts/spacing, per-timeframe polling cadence, all config not code
  - `scripts/smoke_test_ingestion.py` — manual, not part of pytest;
    runs the full pipeline against real Binance + real Postgres, cleans
    up after itself
- **Risk engine** (`core/risk/`, `docs/risk_engine_spec.md`) — replaces
  `FixedFractionSizer`-as-Risk-Engine-stand-in with real portfolio-level
  risk management, tested end-to-end against real Postgres:
  - `rejection_reason.py` — `RejectionReason` (11 values)/`ThrottleReason`
    (3 values) enums; every rejection carries an exact value, never free text
  - `risk_context.py`, `risk_decision.py` — `RiskContext` (input),
    `SizingDecision`/`LayerResult` (output)
  - `risk_config.py` + `config/risk_engine.yaml` — versioned risk
    parameters (`risk_config` table); `ExperimentConfig.risk_config_id`
    now versions risk params across experiments like strategy versions
  - `kill_switch.py` — `KillSwitch`, Postgres-persisted, restart-survival
    tested; never auto-clears
  - `circuit_breaker.py` — `CircuitBreaker`, asymmetric hysteresis
    (immediate trip, N-confirmed clear); pure in-memory by design, with a
    standalone `record_circuit_breaker_event()` for the caller to persist
    transitions (`circuit_breaker_event_log`)
  - `loss_limit_tracker.py` — UTC daily/weekly realized+unrealized PnL vs.
    limits, boundary-tested at the exact UTC midnight/Monday transition
  - `drawdown_monitor.py` — tiered response (0 normal / 1 throttle / 2 hard
    stop / 3 kill-switch-triggering) off running peak equity
  - `exposure_tracker.py` — Phase A same-symbol directional exposure
    (gross/net/concurrent-position/same-direction-concentration limits)
  - `position_sizing_strategies.py` — `PositionSizingStrategy` interface
    (internal to RiskEngine) + `VolatilityAdjustedSizer` +
    `FractionalKellySizer` (fractional Kelly, sample-size-gated,
    never guesses with thin data)
  - `risk_engine.py` — `RiskEngine(PositionSizer)`, the five-layer
    fail-fast pipeline (gate → budget → portfolio → sizing → decision),
    logs every decision to `risk_decision_log`, publishes events on the
    (now domain-agnostic) `EventBus` from the ingestion component
  - `events.py` — `RiskDecisionMade`, `CircuitBreakerTripped/Cleared`,
    `KillSwitchEngaged/Disengaged`, `DailyLossLimitBreached`,
    `DrawdownTierChanged`
  - Known, deliberate gaps (see `core/risk/risk_engine.py` module
    docstring for full rationale): circuit breakers all read
    `atr_percentile_90` (RiskConfig only configures one breaker
    dimension); the "N circuit breaker trips" kill-switch auto-trigger
    is unimplemented (spec gives no N/window); the "hard per-trade cap"
    reuses `max_same_symbol_directional_exposure_pct` (no dedicated
    config field exists for it)
- **Live Execution & Paper Trading — Stage 1 only** (`core/execution/`,
  `core/marketdata/`, `docs/execution_engine_stage1_spec.md`). Real
  exchange order placement (Stage 2) and live trading enablement/API
  key custody (Stage 3) are explicitly NOT built — see the decision
  above. What Stage 1 covers, tested end-to-end against real Postgres
  and a real local WebSocket server (not mocks):
  - `core/execution/order.py` — `OrderState`, `OrderType`, `Order`
    (`transition_to()` is the single choke point for every state
    change), `Fill`; an explicit legal-transition table, exhaustively
    tested over the full state × state cross product
  - `core/execution/execution_adapter.py` — `ExecutionAdapter` interface
    (`submit_order`/`cancel_order`/`get_order_status`/`get_fills`)
  - `core/execution/live_execution_adapter.py` — `LiveExecutionAdapter`,
    a Stage 2 stub; every method raises `NotImplementedError`
  - `core/execution/latency_simulator.py`, `fill_simulator.py` —
    configurable fixed+jittered latency; fills reuse the *existing*
    `ExecutionModel`, no second fee/slippage model
  - `core/execution/paper_execution_adapter.py` — `PaperExecutionAdapter`,
    Stage 1's concrete adapter; fills synchronously (no real order book
    yet), idempotent on `client_order_id`. Only transitions orders to
    `SUBMITTED` — `OrderManager.handle_fill()` owns every fill-driven
    transition, identically for paper and live
  - `core/execution/order_manager.py` — `OrderManager`: `submit()`
    (rejects any `SizingDecision` with `approved_quantity <= 0`, never
    silently no-ops), `handle_fill()`, `cancel()`; updates
    `paper_accounts.current_cash` per fill (no position tracking yet —
    Stage 1's schema has no `positions` table)
  - `core/execution/events.py` — `OrderSubmitted`, `OrderFilled`,
    `OrderRejected`, `OrderCancelled`, `PaperFillSimulated`
  - `core/marketdata/websocket_connection.py` — `WebSocketConnection`:
    exponential backoff + jitter reconnect, heartbeat-timeout-triggered
    reconnect, background-thread/synchronous-API shape (same pattern as
    `PostgresEventBus`)
  - `core/marketdata/market_data_normalizer.py` — `MarketDataNormalizer`/
    `NormalizedTick`; raises on any malformed field, never defaults
  - `core/marketdata/live_market_data_source.py` — `LiveMarketDataSource`,
    the first real `MarketDataSource` implementation; `PaperExecutionAdapter`
    needed zero changes to consume it, since it was already written
    against that Protocol
  - `core/risk/risk_decision.py` — `SizingDecision` gained
    `risk_decision_id`, and `RiskEngine._log_decision()` now captures it
    via `RETURNING id` — closes the gap flagged in the Risk Engine's
    step 2, needed because `Order.risk_decision_id` is a required FK
  - `core/ingestion/event_bus.py` — `EventBus.publish()` generalized to
    accept any `EventLike`-shaped object (a `Protocol`), not just
    `IngestionEvent` — the risk and execution components now publish on
    the same bus
  - Known, deliberate gaps (see `core/execution/order_manager.py` and
    `core/execution/paper_execution_adapter.py` module docstrings):
    `OrderManager`'s constructor takes `mode`/`account_id`/
    `starting_balance` beyond the spec's literal 3 params — nothing
    else could supply them; no `account_snapshots` writing logic yet
    (nothing in Stage 1's spec assigns that responsibility to any
    class)
- **Live Execution Stage 2 — Binance TESTNET only** (`core/execution/`,
  `docs/execution_engine_stage2_spec.md`). Mainnet credentials and any
  live-trading enablement decision remain out of scope (see the Stage 3
  entry below for what IS now built: key encryption/custody, arming,
  audit). Tested against fakes (no real network in the standard
  suite, matching every other exchange-facing component); a separate
  `testnet`-marked integration suite makes real testnet calls and is
  skipped unless `BINANCE_TESTNET_API_KEY`/`BINANCE_TESTNET_API_SECRET`
  are set — **run against real Binance testnet with real (free,
  no-value) credentials as part of this build: all 3 tests pass,
  confirmed stable across two independent runs** (a real market order
  filled end-to-end, a real limit order was cancelled cleanly, and a
  real WebSocket fill notification was independently confirmed by
  REST reconciliation):
  - `binance_clock_sync.py` — `ClockSyncService`, tracks offset vs.
    Binance server time, applied to every signed request's timestamp
  - `binance_symbol_filter_cache.py` — `SymbolFilterCache`, caches
    `GET /exchangeInfo`; every order pre-validated against
    `LOT_SIZE`/`PRICE_FILTER`/`MIN_NOTIONAL` before submission, failing
    closed (rejected locally) if no cached filters exist for a symbol
  - `binance_error_classifier.py` — `BinanceErrorClassifier`, maps
    Binance error codes to `retryable`/`category` ('network' |
    'rate_limit' | 'rejected' | 'fatal' | 'auth') — feeds the existing
    `RetryableIngestionError`/`FatalIngestionError` taxonomy rather
    than inventing a second one; 'rejected' becomes `Order` ->
    `REJECTED` (a normal outcome), never an exception
  - `binance_execution_adapter.py` — `BinanceExecutionAdapter`,
    replaces the Stage 1 `LiveExecutionAdapter` stub. Ambiguous
    submission failures (timeout/connection drop) query the exchange
    for the existing `client_order_id` BEFORE ever retrying — never
    assumes failure and resubmits blind (see the architectural-
    decisions bullet above). `get_order_status()` returns a detached
    SNAPSHOT of the exchange's view rather than mutating the shared
    cached `Order` — a design fix made mid-build (rule 9) after
    realizing an in-place mutation would collide with
    `OrderManager.handle_fill()`'s sole ownership of fill-driven
    transitions once `ReconciliationJob` (below) needed to call both
  - `binance_order_stream_consumer.py` — `BinanceOrderStreamConsumer`.
    **Rewritten mid-build** after discovering Binance's listenKey REST
    endpoint (`POST /api/v3/userDataStream`) returns `410 Gone` on real
    testnet (see the architectural-decisions bullet above for the full
    story) — it now connects directly to the WebSocket API and
    authenticates the stream itself with a signed
    `userDataStream.subscribe.signature` request sent via
    `WebSocketConnection`'s new `on_open` hook, re-sent on every
    reconnect. `ListenKeyManager` (the original REST-based key manager)
    was removed outright, not kept as dead code. Normalizes
    `executionReport` events (now arriving wrapped as
    `{"subscriptionId": N, "event": {...}}`) and forwards only actual
    `TRADE` events to `OrderManager.handle_fill()` — no new fill-
    handling path; malformed messages and fills for unknown orders are
    logged, never raised out of the callback
  - `reconciliation_job.py` — `ReconciliationJob`, adapter-agnostic
    (depends on `ExecutionAdapter`, not Binance directly), polls every
    locally-open LIVE order via `get_order_status()` on a configurable
    cadence (60s default), logs one `reconciliation_log` row per check
    (clean or not), and corrects mismatches: fill-driven corrections
    route through `OrderManager.handle_fill()` (the same shared path);
    exchange-confirmed cancel/reject corrections are applied directly
    (no `OrderManager` method fits "the exchange already resolved this
    a different way than we knew"); a mismatch requiring an ILLEGAL
    transition is published and logged but never forced — left for a
    human. Wired into the existing ingestion `Scheduler` via an
    optional, `TYPE_CHECKING`-gated `reconciliation_job` param — same
    additive pattern as `daily_summary_job`, zero new scheduling
    mechanism, zero hard runtime dependency added to ingestion
  - `events.py` gained `OrderAcknowledgedByExchange`,
    `ExchangeOrderMismatchDetected`, `ExchangeOrderCorrected`,
    `ExchangeErrorClassified`. `ListenKeyRenewed`/
    `ListenKeyExpiredReconnecting` were defined per the spec's original
    literal event list but are now unused — there is no listenKey
    lifecycle left to emit them for (see the auth-flow rewrite above);
    left defined rather than removed since nothing currently guarantees
    the spec's event list is otherwise exhaustive
  - `core/marketdata/websocket_connection.py`'s `WebSocketConnection`
    gained a generic `on_open` hook (fires once per connection,
    including reconnects) to support the signed-subscribe-on-connect
    auth flow — not Binance-specific, a small extension to an
    already-complete Stage 1 component
  - `schema.sql` gained `symbol_filters_cache` (defined per spec;
    `SymbolFilterCache` currently keeps its cache in memory only —
    nothing writes this table yet, flagged rather than silently wired
    up unused) and `reconciliation_log`
  - Known, deliberate scope limits: only `MARKET` and `LIMIT` order
    types are implemented — `STOP`/`STOP_LIMIT`/`OCO` raise
    `FatalIngestionError` rather than being mis-mapped, since their
    Binance parameter shapes are real additional scope; external/manual
    trade detection (an exchange order with no matching
    `client_order_id`) is explicitly deferred to Stage 3, confirmed
    with the user against the spec's own open decision #1;
    `SymbolFilterCache` doesn't model `PERCENT_PRICE_BY_SIDE`
    (price-deviation-from-market) — out of the spec's decision #5 scope
    (`LOT_SIZE`/`PRICE_FILTER`/`MIN_NOTIONAL` only), discovered when a
    test limit order priced 50% below market was correctly rejected by
    the exchange for exactly this reason
  - **Known gap, not fixed here (forbidden by the spec's own
    integration point #1 — `OrderManager` must not change for Stage 2
    to work):** `OrderManager._apply_fill_to_account()` unconditionally
    writes to `paper_accounts.current_cash` regardless of `order.mode`.
    This was unreachable in Stage 1 (`LiveExecutionAdapter` always
    raised `NotImplementedError`) and is now reachable via a real live
    fill for the first time. Fixing it needs either a mode branch in
    `OrderManager` (forbidden) or a real live-account-balance model —
    realistically Stage 3 territory.
- **Live Execution Stage 3 — live trading security** (`core/security/`,
  `docs/execution_engine_stage3_spec.md`). **Complete and tested is
  explicitly NOT the same as "cleared for real money"** — see the
  verbatim caveat in the architectural-decisions section above. Tested
  against fakes/real local Postgres throughout (no real cloud KMS in
  the unit suite, matching every other "no real network" component in
  this project); the real testnet suite (Stage 2's, re-run with this
  stage's credential path wired in) still passes 3/3:
  - `_aead.py` — shared AES-256-GCM helper (key generation +
    nonce\|\|ciphertext framing) used by both `LocalDevKMSClient` and
    `CredentialVault` — one reviewed crypto implementation, not two
  - `kms_client.py` — `KMSClient` interface, `LocalDevKMSClient` (real,
    functional, testnet-only), `AWSKMSClient` (explicit
    `NotImplementedError` stub — confirmed with the user: no cloud KMS
    infra exists in this project yet, building against it would be
    untestable scope creep)
  - `mainnet_gate.py` — `MainnetGate.check()`, raises
    `MainnetGateViolationError` (never a warning) via `isinstance()`
    against the concrete `LocalDevKMSClient` class; the FIRST thing
    built to touch a `mainnet` flag anywhere in the system, proven
    before anything else was allowed to
  - `credential_vault.py` — `CredentialVault.encrypt()`/`decrypt()`,
    envelope encryption; `encrypt()` calls `MainnetGate.check()` as its
    very first action, the lowest possible layer
  - `key_lifecycle_manager.py` — `KeyLifecycleManager`, an explicit
    `_LEGAL_TRANSITIONS` table over `CredentialState` (`PENDING_VALIDATION`
    / `ACTIVE` / `VALIDATION_FAILED` / `ROTATION_DUE` / `REVOKED`,
    mirroring `core/execution/order.py`'s pattern — this state machine
    isn't given verbatim by the spec, designed here and flagged);
    `REVOKED` reachable from every state, terminal once reached;
    `sweep_rotation_due()` (90-day cadence, confirmed with the user)
    is a reminder only, never an automatic key change; gained
    `record_validation_success()` in step 5 since an already-`ACTIVE`
    credential's re-check timestamp update isn't a real state
    transition and can't go through `transition()`'s legality gate
  - `audit_db.py` + `credential_provider.py` — `CredentialProvider.get_credentials()`
    writes to `credential_audit_log` via the dedicated INSERT-only
    `credential_audit_writer` role BEFORE returning anything; never
    caches plaintext past the call; extended in step 7 with an optional
    `revocation` check that runs FIRST, before the vault is ever touched
  - `permission_checker.py` + `binance_permission_checker.py` —
    `ExchangePermissionChecker` interface + a real signed
    `GET /sapi/v1/account/apiRestrictions` implementation (reuses Stage
    2's `ClockSyncService`); one real bug caught by tests: it initially
    classified every HTTP error ≥400 as fatal, never distinguishing
    retryable 5xx/429 — fixed to match the project's established
    classification convention
  - `permission_validator.py` — `PermissionValidator.validate()`: a
    withdrawal-enabled finding transitions the credential to
    `VALIDATION_FAILED`, disarms every strategy via an injected
    `Disarmer` Protocol (structurally decoupled from `ArmingService`,
    which doesn't exist until step 6), and publishes
    `CredentialValidationFailed` — all three, every time, proven
    together, not just the classification. `sweep_active_credentials()`
    is decision #2's recurring re-check, same "clean check is still
    logged evidence" posture as `ReconciliationJob`
  - `arming_service.py` — `ArmingService` (per account/strategy/exchange,
    48h expiry confirmed with the user, `is_armed()` computes expiry at
    READ time so no sweep job is needed for correctness),
    `disarm_all()` (satisfies `PermissionValidator`'s `Disarmer`
    Protocol — proven with a real end-to-end integration test once
    both components existed), `on_config_changed()` (reverts to
    unarmed, requires fresh re-confirmation). `is_trading_permitted()`
    is the standalone dual-gate function combining `KillSwitch` +
    `ArmingService` — neither class imports or knows about the other
  - `emergency_revocation.py` — `EmergencyCredentialRevocation`, its own
    `credential_revocation` table (deliberately NOT a reuse of
    `CredentialState.REVOKED`), mirrors `KillSwitch`'s "never
    auto-clears" posture; `re_grant()` is always explicit and logged
  - `events.py` gained `CredentialDecrypted`, `CredentialValidationFailed`,
    `ArmingStateChanged`, `ArmingExpired`, `EmergencyRevocationTriggered`,
    `KeyRotationDue`
  - `core/execution/binance_execution_adapter.py` **rewired, not
    rewritten** — see the architectural-decisions bullet above for the
    fresh-fetch-per-call design decision, confirmed explicitly with the
    user before implementing since it touches the constructor and every
    public method. All 14 of Stage 2's existing order-logic tests pass
    with unchanged assertions, only the fixture's credential setup
    differs — the concrete proof decision #7 actually holds
  - `schema.sql` gained `encrypted_credentials`, `credential_audit_log`
    (+ `credential_audit_writer` role/grants, including the
    easy-to-forget `BIGSERIAL` sequence `USAGE` grant — caught by
    actually running the immutability test, not assumed), `arming_state`,
    `credential_revocation`
  - Known, deliberate gaps: no real cloud `KMSClient` implementation
    (Stage 3's own open decision #1, confirmed deferred); no live
    per-trade wiring of the dual gate (`is_trading_permitted()`) into
    `OrderManager`/`RiskEngine`'s actual order-submission path — this
    stage built and tested the gate itself, but nothing yet calls it
    before every live order; that integration is real remaining work,
    not assumed done
- **AI Analysis & Signal Explanation Engine** (`core/ai_assistant/`,
  `news_sources/`, `docs/ai_assistant_spec.md`) — strictly downstream
  and read-only with respect to every trading table, enforced at the
  Postgres role level (see the architectural-decisions bullet above),
  tested end-to-end against real local Postgres, no mocks; only
  `LLMClient`'s Anthropic calls are faked (never real network in the
  standard suite, matching the project's established pattern):
  - `prompt_template.py` — `PromptTemplate`/`PromptTemplateRegistry`,
    the only place system-prompt wording lives; every explanation
    references an exact template id/version
  - `readonly_db.py` — `ReadonlySessionLocal`, bound to the dedicated
    `llm_readonly` Postgres role (`SELECT`-only grants, added to
    `schema.sql`)
  - `context_builder.py` — `ContextBuilder`: `build_trade_context()`,
    `build_risk_decision_context()`, `build_regime_context()`,
    `build_daily_summary_context()`. Pulls exactly the relevant rows
    for one subject, no broader query, no invented facts — raises
    rather than fabricating when grounding data is missing (e.g. no
    matching `signal_log` row, no `account_snapshots` coverage for a
    requested date)
  - `llm_usage_tracker.py` — `LLMUsageTracker`, check-then-increment in
    one method against `llm_usage_daily` so the daily call cap is
    enforced code, not configuration nothing reads
  - `llm_client.py` — `LLMClient`, wraps the Claude API; the real
    `anthropic` SDK is an optional dependency
    (`pyproject [project.optional-dependencies].llm`), imported lazily
    only for a real call — the standard test suite never needs it
  - `explanation_cache.py` — `ExplanationCache.get_or_generate()`,
    hash-keyed on serialized grounding facts (`llm_explanations`); a
    cache hit costs zero LLM calls
  - `daily_summary_job.py` — `DailySummaryJob`, triggered by the
    existing ingestion `Scheduler` (an additive, optional
    `daily_summary_job` param — zero new scheduling mechanism, zero
    hard runtime dependency from ingestion onto `core.ai_assistant`)
  - `news_source_adapter.py`/`news_source_registry.py`/
    `news_ingestion_service.py` + `news_sources/coindesk_rss_adapter.py`
    — structural copy of `ExchangeAdapter`/`StrategyRegistry`'s
    discovery pattern; idempotent on `news_articles.url`
  - `chat_tool.py`/`chat_tool_registry.py` — `ChatTool` (no
    write-capable tool exists anywhere in this component, structurally,
    not by omission) + `ChatToolRegistry.execute_tool_call()`, the
    single choke point that strips any LLM-supplied `account_id`/
    `user_id` and injects the real session's. `GetTradeTool` additionally
    verifies resource *ownership* (an order's `account_id`) before
    returning anything — account-id injection resistance alone isn't
    sufficient if a tool trusts a caller-supplied resource id
  - `chat_query_service.py` — `ChatQueryService.answer()`, logs every
    question/tool-call/response to `llm_query_log` unconditionally
  - `events.py` — `ExplanationGenerated`, `NewsIngested`,
    `ChatQueryAnswered`, `LLMUsageCapReached`
  - Two required security tests, both passing:
    `test_readonly_role_enforcement.py` (connects to Postgres *as*
    `llm_readonly`, proves the database itself refuses every write) and
    `test_prompt_injection_resistance.py` (a scripted fake LLM attempts
    a cross-account `account_id` injection and a nonexistent/write-style
    tool name; both refused)
  - Schema additions beyond the spec's literal list, each flagged in
    code where it happened: `orders.account_id` (nullable FK to
    `paper_accounts` — Stage 1's execution schema had no way to
    attribute an order to an account at all) and
    `paper_accounts.last_daily_summary_at` (the Scheduler's due-date
    watermark for `DailySummaryJob`, mirroring `ingestion_watermark`'s
    existing pattern)
  - Known, deliberate gaps: `DailySummaryContext.equity_start`/
    `equity_end` raise `LookupError` for any date `account_snapshots`
    doesn't cover, since Stage 1 has no snapshot-writer yet (see
    `core/execution/order_manager.py`'s own docstring point 3) —
    fabricating an equity figure from `current_cash` would be silently
    wrong for any date other than "right now," so this surfaces the gap
    loudly instead; `ChatQueryService` does at most one tool-call round
    trip (not the full Anthropic multi-turn `tool_result` protocol),
    since the spec's `LLMClient.generate()` signature carries no
    conversation history
- Full test suite in `tests/` — 566 tests collected: 563 passing +
  3 real-testnet-only as of last run, all against real local Postgres
  and/or a real local WebSocket server, no mocks; LLM calls faked in
  the standard suite; Binance calls faked in the standard suite except
  the `testnet`-marked integration suite
  (`test_binance_testnet_integration.py`, requires
  `BINANCE_TESTNET_API_KEY`/`BINANCE_TESTNET_API_SECRET` — skipped
  without them). **That suite was run against real Binance testnet
  twice during this build — once for Stage 2, again after Stage 3's
  credential-vault path was wired in — and all 3 tests pass both
  times**; see the architectural-decisions bullets above for the real
  API-deprecation issue (410 Gone on the old listenKey endpoint) the
  first run surfaced and how it was fixed, and for the credential
  fresh-fetch-per-call design decision the second wiring required.

## What's NOT built yet (next up)
- Execution layer Stage 3's own open decision #1: a real (non-stub)
  `KMSClient` for a real cloud provider (AWS KMS / Vault / etc.) —
  deferred since no cloud infrastructure is configured in this project
  yet; confirmed with the user rather than built speculatively
- `is_trading_permitted()` (the `KillSwitch` + `ArmingService` dual
  gate) is built and tested in isolation but NOT yet called from
  `OrderManager`/`RiskEngine`'s actual live order-submission path —
  the gate exists and works; nothing yet consults it before every live
  order. Real remaining work, not assumed done
- `SymbolFilterCache` doesn't model Binance's `PERCENT_PRICE_BY_SIDE`
  filter (price-deviation-from-market) — only
  `LOT_SIZE`/`PRICE_FILTER`/`MIN_NOTIONAL` per the spec's decision #5;
  a limit order priced far enough from market will be correctly
  rejected by the exchange (not a crash) but not caught locally first
- The `paper_accounts.current_cash`-for-live-orders gap flagged above —
  needs a real live-account-balance model
- Whatever writes `account_snapshots` — nothing does yet (Stage 1
  gap), which is why `DailySummaryContext` can raise `LookupError` for
  dates with no snapshot coverage
- The real-API (non-pytest) integration tests for `LLMClient` mentioned
  in `docs/ai_assistant_spec.md` section 5 — not run as part of this
  build, since they cost real money and need a real `ANTHROPIC_API_KEY`
- **A deliberate testnet soak period under the full Stage 3 security
  path, before any real `mainnet=True` credential is used** — a human
  decision, made separately by the user, not a code deliverable
- SaaS layer — all later phases

## Commands
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
docker compose up -d        # starts local Postgres
pytest -v                   # run all tests
ruff check .                # lint
black .                     # format
mypy core/ strategies/      # type check
```

## Working style
The user (project owner) has zero prior coding experience and is learning
alongside this build. Explain what you're doing and why in plain terms,
not just what. Don't assume familiarity with terminal/git — spell out
commands when introducing a new workflow. Run tests after every change and
report pass/fail honestly, don't claim something works without having run
it.
