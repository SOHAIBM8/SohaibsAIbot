"""
Position sizing is a pluggable interface, not logic baked into the
backtest engine. The Risk Engine (core/risk/risk_engine.py) is the
real implementation of this interface, with portfolio-level exposure
caps and volatility-adjusted sizing. Neither the backtest engine nor
any strategy needs to change when a different sizer is swapped in —
that's the whole point of the hook.

BREAKING CHANGE (docs/risk_engine_spec.md section 2, step 10): size()
used to take (signal, equity, feature_window) and return a bare float.
A real Risk Engine can't enforce portfolio-level exposure, drawdown, or
correlation limits while blind to every other open position — a scalar
equity number isn't enough context. The interface now takes a
RiskContext (equity, feature_window, regime_state, a read-only
PortfolioView, data-quality status, and a timestamp) and returns a
SizingDecision (an approved quantity plus the full audit trail of why),
not a bare float.
"""

from abc import ABC, abstractmethod

from core.risk.risk_context import RiskContext
from core.risk.risk_decision import SizingDecision
from core.strategy_base import Signal


class PositionSizer(ABC):
    @abstractmethod
    def size(self, signal: Signal, context: RiskContext) -> SizingDecision:
        """Return a SizingDecision. approved_quantity == 0.0 vetoes the
        trade entirely — this is how the Risk Engine enforces hard
        limits without callers needing a separate 'is this allowed'
        check."""
        ...


class FixedFractionSizer(PositionSizer):
    """
    Risks a fixed fraction of current equity per trade, sized off the
    strategy's own stop-loss distance when one is declared. Explicitly
    a V1 stand-in, NOT real risk management — no portfolio-level
    exposure awareness (nothing stops five strategies from each risking
    1% simultaneously for 5% total), no volatility adjustment. That gap
    is exactly what RiskEngine closes. Kept as the deliberately naive
    baseline for comparison experiments, not deprecated — wraps its
    single float answer in a SizingDecision with empty layer_results,
    since it doesn't run a multi-layer pipeline.
    """

    def __init__(self, risk_fraction: float = 0.01):
        self.risk_fraction = risk_fraction

    def size(self, signal: Signal, context: RiskContext) -> SizingDecision:
        equity = context.equity
        if equity <= 0 or signal.entry_price <= 0:
            return SizingDecision(approved_quantity=0.0, proposed_quantity=0.0)

        risk_amount = equity * self.risk_fraction
        if signal.stop_loss is not None and signal.entry_price != signal.stop_loss:
            risk_per_unit = abs(signal.entry_price - signal.stop_loss)
            quantity = risk_amount / risk_per_unit
        else:
            # No stop declared: fall back to a flat notional fraction so
            # the backtest can still run. This is precisely the
            # undersized-risk-control case the real Risk Engine must
            # close off — a strategy that never sets a stop shouldn't
            # get sized at all in production.
            quantity = risk_amount / signal.entry_price

        return SizingDecision(approved_quantity=quantity, proposed_quantity=quantity)
