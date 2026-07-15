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

Wired into `core/backtest_engine.py` (CLAUDE.md "What's NOT built yet" —
this module previously had zero callers). Design fix made during that
wiring, not a silent deviation (rule 9): `evaluate()` originally took a
`FeatureWindow` and classified regime itself via its own
`RegimeDetector` instance. `RegimeDetector` is explicitly stateful
(hysteresis) and must be called exactly once per bar in strict
chronological order — a second, independent `classify()` call here
would let this engine's hysteresis state silently diverge from the
regime `BacktestEngine` actually used to decide strategy eligibility
and generate the signal in the first place, so confidence could end up
scored against a different regime than the one that produced the
signal. Fixed by having `evaluate()` accept the already-computed
`RegimeState` directly — the same value `BacktestEngine` already holds
for this bar — mirroring how `RiskContext` already carries a
pre-computed `regime_state` rather than every risk layer re-deriving
it. `ConfidenceEngine` no longer owns a `RegimeDetector` at all.
"""

from dataclasses import dataclass
from typing import Protocol

from core.regime_detector import RegimeState
from core.strategy_base import Regime, Signal, VolRegime


class PerformanceHistory(Protocol):
    """Shape ConfidenceEngine needs back from a performance store query
    — narrow on purpose, matching this project's established Protocol
    pattern (e.g. core/risk/position_sizing_strategies.py's own
    PerformanceStore) rather than depending on a concrete store type
    that doesn't exist yet."""

    sample_size: int
    win_rate: float


class PerformanceStore(Protocol):
    def query(
        self,
        strategy_id: str,
        regime: Regime,
        vol_regime: VolRegime,
        signal_strength_bucket: str,
    ) -> PerformanceHistory: ...


@dataclass
class ConfidenceReport:
    signal: Signal
    confidence: float  # 0-1, calibrated estimate
    sample_size: int  # how many historical instances this rests on
    basis: list[str]  # e.g. ["regime=bull_trend win_rate=0.61 n=842"]
    caveats: list[str]  # e.g. ["low sample size (n=23)"]


class ConfidenceEngine:
    def __init__(
        self,
        performance_store: PerformanceStore,
        min_sample_size: int = 30,
    ) -> None:
        self.performance_store = performance_store  # historical signal outcomes
        self.min_sample_size = min_sample_size

    def evaluate(self, signal: Signal, regime_state: RegimeState) -> ConfidenceReport:
        history = self.performance_store.query(
            strategy_id=signal.strategy_id,
            regime=regime_state.trend,
            vol_regime=regime_state.vol,
            signal_strength_bucket=self._bucket(signal.signal_strength),
        )

        if history.sample_size < self.min_sample_size:
            return ConfidenceReport(
                signal=signal,
                confidence=0.0,
                sample_size=history.sample_size,
                basis=[f"insufficient history ({history.sample_size} samples)"],
                caveats=["not enough data to trust this setup yet"],
            )

        # v1: simple product of historical win rate and regime-detection
        # confidence. Deliberately simple to start — refine with
        # drawdown-adjustment and multi-timeframe agreement once there's
        # enough live/backtest data to validate a more complex formula
        # against, rather than guessing at weights now.
        confidence = history.win_rate * regime_state.trend_confidence

        return ConfidenceReport(
            signal=signal,
            confidence=confidence,
            sample_size=history.sample_size,
            basis=[
                f"trend={regime_state.trend.value} vol={regime_state.vol.value} "
                f"win_rate={history.win_rate:.2f} n={history.sample_size}"
            ],
            caveats=[],
        )

    @staticmethod
    def _bucket(strength: float) -> str:
        return "high" if strength > 0.66 else "medium" if strength > 0.33 else "low"
