import pytest

from core.execution.fill_simulator import FillSimulator
from core.execution.latency_simulator import LatencySimulator
from core.execution_model import ExecutionModel


def test_fill_matches_execution_model_math_directly():
    """Same fee/slippage numbers BacktestEngine's own tests already
    established — no second fee/slippage model, per spec decision #3."""
    execution_model = ExecutionModel(fee_bps=10.0, slippage_bps=5.0)
    latency_simulator = LatencySimulator(base_ms=0.0, jitter_ms=0.0)
    simulator = FillSimulator(execution_model, latency_simulator)

    result = simulator.simulate(reference_price=100.0, order_side=1, quantity=10.0)

    # Buy: slippage adverse (higher), matching ExecutionModel.fill() exactly.
    expected_fill_price = 100.0 * (1 + 5.0 / 10_000)
    expected_fee = expected_fill_price * 10.0 * (10.0 / 10_000)
    assert result.fill_price == pytest.approx(expected_fill_price)
    assert result.fee == pytest.approx(expected_fee)
    assert result.quantity == 10.0


def test_sell_slippage_is_adverse_in_the_opposite_direction():
    execution_model = ExecutionModel(fee_bps=0.0, slippage_bps=5.0)
    latency_simulator = LatencySimulator(base_ms=0.0, jitter_ms=0.0)
    simulator = FillSimulator(execution_model, latency_simulator)

    result = simulator.simulate(reference_price=100.0, order_side=-1, quantity=1.0)

    assert result.fill_price < 100.0  # sells fill lower, never higher


def test_latency_is_applied_and_recorded():
    execution_model = ExecutionModel(fee_bps=0.0, slippage_bps=0.0)
    latency_simulator = LatencySimulator(base_ms=75.0, jitter_ms=0.0)
    simulator = FillSimulator(execution_model, latency_simulator)

    result = simulator.simulate(reference_price=100.0, order_side=1, quantity=1.0)

    assert result.latency_ms == 75.0


def test_zero_fee_and_slippage_fills_at_exact_reference_price():
    execution_model = ExecutionModel(fee_bps=0.0, slippage_bps=0.0)
    latency_simulator = LatencySimulator(base_ms=0.0, jitter_ms=0.0)
    simulator = FillSimulator(execution_model, latency_simulator)

    result = simulator.simulate(reference_price=50.0, order_side=1, quantity=2.0)

    assert result.fill_price == 50.0
    assert result.fee == 0.0
