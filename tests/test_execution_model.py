import pytest

from core.execution_model import ExecutionModel


def test_buy_fills_above_reference_price():
    model = ExecutionModel(fee_bps=0, slippage_bps=10)
    fill = model.fill(reference_price=100.0, order_side=1, quantity=1.0)
    assert fill.fill_price > 100.0


def test_sell_fills_below_reference_price():
    model = ExecutionModel(fee_bps=0, slippage_bps=10)
    fill = model.fill(reference_price=100.0, order_side=-1, quantity=1.0)
    assert fill.fill_price < 100.0


def test_fee_scales_with_notional():
    model = ExecutionModel(fee_bps=10, slippage_bps=0)  # 0.1%
    fill = model.fill(reference_price=100.0, order_side=1, quantity=2.0)
    assert fill.fee == pytest.approx(100.0 * 2.0 * 0.001)


def test_zero_slippage_means_exact_reference_price():
    model = ExecutionModel(fee_bps=0, slippage_bps=0)
    fill = model.fill(reference_price=100.0, order_side=1, quantity=1.0)
    assert fill.fill_price == 100.0
    assert fill.fee == 0.0
