"""
Fee and slippage simulation, kept as its own pluggable component so
different assumptions (per-exchange fee schedules, a volume-based
slippage model later) can be swapped in without touching the backtest
loop itself.
"""

from dataclasses import dataclass


@dataclass
class FillResult:
    fill_price: float
    fee: float


class ExecutionModel:
    """
    V1: flat basis-point fee on notional, flat basis-point slippage
    that is always adverse to the trader (buy orders fill higher,
    sell orders fill lower than the reference price). This is a
    simplification — real slippage scales with order size relative to
    book depth — but it's the right DIRECTION of simplification: a
    fixed adverse cost will never make a backtest look better than
    live trading would, which is the error direction you want when
    the two inevitably disagree.
    """

    def __init__(self, fee_bps: float = 10.0, slippage_bps: float = 5.0):
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps

    def fill(self, reference_price: float, order_side: int, quantity: float) -> FillResult:
        """order_side: +1 for a buy action, -1 for a sell action. This is
        the actual order side, NOT the resulting position direction —
        closing a long is a sell (-1) even though the position was +1."""
        slippage_factor = 1 + (self.slippage_bps / 10_000) * (1 if order_side > 0 else -1)
        fill_price = reference_price * slippage_factor
        notional = fill_price * abs(quantity)
        fee = notional * (self.fee_bps / 10_000)
        return FillResult(fill_price=fill_price, fee=fee)
