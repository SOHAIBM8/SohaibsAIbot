"""
PositionSizingStrategy is internal to RiskEngine — NOT the same
interface BacktestEngine calls (that's the widened PositionSizer in
core/position_sizing.py, arriving in step 10). A strategy here computes
only a base quantity; RiskEngine applies every throttle/cap on top of
it (drawdown tier multiplier, exposure limits, the hard per-trade cap)
in its own five-layer pipeline.

Design notes (rule 9 — gaps in the spec, filled in and flagged here):

1. RiskConfig has no generic "risk_fraction" field for
   VolatilityAdjustedSizer (unlike kelly_fraction_multiplier for
   Kelly). Matching FixedFractionSizer's existing pattern
   (core/position_sizing.py), risk_fraction is a constructor param
   supplied by whoever wires up the sizing strategy, not sourced from
   RiskConfig.

2. Neither sizer's ATR-based stop_distance formula specifies the ATR
   multiplier ("k * atr_14"). RiskConfig has no field for it either.
   Both sizers take atr_multiplier as a constructor param, defaulting
   to 2.0 (a conventional "2x ATR" stop distance).

3. FractionalKellySizer's docstring says it sources stats from "the
   SAME... historical stats ConfidenceEngine already computes" — but
   ConfidenceEngine.evaluate() only exposes win_rate and sample_size
   (via an untyped, undocumented, and (as of this writing) never
   concretely implemented `performance_store.query()` dependency); the
   Kelly formula f*=(b*p-q)/b additionally needs b (average win/loss
   ratio), which nothing in the codebase currently returns.
   `PerformanceHistory` below extends that duck-typed contract with
   `avg_win_loss_ratio` — ConfidenceEngine is NOT modified; both
   classes now document the same expected shape for whatever concrete
   PerformanceStore eventually gets built. FractionalKellySizer
   queries performance_store directly (same call shape as
   ConfidenceEngine.evaluate, including the identical signal_strength
   bucketing) rather than going through ConfidenceEngine, since
   ConfidenceEngine returns a confidence SCORE, not the raw (p, b)
   Kelly needs.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

from core.risk.rejection_reason import RejectionReason
from core.risk.risk_context import RiskContext
from core.strategy_base import Regime, Signal, VolRegime


class PositionSizingStrategy(ABC):
    @abstractmethod
    def compute_base_quantity(
        self, signal: Signal, context: RiskContext
    ) -> tuple[float, RejectionReason | None]: ...


def _stop_distance(signal: Signal, context: RiskContext, atr_multiplier: float) -> float | None:
    """Risk-per-unit distance used to convert a risk fraction into a
    quantity: the strategy's own stop if it set one, else
    atr_multiplier * atr_14 from the feature window. None if neither is
    available — callers must treat that as "can't size this," never
    divide by zero."""
    if signal.stop_loss is not None and signal.entry_price != signal.stop_loss:
        return abs(signal.entry_price - signal.stop_loss)
    try:
        atr: float = context.feature_window.get("atr_14")
    except KeyError:
        return None
    if atr <= 0:
        return None
    return atr_multiplier * atr


class VolatilityAdjustedSizer(PositionSizingStrategy):
    """risk_amount = equity * risk_fraction. stop_distance = strategy's
    own stop if set, else atr_multiplier * atr_14 from
    context.feature_window. quantity = risk_amount / stop_distance."""

    def __init__(self, risk_fraction: float = 0.01, atr_multiplier: float = 2.0):
        self.risk_fraction = risk_fraction
        self.atr_multiplier = atr_multiplier

    def compute_base_quantity(
        self, signal: Signal, context: RiskContext
    ) -> tuple[float, RejectionReason | None]:
        if context.equity <= 0 or signal.entry_price <= 0:
            return 0.0, RejectionReason.POSITION_SIZE_TOO_SMALL

        stop_distance = _stop_distance(signal, context, self.atr_multiplier)
        if stop_distance is None:
            return 0.0, RejectionReason.POSITION_SIZE_TOO_SMALL

        risk_amount = context.equity * self.risk_fraction
        return risk_amount / stop_distance, None


@dataclass
class PerformanceHistory:
    """Duck-typed shape expected from performance_store.query() — see
    the module docstring's design note #3."""

    sample_size: int
    win_rate: float
    avg_win_loss_ratio: float  # b: average win size / average loss size


class PerformanceStore(Protocol):
    def query(
        self,
        *,
        strategy_id: str,
        regime: Regime,
        vol_regime: VolRegime,
        signal_strength_bucket: str,
    ) -> PerformanceHistory: ...


class FractionalKellySizer(PositionSizingStrategy):
    """f* = (b*p - q) / b, where p = win_rate, q = 1-p, b =
    avg_win_loss_ratio — sourced from the same regime-conditioned,
    sample-size-gated historical stats ConfidenceEngine relies on, not
    a separate performance history this class maintains itself. Falls
    back to (0.0, INSUFFICIENT_SAMPLE_FOR_KELLY) below
    kelly_min_sample_size — never guesses with thin data (spec decision
    #4). f* is always scaled by kelly_fraction_multiplier before use —
    never full, unfractional Kelly."""

    def __init__(
        self,
        performance_store: PerformanceStore,
        kelly_fraction_multiplier: float,
        kelly_min_sample_size: int,
        atr_multiplier: float = 2.0,
    ):
        self.performance_store = performance_store
        self.kelly_fraction_multiplier = kelly_fraction_multiplier
        self.kelly_min_sample_size = kelly_min_sample_size
        self.atr_multiplier = atr_multiplier

    def compute_base_quantity(
        self, signal: Signal, context: RiskContext
    ) -> tuple[float, RejectionReason | None]:
        history = self.performance_store.query(
            strategy_id=signal.strategy_id,
            regime=context.regime_state.trend,
            vol_regime=context.regime_state.vol,
            signal_strength_bucket=self._bucket(signal.signal_strength),
        )
        if history.sample_size < self.kelly_min_sample_size:
            return 0.0, RejectionReason.INSUFFICIENT_SAMPLE_FOR_KELLY

        f_star = self.kelly_fraction(history.win_rate, history.avg_win_loss_ratio)
        f_star *= self.kelly_fraction_multiplier
        if f_star <= 0 or context.equity <= 0 or signal.entry_price <= 0:
            return 0.0, RejectionReason.POSITION_SIZE_TOO_SMALL

        stop_distance = _stop_distance(signal, context, self.atr_multiplier)
        if stop_distance is None:
            return 0.0, RejectionReason.POSITION_SIZE_TOO_SMALL

        risk_amount = context.equity * f_star
        return risk_amount / stop_distance, None

    @staticmethod
    def kelly_fraction(p: float, b: float) -> float:
        """f* = (b*p - q) / b. Clamped to 0.0 (never negative — a
        negative Kelly fraction means 'don't take this bet,' not 'bet
        negative size') and when b <= 0 (no meaningful edge ratio)."""
        if b <= 0:
            return 0.0
        q = 1.0 - p
        f_star = (b * p - q) / b
        return max(f_star, 0.0)

    @staticmethod
    def _bucket(strength: float) -> str:
        # Matches ConfidenceEngine._bucket exactly, intentionally
        # duplicated rather than imported (a private staticmethod of
        # another class) — Kelly sizing and confidence scoring must
        # always query the same historical buckets.
        return "high" if strength > 0.66 else "medium" if strength > 0.33 else "low"
