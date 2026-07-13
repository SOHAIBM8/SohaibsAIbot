"""
Experiment tracking. Every backtest run is recorded: parameters, data
range, code/feature versions, and results — so nothing is ever lost and
runs are always comparable and reproducible.

This is a deliberately minimal, hand-rolled schema. If experiment volume
outgrows what a table comfortably handles, MLflow (or similar) is a
reasonable drop-in replacement for this module specifically — no need
to build a fancier version ourselves before that's an actual problem.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ExperimentConfig:
    strategy_ids: list[str]          # e.g. ["ema_cross@1.0.0", "rsi_mean_reversion@1.0.0"]
    symbol: str
    timeframe: str
    date_range: tuple[str, str]
    feature_pipeline_version: str
    fee_bps: float
    slippage_model: str
    code_commit_hash: str            # git commit at run time — non-negotiable
                                      # for being able to reproduce a result


@dataclass
class ExperimentResult:
    experiment_id: int
    config: ExperimentConfig
    started_at: datetime
    finished_at: datetime
    metrics: dict                    # sharpe, max_drawdown, win_rate, total_return, ...
    equity_curve_path: str           # pointer to stored parquet/csv, not inlined
    notes: str = ""


class ExperimentTracker:
    def __init__(self, db):
        self.db = db  # Postgres connection

    def start(self, config: ExperimentConfig) -> int:
        """Insert a row, return experiment_id. Called before the backtest
        runs, so even a crashed run leaves a record."""
        ...

    def finish(self, experiment_id: int, metrics: dict, equity_curve_path: str):
        ...

    def compare(self, experiment_ids: list[int]) -> "ComparisonTable":
        """e.g. compare(ema_cross_v1_id, ema_cross_v2_id) side by side
        without the two runs' results ever getting mixed together."""
        ...
