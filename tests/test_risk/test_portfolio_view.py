import pytest

from core.execution_model import ExecutionModel
from core.portfolio import Portfolio


@pytest.fixture
def portfolio():
    # zero fees/slippage so PnL arithmetic is exact and checkable by hand
    return Portfolio(
        initial_capital=10_000.0, execution_model=ExecutionModel(fee_bps=0, slippage_bps=0)
    )


def test_snapshot_with_no_positions_reflects_cash_only(portfolio):
    view = portfolio.snapshot(current_price=100.0)
    assert view.equity == 10_000.0
    assert view.peak_equity == 10_000.0
    assert view.open_positions == []
    assert view.trade_history == []


def test_snapshot_never_mutates_portfolio_state(portfolio):
    portfolio.open_position(
        strategy_id="s1",
        direction=1,
        reference_price=100.0,
        quantity=10.0,
        entry_time="t0",
        stop_loss=90.0,
        take_profit=None,
        regime_at_entry="bull_trend",
    )
    portfolio.snapshot(current_price=150.0)
    portfolio.snapshot(current_price=50.0)

    assert portfolio.equity_curve == []  # snapshot must never append here
    assert "s1" in portfolio.open_positions  # position untouched


def test_snapshot_reflects_unrealized_long_pnl(portfolio):
    portfolio.open_position(
        strategy_id="s1",
        direction=1,
        reference_price=100.0,
        quantity=10.0,
        entry_time="t0",
        stop_loss=90.0,
        take_profit=None,
        regime_at_entry="bull_trend",
    )
    view = portfolio.snapshot(current_price=110.0)

    assert view.equity == pytest.approx(10_000.0 + 100.0)  # (110-100)*10 unrealized
    assert len(view.open_positions) == 1
    position = view.open_positions[0]
    assert position.strategy_id == "s1"
    assert position.direction == 1
    assert position.entry_price == 100.0
    assert position.quantity == 10.0
    assert position.unrealized_pnl == pytest.approx(100.0)


def test_snapshot_reflects_unrealized_short_pnl(portfolio):
    portfolio.open_position(
        strategy_id="s1",
        direction=-1,
        reference_price=100.0,
        quantity=10.0,
        entry_time="t0",
        stop_loss=110.0,
        take_profit=None,
        regime_at_entry="bear_trend",
    )
    view = portfolio.snapshot(current_price=90.0)

    assert view.equity == pytest.approx(10_000.0 + 100.0)  # price fell 10, short profits
    assert view.open_positions[0].unrealized_pnl == pytest.approx(100.0)


def test_snapshot_peak_equity_uses_historical_high_water_mark(portfolio):
    portfolio.mark_to_market(timestamp="t0", current_price=100.0)  # equity 10,000
    portfolio.open_position(
        strategy_id="s1",
        direction=1,
        reference_price=100.0,
        quantity=10.0,
        entry_time="t1",
        stop_loss=90.0,
        take_profit=None,
        regime_at_entry="bull_trend",
    )
    portfolio.mark_to_market(timestamp="t2", current_price=150.0)  # equity 10,500, new peak

    view = portfolio.snapshot(current_price=120.0)  # equity now 10,200 — below the peak
    assert view.equity == pytest.approx(10_000.0 + (120.0 - 100.0) * 10.0)
    assert view.peak_equity == pytest.approx(10_500.0)


def test_snapshot_peak_equity_reflects_a_new_high_not_yet_recorded(portfolio):
    portfolio.mark_to_market(timestamp="t0", current_price=100.0)  # equity 10,000 recorded

    view = portfolio.snapshot(current_price=200.0)  # equity would be 10,000, no position open
    assert view.peak_equity == pytest.approx(10_000.0)

    portfolio.open_position(
        strategy_id="s1",
        direction=1,
        reference_price=100.0,
        quantity=10.0,
        entry_time="t1",
        stop_loss=90.0,
        take_profit=None,
        regime_at_entry="bull_trend",
    )
    view = portfolio.snapshot(
        current_price=300.0
    )  # equity 12,000 — a new high, not yet in equity_curve
    assert view.equity == pytest.approx(10_000.0 + (300.0 - 100.0) * 10.0)
    assert view.peak_equity == pytest.approx(view.equity)


def test_snapshot_trade_history_reflects_closed_trades(portfolio):
    portfolio.open_position(
        strategy_id="s1",
        direction=1,
        reference_price=100.0,
        quantity=10.0,
        entry_time="t0",
        stop_loss=90.0,
        take_profit=None,
        regime_at_entry="bull_trend",
    )
    portfolio.close_position("s1", reference_price=110.0, exit_time="t1", exit_reason="manual")

    view = portfolio.snapshot(current_price=110.0)
    assert len(view.trade_history) == 1
    assert view.trade_history[0].pnl == pytest.approx(100.0)
    assert view.open_positions == []
