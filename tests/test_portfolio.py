import pytest

from core.execution_model import ExecutionModel
from core.portfolio import Portfolio


@pytest.fixture
def portfolio():
    # zero fees/slippage in most tests so PnL arithmetic is exact and checkable by hand
    return Portfolio(initial_capital=10_000.0, execution_model=ExecutionModel(fee_bps=0, slippage_bps=0))


def test_long_round_trip_profit(portfolio):
    portfolio.open_position(
        strategy_id="s1", direction=1, reference_price=100.0, quantity=10.0,
        entry_time="t0", stop_loss=90.0, take_profit=None, regime_at_entry="bull_trend",
    )
    portfolio.close_position("s1", reference_price=110.0, exit_time="t1", exit_reason="manual")

    trade = portfolio.trades[0]
    assert trade.pnl == pytest.approx(100.0)  # (110-100)*10
    assert portfolio.cash == pytest.approx(10_000.0 + 100.0)


def test_short_round_trip_profit(portfolio):
    portfolio.open_position(
        strategy_id="s1", direction=-1, reference_price=100.0, quantity=10.0,
        entry_time="t0", stop_loss=110.0, take_profit=None, regime_at_entry="bear_trend",
    )
    portfolio.close_position("s1", reference_price=90.0, exit_time="t1", exit_reason="manual")

    trade = portfolio.trades[0]
    assert trade.pnl == pytest.approx(100.0)  # price fell 10, short profits
    assert portfolio.cash == pytest.approx(10_000.0 + 100.0)


def test_long_round_trip_loss(portfolio):
    portfolio.open_position(
        strategy_id="s1", direction=1, reference_price=100.0, quantity=10.0,
        entry_time="t0", stop_loss=90.0, take_profit=None, regime_at_entry="bull_trend",
    )
    portfolio.close_position("s1", reference_price=95.0, exit_time="t1", exit_reason="stop_loss")

    trade = portfolio.trades[0]
    assert trade.pnl == pytest.approx(-50.0)  # (95-100)*10


def test_fees_reduce_pnl():
    portfolio = Portfolio(
        initial_capital=10_000.0,
        execution_model=ExecutionModel(fee_bps=10, slippage_bps=0),  # 0.1% each way
    )
    portfolio.open_position(
        strategy_id="s1", direction=1, reference_price=100.0, quantity=10.0,
        entry_time="t0", stop_loss=None, take_profit=None, regime_at_entry="bull_trend",
    )
    portfolio.close_position("s1", reference_price=110.0, exit_time="t1", exit_reason="manual")

    trade = portfolio.trades[0]
    gross = (110.0 - 100.0) * 10.0
    entry_fee = 100.0 * 10.0 * 0.001
    exit_fee = 110.0 * 10.0 * 0.001
    assert trade.pnl == pytest.approx(gross - entry_fee - exit_fee)


def test_r_multiple_computed_from_stop_distance(portfolio):
    portfolio.open_position(
        strategy_id="s1", direction=1, reference_price=100.0, quantity=10.0,
        entry_time="t0", stop_loss=95.0, take_profit=None, regime_at_entry="bull_trend",
    )
    portfolio.close_position("s1", reference_price=110.0, exit_time="t1", exit_reason="manual")

    trade = portfolio.trades[0]
    # risk was 5 (100-95), reward was 10 (110-100) -> 2R
    assert trade.r_multiple == pytest.approx(2.0)


def test_r_multiple_is_none_without_a_stop(portfolio):
    portfolio.open_position(
        strategy_id="s1", direction=1, reference_price=100.0, quantity=10.0,
        entry_time="t0", stop_loss=None, take_profit=None, regime_at_entry="bull_trend",
    )
    portfolio.close_position("s1", reference_price=110.0, exit_time="t1", exit_reason="manual")

    assert portfolio.trades[0].r_multiple is None


def test_mark_to_market_reflects_unrealized_long_pnl(portfolio):
    portfolio.open_position(
        strategy_id="s1", direction=1, reference_price=100.0, quantity=10.0,
        entry_time="t0", stop_loss=None, take_profit=None, regime_at_entry="bull_trend",
    )
    equity = portfolio.mark_to_market(timestamp="t0.5", current_price=105.0)
    assert equity == pytest.approx(10_000.0 + 50.0)  # unrealized (105-100)*10


def test_mark_to_market_reflects_unrealized_short_pnl(portfolio):
    portfolio.open_position(
        strategy_id="s1", direction=-1, reference_price=100.0, quantity=10.0,
        entry_time="t0", stop_loss=None, take_profit=None, regime_at_entry="bear_trend",
    )
    equity = portfolio.mark_to_market(timestamp="t0.5", current_price=95.0)
    assert equity == pytest.approx(10_000.0 + 50.0)  # price fell, short is up
