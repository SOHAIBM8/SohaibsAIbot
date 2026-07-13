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
- `core/experiment.py` — experiment tracking (stubs; not yet wired to Postgres)
- `core/db.py`, `core/logging_config.py` — infra plumbing
- `strategies/ema_cross.py`, `strategies/rsi_mean_reversion.py` — reference strategies
- `schema.sql` — Postgres schema
- Full test suite in `tests/` — 62 tests passing as of last run

## What's NOT built yet (next up)
- Real Postgres wiring for `ExperimentTracker` (currently stubs)
- Historical data ingestion (raw OHLCV from Binance)
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
