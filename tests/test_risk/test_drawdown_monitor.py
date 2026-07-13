import pytest

from core.portfolio import PortfolioView
from core.risk.drawdown_monitor import DrawdownMonitor

PEAK = 10_000.0


def view(equity: float) -> PortfolioView:
    return PortfolioView(equity=equity, peak_equity=PEAK, open_positions=[], trade_history=[])


@pytest.fixture
def monitor():
    return DrawdownMonitor(tier_1_pct=0.10, tier_1_factor=0.5, tier_2_pct=0.15, tier_3_pct=0.25)


def test_no_drawdown_is_tier_0(monitor):
    result = monitor.evaluate(view(PEAK))
    assert result.tier == 0
    assert result.current_drawdown_pct == 0.0
    assert result.size_multiplier == 1.0


def test_small_drawdown_under_tier_1_is_tier_0(monitor):
    result = monitor.evaluate(view(9_010.0))  # -9.9%, just under the 10% throttle threshold
    assert result.tier == 0
    assert result.size_multiplier == 1.0


def test_drawdown_exactly_at_tier_1_threshold_throttles(monitor):
    result = monitor.evaluate(view(9_000.0))  # exactly -10%
    assert result.tier == 1
    assert result.size_multiplier == 0.5
    assert result.current_drawdown_pct == pytest.approx(-0.10)


def test_drawdown_between_tier_1_and_tier_2_stays_tier_1(monitor):
    result = monitor.evaluate(view(8_800.0))  # -12%
    assert result.tier == 1
    assert result.size_multiplier == 0.5


def test_drawdown_exactly_at_tier_2_threshold_hard_stops(monitor):
    result = monitor.evaluate(view(8_500.0))  # exactly -15%
    assert result.tier == 2
    assert result.size_multiplier == 0.0


def test_drawdown_between_tier_2_and_tier_3_stays_tier_2(monitor):
    result = monitor.evaluate(view(8_000.0))  # -20%
    assert result.tier == 2
    assert result.size_multiplier == 0.0


def test_drawdown_exactly_at_tier_3_threshold_is_kill_switch_tier(monitor):
    result = monitor.evaluate(view(7_500.0))  # exactly -25%
    assert result.tier == 3
    assert result.size_multiplier == 0.0
    assert result.current_drawdown_pct == pytest.approx(-0.25)


def test_drawdown_beyond_tier_3_stays_tier_3(monitor):
    result = monitor.evaluate(view(6_000.0))  # -40%
    assert result.tier == 3
    assert result.size_multiplier == 0.0


def test_current_drawdown_pct_matches_backtest_max_drawdown_sign_convention(monitor):
    # core/metrics.py's _max_drawdown reports a negative fraction of the
    # peak (e.g. -0.23 = 23% drawdown) — DrawdownMonitor must match,
    # not report a positive magnitude.
    result = monitor.evaluate(view(9_500.0))
    assert result.current_drawdown_pct < 0
    assert result.current_drawdown_pct == pytest.approx(-0.05)


def test_zero_peak_equity_does_not_raise_zero_division(monitor):
    degenerate_view = PortfolioView(
        equity=0.0, peak_equity=0.0, open_positions=[], trade_history=[]
    )
    result = monitor.evaluate(degenerate_view)
    assert result.tier == 0
    assert result.current_drawdown_pct == 0.0


def test_equity_above_peak_is_not_a_drawdown():
    # Shouldn't happen in practice (peak_equity should track the max),
    # but the math must not report a "negative drawdown" as a breach.
    monitor = DrawdownMonitor(tier_1_pct=0.10, tier_1_factor=0.5, tier_2_pct=0.15, tier_3_pct=0.25)
    result = monitor.evaluate(
        PortfolioView(equity=11_000.0, peak_equity=10_000.0, open_positions=[], trade_history=[])
    )
    assert result.tier == 0
    assert result.current_drawdown_pct == pytest.approx(0.10)
    assert result.size_multiplier == 1.0
