"""
Position sizing is a pluggable interface, not logic baked into the
backtest engine. V1 ships a simple fixed-fractional sizer as a stand-in;
the Risk Engine (a later phase) will implement this same interface with
portfolio-level exposure caps and volatility-adjusted sizing. Neither
the backtest engine nor any strategy needs to change when that happens
— this is the whole point of building the hook now.
"""

from abc import ABC, abstractmethod

from core.feature_store import FeatureWindow
from core.strategy_base import Signal


class PositionSizer(ABC):
    @abstractmethod
    def size(self, signal: Signal, equity: float, feature_window: FeatureWindow) -> float:
        """Return quantity (units of the asset) to trade. Returning 0.0
        vetoes the trade entirely — this is how the future Risk Engine
        enforces hard limits without callers needing a separate
        'is this allowed' check."""
        ...


class FixedFractionSizer(PositionSizer):
    """
    Risks a fixed fraction of current equity per trade, sized off the
    strategy's own stop-loss distance when one is declared. Explicitly
    a V1 stand-in, NOT real risk management — no portfolio-level
    exposure awareness (nothing stops five strategies from each risking
    1% simultaneously for 5% total), no volatility adjustment. That gap
    is exactly what the Risk Engine phase exists to close.
    """

    def __init__(self, risk_fraction: float = 0.01):
        self.risk_fraction = risk_fraction

    def size(self, signal: Signal, equity: float, feature_window: FeatureWindow) -> float:
        if equity <= 0 or signal.entry_price <= 0:
            return 0.0
        risk_amount = equity * self.risk_fraction
        if signal.stop_loss is not None and signal.entry_price != signal.stop_loss:
            risk_per_unit = abs(signal.entry_price - signal.stop_loss)
            return risk_amount / risk_per_unit
        # No stop declared: fall back to a flat notional fraction so the
        # backtest can still run. This is precisely the undersized-risk-
        # control case the real Risk Engine must close off — a strategy
        # that never sets a stop shouldn't get sized at all in production.
        return risk_amount / signal.entry_price
