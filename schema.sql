-- Metadata / application state lives in Postgres from day one.
-- Historical OHLCV + feature snapshots for backtesting live in Parquet,
-- partitioned by symbol/month, read directly by the backtest engine.
-- Timescale extension can be added to this same Postgres instance later
-- for a live-query path, with zero migration.
--
-- Update: the "later" above is now — see the ingestion schema block
-- at the bottom of this file, which enables TimescaleDB on raw_ohlcv
-- from day one of the ingestion component (see
-- docs/historical_data_ingestion_spec.md, decision #1).

CREATE TABLE strategies (
    strategy_id     TEXT PRIMARY KEY,      -- "ema_cross@1.0.0"
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    author          TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    description     TEXT,
    parameters      JSONB,
    compatible_pipeline_versions TEXT[],
    works_best_in   TEXT[]
);

CREATE TABLE feature_registry (
    feature_name    TEXT NOT NULL,
    version         TEXT NOT NULL,
    formula_ref     TEXT NOT NULL,          -- module.function path
    parameters      JSONB,
    dependencies    TEXT[],
    last_updated    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (feature_name, version)
);

CREATE TABLE experiments (
    experiment_id       SERIAL PRIMARY KEY,
    strategy_ids        TEXT[] NOT NULL,
    symbol               TEXT NOT NULL,
    timeframe            TEXT NOT NULL,
    date_start            DATE NOT NULL,
    date_end               DATE NOT NULL,
    feature_pipeline_version TEXT NOT NULL,
    fee_bps               NUMERIC,
    slippage_model         TEXT,
    code_commit_hash       TEXT NOT NULL,
    started_at             TIMESTAMPTZ NOT NULL,
    finished_at            TIMESTAMPTZ,
    metrics                JSONB,
    equity_curve_path      TEXT,
    notes                  TEXT
);

-- Explainability log: every signal a strategy generates OR rejects,
-- per bar, whether or not it was acted on. This is the rich research
-- dataset — "why generated / why rejected / what almost happened."
CREATE TABLE signal_log (
    id                  BIGSERIAL PRIMARY KEY,
    experiment_id       INT REFERENCES experiments(experiment_id),
    symbol              TEXT NOT NULL,
    bar_time            TIMESTAMPTZ NOT NULL,
    strategy_id         TEXT NOT NULL,
    regime              TEXT,
    regime_confidence   NUMERIC,
    direction           SMALLINT,
    signal_strength     NUMERIC,
    confidence          NUMERIC,
    reasons             TEXT[],
    rejected_reasons    TEXT[],
    acted_on            BOOLEAN,
    outcome             JSONB          -- filled in after the fact: pnl, exit_reason
);

CREATE INDEX idx_signal_log_strategy_regime ON signal_log (strategy_id, regime);
CREATE INDEX idx_signal_log_experiment ON signal_log (experiment_id);

-- =====================================================================
-- Risk engine (docs/risk_engine_spec.md) — step 2: RiskConfig only.
-- kill_switch_state, circuit_breaker_event_log, and risk_decision_log
-- are added in later steps (3, 4, 9) per the spec's build order.
-- =====================================================================

CREATE TABLE risk_config (
    risk_config_id      TEXT PRIMARY KEY,
    version              TEXT NOT NULL,
    daily_loss_limit_pct  NUMERIC,
    weekly_loss_limit_pct NUMERIC,
    drawdown_tier_1_pct   NUMERIC,
    drawdown_tier_1_factor NUMERIC,
    drawdown_tier_2_pct   NUMERIC,
    drawdown_tier_3_pct   NUMERIC,
    max_gross_exposure_pct NUMERIC,
    max_net_exposure_pct   NUMERIC,
    max_concurrent_positions INT,
    max_same_symbol_directional_exposure_pct NUMERIC,
    sizing_method          TEXT,
    kelly_fraction_multiplier NUMERIC,
    kelly_min_sample_size    INT,
    circuit_breaker_atr_percentile_threshold NUMERIC,
    circuit_breaker_confirmation_bars INT,
    kill_switch_auto_flatten BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Risk parameters are now versioned and comparable across experiments
-- exactly like strategy versions already are.
ALTER TABLE experiments ADD COLUMN risk_config_id TEXT REFERENCES risk_config(risk_config_id);

-- Step 3: kill switch state. Persisted (not held only in memory) so a
-- process restart never silently clears an emergency stop. 'global' is
-- the only scope used in V1; the column exists for a future per-
-- strategy/per-symbol kill switch without a schema change.
CREATE TABLE kill_switch_state (
    scope                  TEXT PRIMARY KEY,
    engaged                BOOLEAN NOT NULL DEFAULT FALSE,
    engaged_at             TIMESTAMPTZ,
    engaged_reason         TEXT,
    engaged_by             TEXT,
    auto_flatten_positions BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Step 4: audit trail for CircuitBreaker trip/clear transitions.
-- CircuitBreaker itself is pure in-memory (see core/risk/circuit_breaker.py);
-- rows here are written by whichever caller has a db handle (RiskEngine,
-- from step 9 onward).
CREATE TABLE circuit_breaker_event_log (
    id            BIGSERIAL PRIMARY KEY,
    breaker_name  TEXT NOT NULL,
    event_type    TEXT NOT NULL,   -- 'tripped' | 'cleared'
    reason        TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL
);

-- Step 9: one row per RiskEngine.size() call — the full audit trail of
-- every sizing decision, approved or rejected.
CREATE TABLE risk_decision_log (
    id                BIGSERIAL PRIMARY KEY,
    experiment_id     INT REFERENCES experiments(experiment_id),
    bar_time          TIMESTAMPTZ NOT NULL,
    strategy_id       TEXT NOT NULL,
    proposed_quantity NUMERIC,
    approved_quantity NUMERIC,
    rejection_reason  TEXT,           -- RejectionReason value, or null
    throttle_reasons  TEXT[],
    layer_results     JSONB,
    risk_config_id    TEXT REFERENCES risk_config(risk_config_id)
);

CREATE INDEX idx_risk_decision_log_experiment ON risk_decision_log (experiment_id);

-- =====================================================================
-- Historical data ingestion (docs/historical_data_ingestion_spec.md)
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Every ingestion run (backfill / incremental / gap_repair / data_quality)
-- gets exactly one row here — this table alone answers "what was
-- requested, received, stored, validated, retried, skipped, and why"
-- for every job that has ever run. Created before raw_ohlcv because
-- raw_ohlcv.source_run_id references it.
CREATE TABLE ingestion_run_log (
    run_id              BIGSERIAL PRIMARY KEY,
    run_type            TEXT NOT NULL CHECK (run_type IN ('backfill', 'incremental', 'gap_repair', 'data_quality')),
    exchange            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
    requested_range     JSONB,
    received_count      INT NOT NULL DEFAULT 0,
    stored_count        INT NOT NULL DEFAULT 0,
    validation_failures JSONB,
    retries             INT NOT NULL DEFAULT 0,
    skipped_reason      TEXT,
    error_message       TEXT
);

CREATE INDEX idx_ingestion_run_log_instrument ON ingestion_run_log (exchange, symbol, timeframe, started_at DESC);

-- Raw OHLCV candles. Immutable/append-only: closed candles are never
-- updated, only inserted (ON CONFLICT DO NOTHING at the application
-- layer) — this table is a TimescaleDB hypertable, partitioned on
-- open_time, because time-range queries over this table (backtests,
-- gap scans, continuous aggregates) are the dominant access pattern.
CREATE TABLE raw_ohlcv (
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    open_time       TIMESTAMPTZ NOT NULL,
    open            NUMERIC NOT NULL,
    high            NUMERIC NOT NULL,
    low             NUMERIC NOT NULL,
    close           NUMERIC NOT NULL,
    volume          NUMERIC NOT NULL,
    is_closed       BOOLEAN NOT NULL DEFAULT TRUE CHECK (is_closed = TRUE),
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_run_id   BIGINT REFERENCES ingestion_run_log(run_id),
    PRIMARY KEY (exchange, symbol, timeframe, open_time)
);

-- Chunk interval starts at 7 days per rule 8 (avoid the premature
-- optimization of guessing a smaller interval before real ingestion
-- volume is observed); revisit once there's data to profile.
SELECT create_hypertable('raw_ohlcv', 'open_time', chunk_time_interval => INTERVAL '7 days');

-- Compress chunks older than 30 days. No retention policy — full
-- history is wanted; the hook exists but nothing auto-deletes data.
ALTER TABLE raw_ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange, symbol, timeframe',
    timescaledb.compress_orderby = 'open_time DESC'
);
SELECT add_compression_policy('raw_ohlcv', INTERVAL '30 days');

-- Daily OHLCV rolled up from 1m/1h data, refreshed nightly. Filtered to
-- sub-daily source timeframes so a stored 1d candle is never
-- double-counted into its own daily rollup.
CREATE MATERIALIZED VIEW raw_ohlcv_daily
WITH (timescaledb.continuous) AS
SELECT
    exchange,
    symbol,
    timeframe AS source_timeframe,
    time_bucket('1 day', open_time) AS day,
    first(open, open_time) AS open,
    max(high) AS high,
    min(low) AS low,
    last(close, open_time) AS close,
    sum(volume) AS volume
FROM raw_ohlcv
WHERE timeframe IN ('1m', '5m', '15m', '1h', '4h')
GROUP BY exchange, symbol, source_timeframe, day
WITH NO DATA;

SELECT add_continuous_aggregate_policy('raw_ohlcv_daily',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day'
);

-- Per-(exchange,symbol,timeframe) ingestion state: what's been
-- discovered/ingested so far, and when each background sweep last ran.
CREATE TABLE ingestion_watermark (
    exchange                    TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    timeframe                   TEXT NOT NULL,
    earliest_available_at       TIMESTAMPTZ,
    last_ingested_open_time     TIMESTAMPTZ,
    backfill_complete           BOOLEAN NOT NULL DEFAULT FALSE,
    last_gap_scan_at            TIMESTAMPTZ,
    last_data_quality_check_at  TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (exchange, symbol, timeframe)
);

-- Detected holes in raw_ohlcv. status transitions: pending -> repaired
-- (data was found on retry) or pending -> confirmed_absent (3 attempts,
-- >=24h apart, exhausted — a terminal state the gap scanner must not
-- re-flag).
CREATE TABLE ingestion_gap (
    gap_id              BIGSERIAL PRIMARY KEY,
    exchange            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL,
    gap_start           TIMESTAMPTZ NOT NULL,
    gap_end             TIMESTAMPTZ NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'repaired', 'confirmed_absent')),
    attempts            INT NOT NULL DEFAULT 0,
    last_attempt_at     TIMESTAMPTZ,
    next_attempt_after  TIMESTAMPTZ,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    UNIQUE (exchange, symbol, timeframe, gap_start, gap_end)
);

