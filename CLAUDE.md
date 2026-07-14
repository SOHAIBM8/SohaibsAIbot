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
- **Live Execution & Paper Trading ships in three stages; only Stage 1
  is built.** `OrderManager` and the order state machine are IDENTICAL
  for paper and live — only `ExecutionAdapter` differs, so Stage 2
  (real exchange order placement) plugs in without touching
  `OrderManager`. Stage 1 has ZERO exchange authentication and ZERO
  real order placement — `LiveExecutionAdapter` is an interface stub
  only, every method raises `NotImplementedError`. **Stage 2 (real
  exchange adapters) and Stage 3 (live trading enablement, API key
  custody) are separate, not-yet-specced phases** — do not treat
  Stage 1 as "the execution layer is done." Every order, paper or
  live, must originate from an approved `SizingDecision` — no code
  path places an order without Risk Engine approval.
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
Stage 1 (paper trading + shared order state machine + read-only market
data) is built; Stage 2 (real exchange order placement) and Stage 3
(live trading enablement, API key custody) are NOT started and need
their own specs before any work begins on them. The AI Analysis &
Signal Explanation Engine (all 9 build-order steps) is built, strictly
downstream/read-only of everything above it — see the dedicated
write-up below. SaaS multi-tenancy is next, not yet started.

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
- Full test suite in `tests/` — 402 tests passing as of last run (345 prior +
  57 across `tests/test_ai_assistant/`, all against real local Postgres
  and/or a real local WebSocket server, no mocks; LLM calls faked)

## What's NOT built yet (next up)
- Execution layer Stage 2 (real exchange order placement — Binance
  first, Kraken/Coinbase for US-user coverage) and Stage 3 (live
  trading enablement, API key custody) — both need their own specs
- Whatever writes `account_snapshots` — nothing does yet (Stage 1
  gap), which is why `DailySummaryContext` can raise `LookupError` for
  dates with no snapshot coverage
- The real-API (non-pytest) integration tests for `LLMClient` mentioned
  in `docs/ai_assistant_spec.md` section 5 — not run as part of this
  build, since they cost real money and need a real `ANTHROPIC_API_KEY`
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
