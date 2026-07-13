"""
Confidence is owned here, not by individual strategies. A strategy says:
"I see a long setup, signal_strength 0.7, for these reasons." This module
asks: historically, how often has THIS strategy, in THIS regime, at THIS
signal_strength range, actually worked? That answer is confidence.

Kept separate from strategy code so strategies stay pure, swappable, and
easy to reason about — and so confidence methodology can evolve (start
with historical win-rate lookups, later add multi-timeframe confirmation,
drawdown-adjusted scoring, liquidity checks) without touching a single
strategy implementation.
"""

from dataclasses import dataclass
from core.strategy_base import Signal, Regime


@dataclass
class ConfidenceReport:
    signal: Signal
    confidence: float              # 0-1, calibrated estimate
    sample_size: int               # how many historical instances this rests on
    basis: list[str]               # e.g. ["regime=bull_trend win_rate=0.61 n=842"]
    caveats: list[str]             # e.g. ["low sample size (n=23)"]


class ConfidenceEngine:
    def __init__(self, performance_store, regime_detector, min_sample_size=30):
        self.performance_store = performance_store   # historical signal outcomes
        self.regime_detector = regime_detector
        self.min_sample_size = min_sample_size

    def evaluate(self, signal: Signal, feature_window: "FeatureWindow") -> ConfidenceReport:
        state = self.regime_detector.classify(feature_window)

        history = self.performance_store.query(
            strategy_id=signal.strategy_id,
            regime=state.trend,
            vol_regime=state.vol,
            signal_strength_bucket=self._bucket(signal.signal_strength),
        )

        if history.sample_size < self.min_sample_size:
            return ConfidenceReport(
                signal=signal, confidence=0.0, sample_size=history.sample_size,
                basis=[f"insufficient history ({history.sample_size} samples)"],
                caveats=["not enough data to trust this setup yet"],
            )

        # v1: simple product of historical win rate and regime-detection
        # confidence. Deliberately simple to start — refine with
        # drawdown-adjustment and multi-timeframe agreement once there's
        # enough live/backtest data to validate a more complex formula
        # against, rather than guessing at weights now.
        confidence = history.win_rate * state.trend_confidence

        return ConfidenceReport(
            signal=signal,
            confidence=confidence,
            sample_size=history.sample_size,
            basis=[
                f"trend={state.trend.value} vol={state.vol.value} "
                f"win_rate={history.win_rate:.2f} n={history.sample_size}"
            ],
            caveats=[],
        )

    @staticmethod
    def _bucket(strength: float) -> str:
        return "high" if strength > 0.66 else "medium" if strength > 0.33 else "low"
