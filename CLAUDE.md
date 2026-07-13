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

## Build order (don't skip ahead)
Foundations → backtesting engine → execution layer → risk engine → SaaS
multi-tenancy → AI signal research. AI is deliberately last — infra and
risk discipline come first. Risk engine is now built; execution layer
is next.

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
- Full test suite in `tests/` — 231 tests passing as of last run (124 prior +
  105 in `tests/test_risk/` + 2 more in `test_experiment.py` for
  `risk_config_id`, all against real local Postgres, no mocks)

## What's NOT built yet (next up)
- Execution layer (real exchange connectivity — Binance first, Kraken/Coinbase for US-user coverage)
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
