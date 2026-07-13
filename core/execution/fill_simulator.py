"""
Paper fill simulation. Reuses the EXISTING ExecutionModel
(core/execution_model.py) for fee/slippage — no second fee/slippage
model, per spec decision #3 — and layers a LatencySimulator delay on
top. Kept separate from PaperExecutionAdapter so the fill math is
independently testable without any Order/state-machine bookkeeping.
"""

from dataclasses import dataclass

from core.execution.latency_simulator import LatencySimulator
from core.execution_model import ExecutionModel


@dataclass
class SimulatedFill:
    fill_price: float
    quantity: float
    fee: float
    latency_ms: float


class FillSimulator:
    def __init__(self, execution_model: ExecutionModel, latency_simulator: LatencySimulator):
        self.execution_model = execution_model
        self.latency_simulator = latency_simulator

    def simulate(self, reference_price: float, order_side: int, quantity: float) -> SimulatedFill:
        """order_side: +1 buy, -1 sell — same convention as
        ExecutionModel.fill()'s order_side, NOT a position direction."""
        latency_ms = self.latency_simulator.delay()
        result = self.execution_model.fill(reference_price, order_side, quantity)
        return SimulatedFill(
            fill_price=result.fill_price, quantity=quantity, fee=result.fee, latency_ms=latency_ms
        )
