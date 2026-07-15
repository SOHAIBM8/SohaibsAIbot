"""
core/confidence_engine.py's first real test coverage — previously had
none at all (CLAUDE.md "What's NOT built yet"). Pure unit tests: a fake
PerformanceStore is enough since ConfidenceEngine itself has no I/O of
its own — real Postgres coverage for the concrete PerformanceStore
implementation lives in tests/test_signal_performance_store.py.
"""

from dataclasses import dataclass

from core.confidence_engine import ConfidenceEngine
from core.regime_detector import RegimeState
from core.strategy_base import Regime, Signal, VolRegime


@dataclass
class _FakeHistory:
    sample_size: int
    win_rate: float


class _FakePerformanceStore:
    def __init__(self, history: _FakeHistory, expected_query: dict | None = None):
        self._history = history
        self._expected_query = expected_query
        self.calls: list[dict] = []

    def query(self, strategy_id, regime, vol_regime, signal_strength_bucket):
        call = {
            "strategy_id": strategy_id,
            "regime": regime,
            "vol_regime": vol_regime,
            "signal_strength_bucket": signal_strength_bucket,
        }
        self.calls.append(call)
        if self._expected_query is not None:
            assert call == self._expected_query
        return self._history


def make_signal(strategy_id="s1@1.0.0", signal_strength=0.5, direction=1) -> Signal:
    return Signal(
        direction=direction,
        entry_price=100.0,
        stop_loss=None,
        take_profit=None,
        strategy_id=strategy_id,
        signal_strength=signal_strength,
        reasons=["test"],
    )


def make_regime_state(trend=Regime.BULL_TREND, trend_confidence=0.8, vol=VolRegime.NORMAL_VOL):
    return RegimeState(
        trend=trend, trend_confidence=trend_confidence, vol=vol, vol_confidence=0.5, reasons=[]
    )


def test_insufficient_sample_size_returns_zero_confidence():
    store = _FakePerformanceStore(_FakeHistory(sample_size=5, win_rate=0.9))
    engine = ConfidenceEngine(performance_store=store, min_sample_size=30)

    report = engine.evaluate(make_signal(), make_regime_state())

    assert report.confidence == 0.0
    assert report.sample_size == 5
    assert "insufficient history" in report.basis[0]
    assert report.caveats == ["not enough data to trust this setup yet"]


def test_sufficient_sample_size_computes_confidence_as_win_rate_times_trend_confidence():
    store = _FakePerformanceStore(_FakeHistory(sample_size=100, win_rate=0.6))
    engine = ConfidenceEngine(performance_store=store, min_sample_size=30)

    report = engine.evaluate(make_signal(), make_regime_state(trend_confidence=0.5))

    assert report.confidence == 0.6 * 0.5
    assert report.sample_size == 100
    assert report.caveats == []
    assert "win_rate=0.60" in report.basis[0]


def test_exactly_at_min_sample_size_is_sufficient_not_insufficient():
    store = _FakePerformanceStore(_FakeHistory(sample_size=30, win_rate=1.0))
    engine = ConfidenceEngine(performance_store=store, min_sample_size=30)

    report = engine.evaluate(make_signal(), make_regime_state(trend_confidence=1.0))

    assert report.confidence == 1.0
    assert report.caveats == []


def test_query_is_built_from_the_signal_and_regime_state_not_hardcoded():
    signal = make_signal(strategy_id="ema_cross@1.0.0", signal_strength=0.9)
    regime_state = make_regime_state(trend=Regime.BEAR_TREND, vol=VolRegime.HIGH_VOL)
    store = _FakePerformanceStore(
        _FakeHistory(sample_size=0, win_rate=0.0),
        expected_query={
            "strategy_id": "ema_cross@1.0.0",
            "regime": Regime.BEAR_TREND,
            "vol_regime": VolRegime.HIGH_VOL,
            "signal_strength_bucket": "high",
        },
    )
    engine = ConfidenceEngine(performance_store=store)

    engine.evaluate(signal, regime_state)

    assert len(store.calls) == 1


def test_bucket_thresholds_match_the_documented_boundaries():
    store = _FakePerformanceStore(_FakeHistory(sample_size=0, win_rate=0.0))
    engine = ConfidenceEngine(performance_store=store)
    regime_state = make_regime_state()

    engine.evaluate(make_signal(signal_strength=0.2), regime_state)
    engine.evaluate(make_signal(signal_strength=0.5), regime_state)
    engine.evaluate(make_signal(signal_strength=0.9), regime_state)
    engine.evaluate(make_signal(signal_strength=0.66), regime_state)  # boundary: not > 0.66
    engine.evaluate(make_signal(signal_strength=0.33), regime_state)  # boundary: not > 0.33

    buckets = [c["signal_strength_bucket"] for c in store.calls]
    assert buckets == ["low", "medium", "high", "medium", "low"]


def test_does_not_call_a_regime_detector_itself():
    """Design fix (see module docstring): ConfidenceEngine must never
    independently classify regime — it must use exactly the RegimeState
    the caller already computed, so it can't diverge from the regime
    that actually drove strategy eligibility/signal generation. This is
    structurally guaranteed by evaluate()'s signature no longer
    accepting a FeatureWindow at all."""
    import inspect

    params = inspect.signature(ConfidenceEngine.evaluate).parameters
    assert "feature_window" not in params
    assert "regime_state" in params
