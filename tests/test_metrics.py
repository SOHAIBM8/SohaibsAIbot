import pandas as pd
import pytest

from core.metrics import compute_metrics
from core.portfolio import Trade


def make_trade(pnl, entry_time, exit_time, r_multiple=None):
    return Trade(
        strategy_id="s1", direction=1, entry_time=entry_time, exit_time=exit_time,
        entry_price=100.0, exit_price=100.0 + pnl, quantity=1.0, fees_paid=0.0,
        pnl=pnl, pnl_pct=pnl / 100.0, r_multiple=r_multiple, exit_reason="manual",
        regime_at_entry="bull_trend",
    )


def test_win_rate_and_profit_factor():
    trades = [make_trade(10, 0, 1), make_trade(-5, 1, 2), make_trade(20, 2, 3)]
    equity = pd.Series([100, 110, 105, 125])

    metrics = compute_metrics(trades, equity, periods_per_year=252)

    assert metrics.win_rate == pytest.approx(2 / 3)
    assert metrics.profit_factor == pytest.approx(30 / 5)  # gross profit / gross loss


def test_profit_factor_is_inf_with_no_losses():
    trades = [make_trade(10, 0, 1), make_trade(5, 1, 2)]
    equity = pd.Series([100, 110, 115])
    metrics = compute_metrics(trades, equity, periods_per_year=252)
    assert metrics.profit_factor == float("inf")


def test_profit_factor_is_zero_with_no_trades():
    metrics = compute_metrics([], pd.Series([100.0]), periods_per_year=252)
    assert metrics.profit_factor == 0.0
    assert metrics.win_rate == 0.0
    assert metrics.total_trades == 0


def test_expectancy_is_mean_pnl_per_trade():
    trades = [make_trade(10, 0, 1), make_trade(-4, 1, 2), make_trade(6, 2, 3)]
    equity = pd.Series([100, 110, 106, 112])
    metrics = compute_metrics(trades, equity, periods_per_year=252)
    assert metrics.expectancy == pytest.approx((10 - 4 + 6) / 3)


def test_avg_r_multiple_ignores_trades_without_a_stop():
    trades = [make_trade(10, 0, 1, r_multiple=2.0), make_trade(5, 1, 2, r_multiple=None)]
    equity = pd.Series([100, 110, 115])
    metrics = compute_metrics(trades, equity, periods_per_year=252)
    assert metrics.avg_r_multiple == pytest.approx(2.0)


def test_avg_r_multiple_is_none_with_no_stops_anywhere():
    trades = [make_trade(10, 0, 1, r_multiple=None)]
    equity = pd.Series([100, 110])
    metrics = compute_metrics(trades, equity, periods_per_year=252)
    assert metrics.avg_r_multiple is None


def test_max_drawdown_measured_from_peak():
    # up to 120, down to 90 (peak 120 -> trough 90 = -25%), back up to 100
    equity = pd.Series([100.0, 120.0, 90.0, 100.0])
    metrics = compute_metrics([], equity, periods_per_year=252)
    assert metrics.max_drawdown == pytest.approx((90.0 - 120.0) / 120.0)


def test_cagr_doubling_in_one_year():
    # 252 trading days, equity doubles -> CAGR should be ~100%
    equity = pd.Series([100.0] + [100.0] * 250 + [200.0])
    metrics = compute_metrics([], equity, periods_per_year=252)
    assert metrics.cagr == pytest.approx(1.0, rel=0.05)


def test_sharpe_is_zero_for_flat_equity_curve():
    equity = pd.Series([100.0, 100.0, 100.0, 100.0])
    metrics = compute_metrics([], equity, periods_per_year=252)
    assert metrics.sharpe_ratio == 0.0


def test_sharpe_is_positive_for_consistent_gains():
    equity = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0])
    metrics = compute_metrics([], equity, periods_per_year=252)
    assert metrics.sharpe_ratio > 0


def test_sortino_ignores_upside_volatility():
    """A path with a big upside spike but no downside moves should have
    a very high (or undefined-safe) Sortino, since only downside
    deviation is penalized."""
    equity = pd.Series([100.0, 101.0, 150.0, 151.0])  # big up-jump, no losses
    metrics = compute_metrics([], equity, periods_per_year=252)
    assert metrics.sortino_ratio == 0.0  # no downside periods at all -> defined as 0, not inf


def test_exposure_fraction_from_trade_timestamps():
    equity = pd.Series([100, 101, 102, 103, 104], index=[0, 1, 2, 3, 4])
    trades = [make_trade(5, entry_time=1, exit_time=2)]  # in position for bars 1,2
    metrics = compute_metrics(trades, equity, periods_per_year=252)
    assert metrics.exposure == pytest.approx(2 / 5)
