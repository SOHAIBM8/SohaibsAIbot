"""
Demonstrates the actual integration point: this is what one bar of a
real backtest engine loop looks like once regime detection exists.
(The full BacktestEngine class is a later component — this test is
the contract it will be built against.)

Loop shape, per bar:
    1. Build a FeatureWindow from precomputed features
    2. state = regime_detector.classify(window)     <- this component
    3. eligible = strategy_registry.get_candidates_for_regime(state)
    4. for strategy in eligible: signal = strategy.generate_signal(window)
    5. if signal.direction != 0: confidence_report = confidence_engine.evaluate(signal, window)
    6. log everything to signal_log regardless of whether it was acted on
"""

from core.feature_store import FeatureWindow
from core.regime_config import RegimeDetectorConfig
from core.regime_detector import RegimeDetector
from core.strategy_base import Regime, StrategyMeta, Signal, VolRegime, StrategyBase
from core.strategy_registry import StrategyRegistry


class _StubTrendStrategy(StrategyBase):
    """A minimal strategy stand-in, avoiding a dependency on the real
    plugin discovery mechanism (that's covered by strategy_registry's
    own tests) — this test is about the regime -> eligibility wiring."""
    meta = StrategyMeta(
        name="stub_trend", version="1.0.0", author="test",
        created_at=None, description="", parameters={},
        compatible_pipeline_versions=["features_v1"],
        works_best_in=[Regime.BULL_TREND],
    )
    required_features = []
    min_lookback = 0

    def generate_signal(self, feature_window) -> Signal:
        return Signal(
            direction=1, entry_price=100, stop_loss=None, take_profit=None,
            strategy_id=self.meta.strategy_id, signal_strength=0.8,
        )

    def validate(self, feature_registry):
        return []  # skip feature validation for this stub


class _StubSidewaysStrategy(StrategyBase):
    meta = StrategyMeta(
        name="stub_sideways", version="1.0.0", author="test",
        created_at=None, description="", parameters={},
        compatible_pipeline_versions=["features_v1"],
        works_best_in=[Regime.SIDEWAYS],
    )
    required_features = []
    min_lookback = 0

    def generate_signal(self, feature_window) -> Signal:
        return Signal(
            direction=0, entry_price=100, stop_loss=None, take_profit=None,
            strategy_id=self.meta.strategy_id, signal_strength=0.0,
        )

    def validate(self, feature_registry):
        return []


def _window(ema_20=110, ema_50=100, adx=30.0, atr_pct=0.5):
    return FeatureWindow(
        symbol="BTCUSDT", timeframe="1h", as_of="t",
        values={"ema_20": ema_20, "ema_50": ema_50, "adx_14": adx, "atr_percentile_90": atr_pct},
    )


def test_only_regime_matching_strategies_are_eligible():
    detector = RegimeDetector(RegimeDetectorConfig(min_confirmation_bars=1))
    registry = StrategyRegistry(feature_registry=None)
    registry._strategies = {  # bypass discover() — inject directly for this test
        "stub_trend@1.0.0": _StubTrendStrategy(),
        "stub_sideways@1.0.0": _StubSidewaysStrategy(),
    }

    # Strong bull trend bar
    state = detector.classify(_window(adx=30.0))
    eligible = registry.get_candidates_for_regime(state)

    assert len(eligible) == 1
    assert eligible[0].meta.strategy_id == "stub_trend@1.0.0"


def test_eligibility_switches_when_confirmed_regime_changes():
    detector = RegimeDetector(RegimeDetectorConfig(min_confirmation_bars=1))
    registry = StrategyRegistry(feature_registry=None)
    registry._strategies = {
        "stub_trend@1.0.0": _StubTrendStrategy(),
        "stub_sideways@1.0.0": _StubSidewaysStrategy(),
    }

    bull_state = detector.classify(_window(adx=30.0))
    bull_eligible = {s.meta.strategy_id for s in registry.get_candidates_for_regime(bull_state)}

    sideways_state = detector.classify(_window(adx=5.0))
    sideways_eligible = {s.meta.strategy_id for s in registry.get_candidates_for_regime(sideways_state)}

    assert bull_eligible == {"stub_trend@1.0.0"}
    assert sideways_eligible == {"stub_sideways@1.0.0"}


def test_only_eligible_strategies_would_be_asked_for_signals():
    """The point of the whole gate: an ineligible strategy is never
    even called for generate_signal in the real engine loop."""
    detector = RegimeDetector(RegimeDetectorConfig(min_confirmation_bars=1))
    registry = StrategyRegistry(feature_registry=None)
    registry._strategies = {"stub_trend@1.0.0": _StubTrendStrategy()}

    sideways_state = detector.classify(_window(adx=5.0))
    eligible = registry.get_candidates_for_regime(sideways_state)

    assert eligible == []  # engine loop would skip calling generate_signal entirely
