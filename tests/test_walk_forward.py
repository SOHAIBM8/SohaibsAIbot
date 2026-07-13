import pandas as pd
import pytest

from core.walk_forward import split_windows, run_walk_forward
from core.backtest_engine import BacktestEngine
from core.execution_model import ExecutionModel
from core.position_sizing import FixedFractionSizer
from core.regime_config import RegimeDetectorConfig
from core.regime_detector import RegimeDetector


def test_split_windows_covers_full_dataframe_without_gaps():
    df = pd.DataFrame({"x": range(100)})
    windows = split_windows(df, n_windows=4)

    assert len(windows) == 4
    assert sum(len(w) for w in windows) == 100
    # sequential, non-overlapping
    assert windows[0].index[-1] < windows[1].index[0]
    assert windows[-1].index[-1] == df.index[-1]


def test_split_windows_last_window_absorbs_remainder():
    df = pd.DataFrame({"x": range(10)})
    windows = split_windows(df, n_windows=3)  # 10 / 3 = 3 per window, remainder goes to last
    assert [len(w) for w in windows] == [3, 3, 4]


def test_split_windows_rejects_too_many_windows_for_data_size():
    df = pd.DataFrame({"x": range(2)})
    with pytest.raises(ValueError):
        split_windows(df, n_windows=5)


def test_run_walk_forward_produces_one_result_per_window():
    df = pd.DataFrame({
        "open": [100.0] * 40, "high": [102.0] * 40, "low": [98.0] * 40, "close": [101.0] * 40,
        "ema_20": [100.0] * 40, "ema_50": [100.0] * 40,
        "adx_14": [10.0] * 40, "atr_percentile_90": [0.5] * 40,
    })

    def engine_factory():
        return BacktestEngine(
            strategies=[],  # no strategies -> no trades, just checking the plumbing
            regime_detector=RegimeDetector(RegimeDetectorConfig()),
            position_sizer=FixedFractionSizer(),
            execution_model=ExecutionModel(),
            initial_capital=10_000.0,
        )

    results = run_walk_forward(engine_factory, df, n_windows=4, periods_per_year=252)

    assert len(results) == 4
    for r in results:
        assert r.metrics.total_trades == 0
        assert len(r.result.equity_curve) > 0