CREATE INDEX idx_ingestion_gap_pending ON ingestion_gap (exchange, symbol, timeframe, status) WHERE status = 'pending';

-- What to ingest. Adding coverage is a row insert here, never a code
-- change.
CREATE TABLE tracked_instruments (
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (exchange, symbol, timeframe)
);

-- Nightly correctness audits — distinct from ingestion_gap, which only
-- tracks completeness. This tracks whether stored data is actually
-- right (duplicates, OHLC violations, drift vs. the exchange, etc).
CREATE TABLE data_quality_report (
    report_id           BIGSERIAL PRIMARY KEY,
    exchange             TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    timeframe            TEXT NOT NULL,
    run_at               TIMESTAMPTZ NOT NULL,
    checks_run           JSONB NOT NULL,
    issues_found         JSONB NOT NULL,
    candles_checked      INT NOT NULL DEFAULT 0,
    cross_check_diffs    INT NOT NULL DEFAULT 0,
    summary              TEXT
);

CREATE INDEX idx_data_quality_report_instrument ON data_quality_report (exchange, symbol, timeframe, run_at DESC);

-- =====================================================================
-- Live Execution & Paper Trading, Stage 1 (docs/execution_engine_stage1_spec.md)
-- Stage 1 only: paper trading + shared order state machine + read-only
-- market data. No balances/reconciliation/external-trade tables —
-- those are meaningless without a real exchange connection and belong
-- to Stage 2/3.
-- =====================================================================

