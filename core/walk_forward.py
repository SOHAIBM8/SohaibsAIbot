"""
Walk-forward validation: splits the full history into sequential
windows and runs the backtest independently on each, rather than one
big in-sample run. Strong performance on one contiguous historical
period is weak evidence; consistent performance across several
independent, sequential windows is much stronger evidence against
overfitting.

V1 has no parameter-fitting step — our strategies are rule-based with
fixed parameters, not fit to data — so this reports metrics PER WINDOW
rather than performing train/test optimization. That's still valuable:
it surfaces regime-dependent strategies that only worked in one
historical stretch, which an aggregate single backtest would hide.
When parameter optimization is introduced later (e.g. for an ML
strategy), the train portion of each window is exactly where that
fitting would happen, with the test portion remaining untouched —
this splitter is deliberately structured so that extension doesn't
require rewriting it.
"""

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from core.backtest_engine import BacktestEngine, BacktestResult
from core.metrics import PerformanceMetrics, compute_metrics


@dataclass
class WalkForwardWindow:
    window_index: int
    test_start: object
    test_end: object
    result: BacktestResult
    metrics: PerformanceMetrics


def split_windows(df: pd.DataFrame, n_windows: int) -> list[pd.DataFrame]:
    """Split df into n_windows sequential, non-overlapping chunks."""
    if n_windows < 1:
        raise ValueError("n_windows must be >= 1")
    chunk_size = len(df) // n_windows
    if chunk_size == 0:
        raise ValueError("not enough data for the requested number of windows")
    windows = []
    for i in range(n_windows):
        start = i * chunk_size
        end = len(df) if i == n_windows - 1 else (i + 1) * chunk_size
        windows.append(df.iloc[start:end])
    return windows


def run_walk_forward(
    engine_factory: Callable[[], BacktestEngine],
    feature_df: pd.DataFrame,
    n_windows: int,
    periods_per_year: int,
) -> list[WalkForwardWindow]:
    """engine_factory: zero-arg callable returning a FRESH BacktestEngine
    for each window — fresh, so regime-detector hysteresis state and
    portfolio cash never leak between windows. Each window is evaluated
    as if it were an independent backtest starting from scratch."""
    results = []
    for i, window_df in enumerate(split_windows(feature_df, n_windows)):
        engine = engine_factory()
        result = engine.run(window_df)
        metrics = compute_metrics(result.trades, result.equity_curve, periods_per_year)
        results.append(
            WalkForwardWindow(
                window_index=i,
                test_start=window_df.index[0],
                test_end=window_df.index[-1],
                result=result,
                metrics=metrics,
            )
        )
    return results
