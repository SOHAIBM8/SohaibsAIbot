"""
Experiment tracking. Every backtest run is recorded: parameters, data
range, code/feature versions, and results — so nothing is ever lost and
runs are always comparable and reproducible.

This is a deliberately minimal, hand-rolled schema. If experiment volume
outgrows what a table comfortably handles, MLflow (or similar) is a
reasonable drop-in replacement for this module specifically — no need
to build a fancier version ourselves before that's an actual problem.

Design note (settled while wiring this to real SQL, per CLAUDE.md rule 9):
`ExperimentConfig.date_range` was a `tuple[str, str]`. The `experiments`
table stores `date_start`/`date_end` as two separate DATE columns, and
SQLAlchemy's `date` type wants `datetime.date`, not an arbitrary string.
Rather than smuggle a two-element tuple through `Core.mapping`, `start()`
unpacks it into `date_start`/`date_end` and parses each with
`date.fromisoformat`. The dataclass itself is unchanged — this is a
translation done at the persistence boundary, which is exactly where
that kind of translation belongs.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import cast

import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)


@dataclass
class ExperimentConfig:
    strategy_ids: list[str]  # e.g. ["ema_cross@1.0.0", "rsi_mean_reversion@1.0.0"]
    symbol: str
    timeframe: str
    date_range: tuple[str, str]
    feature_pipeline_version: str
    fee_bps: float
    slippage_model: str
    code_commit_hash: str  # git commit at run time — non-negotiable
    # for being able to reproduce a result
    risk_config_id: str | None = None  # versions risk parameters across
    # experiments exactly like strategy_ids already does (docs/risk_engine_spec.md)


@dataclass
class ExperimentResult:
    experiment_id: int
    config: ExperimentConfig
    started_at: datetime
    finished_at: datetime | None
    metrics: dict
    equity_curve_path: str | None
    notes: str = ""


@dataclass
class ComparisonTable:
    """Side-by-side view of multiple experiments' configs and metrics,
    keyed by experiment_id so results from different runs never get
    merged into a single averaged/aggregated row by accident — the whole
    point of compare() is to keep runs distinct while viewing them
    together."""

    results: list[ExperimentResult] = field(default_factory=list)

    def metric(self, metric_name: str) -> dict[int, float | None]:
        """metric_name's value per experiment_id, e.g. compare
        Sharpe across runs without pulling in every other metric."""
        return {r.experiment_id: r.metrics.get(metric_name) for r in self.results}

    def as_rows(self) -> list[dict]:
        """One flat dict per experiment — convenient for a pandas
        DataFrame or a quick printed table."""
        rows = []
        for r in self.results:
            row = {
                "experiment_id": r.experiment_id,
                "strategy_ids": r.config.strategy_ids,
                "symbol": r.config.symbol,
                "timeframe": r.config.timeframe,
                "code_commit_hash": r.config.code_commit_hash,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                **r.metrics,
            }
            rows.append(row)
        return rows


class ExperimentTracker:
    """Reads/writes the `experiments` table (schema.sql). Takes a
    SQLAlchemy Session rather than a raw connection so it composes with
    core.db.SessionLocal — no separate connection path, per project rule."""

    def __init__(self, db: Session):
        self.db = db

    def start(self, config: ExperimentConfig) -> int:
        """Insert a row, return experiment_id. Called before the backtest
        runs, so even a crashed run leaves a record."""
        date_start, date_end = config.date_range
        started_at = datetime.now(UTC)
        result = self.db.execute(
            text("""
                INSERT INTO experiments (
                    strategy_ids, symbol, timeframe, date_start, date_end,
                    feature_pipeline_version, fee_bps, slippage_model,
                    code_commit_hash, started_at, risk_config_id
                ) VALUES (
                    :strategy_ids, :symbol, :timeframe, :date_start, :date_end,
                    :feature_pipeline_version, :fee_bps, :slippage_model,
                    :code_commit_hash, :started_at, :risk_config_id
                )
                RETURNING experiment_id
                """),
            {
                "strategy_ids": config.strategy_ids,
                "symbol": config.symbol,
                "timeframe": config.timeframe,
                "date_start": date.fromisoformat(date_start),
                "date_end": date.fromisoformat(date_end),
                "feature_pipeline_version": config.feature_pipeline_version,
                "fee_bps": config.fee_bps,
                "slippage_model": config.slippage_model,
                "code_commit_hash": config.code_commit_hash,
                "started_at": started_at,
                "risk_config_id": config.risk_config_id,
            },
        )
        experiment_id: int = result.scalar_one()
        self.db.commit()
        logger.info("experiment_started", experiment_id=experiment_id, symbol=config.symbol)
        return experiment_id

    def finish(self, experiment_id: int, metrics: dict, equity_curve_path: str) -> None:
        """Record final metrics and the equity curve pointer. Raises
        ValueError if experiment_id doesn't exist — finishing a run that
        was never started is a bug, not something to silently ignore."""
        result = cast(
            CursorResult,
            self.db.execute(
                text("""
                UPDATE experiments
                SET finished_at = :finished_at,
                    metrics = :metrics,
                    equity_curve_path = :equity_curve_path
                WHERE experiment_id = :experiment_id
                """),
                {
                    "finished_at": datetime.now(UTC),
                    "metrics": json.dumps(metrics),
                    "equity_curve_path": equity_curve_path,
                    "experiment_id": experiment_id,
                },
            ),
        )
        if result.rowcount == 0:
            self.db.rollback()
            raise ValueError(f"no experiment with experiment_id={experiment_id}")
        self.db.commit()
        logger.info("experiment_finished", experiment_id=experiment_id)

    def compare(self, experiment_ids: list[int]) -> ComparisonTable:
        """e.g. compare(ema_cross_v1_id, ema_cross_v2_id) side by side
        without the two runs' results ever getting mixed together."""
        if not experiment_ids:
            return ComparisonTable(results=[])
        rows = (
            self.db.execute(
                text("""
                SELECT experiment_id, strategy_ids, symbol, timeframe, date_start,
                       date_end, feature_pipeline_version, fee_bps, slippage_model,
                       code_commit_hash, started_at, finished_at, metrics,
                       equity_curve_path, notes, risk_config_id
                FROM experiments
                WHERE experiment_id = ANY(:experiment_ids)
                ORDER BY experiment_id
                """),
                {"experiment_ids": experiment_ids},
            )
            .mappings()
            .all()
        )

        results = [
            ExperimentResult(
                experiment_id=row["experiment_id"],
                config=ExperimentConfig(
                    strategy_ids=row["strategy_ids"],
                    symbol=row["symbol"],
                    timeframe=row["timeframe"],
                    date_range=(row["date_start"].isoformat(), row["date_end"].isoformat()),
                    feature_pipeline_version=row["feature_pipeline_version"],
                    fee_bps=float(row["fee_bps"]) if row["fee_bps"] is not None else 0.0,
                    slippage_model=row["slippage_model"],
                    code_commit_hash=row["code_commit_hash"],
                    risk_config_id=row["risk_config_id"],
                ),
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                metrics=row["metrics"] or {},
                equity_curve_path=row["equity_curve_path"],
                notes=row["notes"] or "",
            )
            for row in rows
        ]
        return ComparisonTable(results=results)