-- Step 1: orders/fills.
CREATE TABLE orders (
    client_order_id     TEXT PRIMARY KEY,
    exchange_order_id   TEXT,                -- null until Stage 2
    strategy_id         TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    order_type          TEXT NOT NULL,
    direction           SMALLINT NOT NULL,
    quantity            NUMERIC NOT NULL,
    limit_price         NUMERIC,
    stop_price          NUMERIC,
    mode                TEXT NOT NULL,       -- 'paper' | 'live'
    state               TEXT NOT NULL,
    risk_decision_id    BIGINT NOT NULL REFERENCES risk_decision_log(id),
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL
);

CREATE TABLE fills (
    id                  BIGSERIAL PRIMARY KEY,
    client_order_id     TEXT NOT NULL REFERENCES orders(client_order_id),
    fill_price          NUMERIC NOT NULL,
    quantity            NUMERIC NOT NULL,
    fee                 NUMERIC NOT NULL,
    is_partial          BOOLEAN NOT NULL,
    filled_at           TIMESTAMPTZ NOT NULL
);

-- Step 3: paper account balance tracking. Table only for now — the
-- actual balance-update logic lives in OrderManager.handle_fill()
-- (step 4), since that's the one place a fill's effect on an account
-- is known.
CREATE TABLE paper_accounts (
    account_id          TEXT PRIMARY KEY,
    starting_balance     NUMERIC NOT NULL,
    current_cash          NUMERIC NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL
);

