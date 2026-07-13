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
- **Risk engine (not yet built) has final authority** over position size
  and stops — a strategy's `entry_price`/`stop_loss`/`take_profit` are
  proposals, not orders.
- **Binance for development**, but the exchange abstraction must support
  Kraken/Coinbase early — Binance.com is unavailable to US users, and this
  is headed toward a multi-user SaaS.

## Build order (don't skip ahead)
Foundations → backtesting engine → execution layer → risk engine → SaaS
multi-tenancy → AI signal research. AI is deliberately last — infra and
risk discipline come first.

## What's built so far
- `core/strategy_base.py` — `Signal`, `StrategyMeta`, `StrategyBase`, `Regime`, `VolRegime`
- `core/strategy_registry.py` — plugin discovery, regime-based filtering
- `core/confidence_engine.py` — confidence scoring, separate from strategies
- `core/feature_store.py` — `FeatureRegistry`, `FeatureWindow`, dependency resolution
- `core/indicators/` — `pandas_ta_adapter.py` (library wrap), `derived.py` (hand-written), `register.py` (default registry)
- `core/regime_detector.py` + `core/regime_config.py` — rule-based trend/vol regime detection with hysteresis
- `core/execution_model.py` — fee + slippage simulation
- `core/position_sizing.py` — `PositionSizer` interface + `FixedFractionSizer` (Risk Engine stand-in)
- `core/portfolio.py` — cash/position/trade tracking, long & short
- `core/backtest_engine.py` — event-driven loop, next-bar-open execution, warmup handling, multi-strategy
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
- Full test suite in `tests/` — 124 tests passing as of last run (68 prior +
  56 in `tests/ingestion/`, all against real local Postgres, no mocks)

## What's NOT built yet (next up)
- Risk engine (real portfolio-level exposure limits, replacing `FixedFractionSizer`)
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
