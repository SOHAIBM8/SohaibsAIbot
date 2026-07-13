-- Metadata / application state lives in Postgres from day one.
-- Historical OHLCV + feature snapshots for backtesting live in Parquet,
-- partitioned by symbol/month, read directly by the backtest engine.
-- Timescale extension can be added to this same Postgres instance later
-- for a live-query path, with zero migration.

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