CREATE TABLE account_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    account_id            TEXT NOT NULL REFERENCES paper_accounts(account_id),
    equity                  NUMERIC NOT NULL,
    open_position_count       INT NOT NULL,
    snapshot_at                 TIMESTAMPTZ NOT NULL
);

-- =====================================================================
-- AI Analysis & Signal Explanation Engine (docs/ai_assistant_spec.md)
-- Strictly downstream and read-only w.r.t. every trading table above —
-- see the llm_readonly role (step 2, below) for the guarantee enforced
-- at the database grant level, not just in application code.
-- =====================================================================

-- Step 1: versioned prompt templates. The only place system-prompt
-- wording lives (decision #5) — every generated explanation references
-- a template_id here, so what was asked is always reproducible.
CREATE TABLE prompt_templates (
    template_id    TEXT PRIMARY KEY,
    version         TEXT NOT NULL,
    subject_type     TEXT NOT NULL,   -- 'trade' | 'risk_decision' | 'regime' | 'daily_summary' | 'chat'
    template_text      TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL
);

-- Step 2: dedicated read-only role (decision #1). No INSERT/UPDATE/
-- DELETE grant exists for this role on ANY table, now or in the
-- future — any table added later (including a future exchange-key
-- vault) must be deliberately excluded from this role's grants, not
-- deliberately included. Password is a local-dev-only convenience,
-- matching the plaintext POSTGRES_PASSWORD already used for the
-- primary app role in docker-compose.yml; override via
-- LLM_READONLY_DATABASE_URL (core/ai_assistant/readonly_db.py) for
-- any non-local environment.
DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'llm_readonly') THEN
      CREATE ROLE llm_readonly LOGIN PASSWORD 'llm_readonly_dev_password';
   END IF;
END
$$;
GRANT CONNECT ON DATABASE trading_platform TO llm_readonly;
GRANT USAGE ON SCHEMA public TO llm_readonly;
GRANT SELECT ON
    signal_log, risk_decision_log, orders, fills,
    experiments, paper_accounts, account_snapshots
TO llm_readonly;
-- news_articles is granted in step 6, once that table exists.

-- Step 3: daily LLM call/cost cap, enforced in code
-- (core/ai_assistant/llm_usage_tracker.py), not just configured.
-- daily_cap_calls is stored per-row (not read from config at query
-- time) so a changed cap never silently rewrites the meaning of an
-- already-closed day's history.
CREATE TABLE llm_usage_daily (
    usage_date         DATE PRIMARY KEY,
    calls_made           INT NOT NULL DEFAULT 0,
    tokens_used             BIGINT NOT NULL DEFAULT 0,
    estimated_cost             NUMERIC NOT NULL DEFAULT 0,
    daily_cap_calls               INT NOT NULL,
    daily_cap_reached                BOOLEAN NOT NULL DEFAULT FALSE
);

-- Step 4: one row per actually-generated explanation. grounding_fact_hash
-- is what ExplanationCache keys on to avoid a repeat LLM call for
-- unchanged facts; prompt_template_id makes every explanation
-- traceable to the exact wording that produced it (decision #5).
CREATE TABLE llm_explanations (
    explanation_id    BIGSERIAL PRIMARY KEY,
    subject_type        TEXT NOT NULL,
    subject_id            TEXT NOT NULL,
    grounding_fact_hash     TEXT NOT NULL,
    prompt_template_id        TEXT NOT NULL REFERENCES prompt_templates(template_id),
    model_used                  TEXT NOT NULL,
    generated_text                TEXT NOT NULL,
    generated_at                    TIMESTAMPTZ NOT NULL,
    tokens_used                      INT,
    cost_estimate                      NUMERIC
);

CREATE INDEX idx_llm_explanations_subject_hash
    ON llm_explanations (subject_type, subject_id, grounding_fact_hash);

-- Step 6: news articles. UNIQUE(url) is a necessary addition beyond
-- the spec's literal column list — NewsIngestionService's ON CONFLICT
-- DO NOTHING idempotency needs a unique/exclusion constraint to target,
-- and a URL is a sufficient natural key for one article across repeated
-- feed fetches.
CREATE TABLE news_articles (
    article_id     BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    url              TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    published_at       TIMESTAMPTZ,
    ingested_at          TIMESTAMPTZ NOT NULL,
    raw_content            TEXT
);

GRANT SELECT ON news_articles TO llm_readonly;

-- Step 5: orders had no account_id column — Stage 1's execution schema
-- never needed one (OrderManager already tracks account_id in-process,
-- see its own module docstring point 2), but ContextBuilder.
-- build_daily_summary_context() is account-scoped and has no other way
-- to determine which orders belong to which paper account. Additive,
-- nullable (a live-mode order in a future stage may have no paper
-- account at all) — same pattern as `experiments.risk_config_id`.
ALTER TABLE orders ADD COLUMN account_id TEXT REFERENCES paper_accounts(account_id);

-- Step 5: DailySummaryJob is "triggered by the existing Scheduler, not
-- a new scheduling mechanism" — Scheduler.run_once() needs a per-
-- account "when did this last run" watermark to decide if a nightly
-- summary is due, exactly like ingestion_watermark already does for
-- backfill/gap-scan/data-quality cadence. Additive, nullable.
ALTER TABLE paper_accounts ADD COLUMN last_daily_summary_at TIMESTAMPTZ;
