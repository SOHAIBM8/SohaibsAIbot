from datetime import UTC, datetime

import pytest

from core.feature_store import FeatureWindow
from core.portfolio import PortfolioView
from core.regime_detector import RegimeState
from core.risk.position_sizing_strategies import (
    FractionalKellySizer,
    PerformanceHistory,
    VolatilityAdjustedSizer,
)
from core.risk.rejection_reason import RejectionReason
from core.risk.risk_context import RiskContext
from core.strategy_base import Regime, Signal, VolRegime


def make_signal(entry_price=100.0, stop_loss=None, strategy_id="s1", signal_strength=0.5) -> Signal:
    return Signal(
        direction=1,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=None,
        strategy_id=strategy_id,
        signal_strength=signal_strength,
    )


def make_context(equity=10_000.0, feature_values=None) -> RiskContext:
    window = FeatureWindow(
        symbol="BTC/USDT", timeframe="1h", as_of=datetime.now(UTC), values=feature_values or {}
    )
    regime_state = RegimeState(
        trend=Regime.BULL_TREND, trend_confidence=0.8, vol=VolRegime.NORMAL_VOL, vol_confidence=0.5
    )
    portfolio_view = PortfolioView(
        equity=equity, peak_equity=equity, open_positions=[], trade_history=[]
    )
    return RiskContext(
        equity=equity,
        feature_window=window,
        regime_state=regime_state,
        portfolio_view=portfolio_view,
        data_quality_ok=True,
        data_quality_reason=None,
        as_of=window.as_of,
    )


# --- VolatilityAdjustedSizer -------------------------------------------------


def test_uses_strategys_own_stop_when_set():
    sizer = VolatilityAdjustedSizer(risk_fraction=0.02)
    signal = make_signal(entry_price=100.0, stop_loss=90.0)
    context = make_context(equity=10_000.0)

    quantity, reason = sizer.compute_base_quantity(signal, context)
    assert reason is None
    # risk_amount = 10,000 * 0.02 = 200; stop_distance = 10 -> quantity = 20
    assert quantity == pytest.approx(20.0)


def test_falls_back_to_atr_when_no_stop_set():
    sizer = VolatilityAdjustedSizer(risk_fraction=0.02, atr_multiplier=2.0)
    signal = make_signal(entry_price=100.0, stop_loss=None)
    context = make_context(equity=10_000.0, feature_values={"atr_14": 5.0})

    quantity, reason = sizer.compute_base_quantity(signal, context)
    assert reason is None
    # risk_amount = 200; stop_distance = 2.0 * 5.0 = 10 -> quantity = 20
    assert quantity == pytest.approx(20.0)


def test_rejects_when_no_stop_and_no_atr_feature():
    sizer = VolatilityAdjustedSizer(risk_fraction=0.02)
    signal = make_signal(stop_loss=None)
    context = make_context(feature_values={})

    quantity, reason = sizer.compute_base_quantity(signal, context)
    assert quantity == 0.0
    assert reason == RejectionReason.POSITION_SIZE_TOO_SMALL


def test_rejects_when_atr_is_zero():
    sizer = VolatilityAdjustedSizer(risk_fraction=0.02)
    signal = make_signal(stop_loss=None)
    context = make_context(feature_values={"atr_14": 0.0})

    quantity, reason = sizer.compute_base_quantity(signal, context)
    assert quantity == 0.0
    assert reason == RejectionReason.POSITION_SIZE_TOO_SMALL


def test_rejects_zero_equity():
    sizer = VolatilityAdjustedSizer(risk_fraction=0.02)
    signal = make_signal(stop_loss=90.0)
    context = make_context(equity=0.0)

    quantity, reason = sizer.compute_base_quantity(signal, context)
    assert quantity == 0.0
    assert reason == RejectionReason.POSITION_SIZE_TOO_SMALL


def test_rejects_zero_entry_price():
    sizer = VolatilityAdjustedSizer(risk_fraction=0.02)
    signal = make_signal(entry_price=0.0, stop_loss=90.0)
    context = make_context()

    quantity, reason = sizer.compute_base_quantity(signal, context)
    assert quantity == 0.0
    assert reason == RejectionReason.POSITION_SIZE_TOO_SMALL


# --- FractionalKellySizer: hand-computed (p, b) pairs -----------------------


@pytest.mark.parametrize(
    "p, b, expected",
    [
        (0.6, 2.0, 0.4),  # (2*0.6 - 0.4) / 2 = 0.8/2 = 0.4
        (0.5, 1.0, 0.0),  # (1*0.5 - 0.5) / 1 = 0.0 (breakeven edge)
        (0.4, 1.0, 0.0),  # (1*0.4 - 0.6) / 1 = -0.2 -> clamped to 0.0 (negative edge)
        (0.7, 0.5, 0.1),  # (0.5*0.7 - 0.3) / 0.5 = 0.05/0.5 = 0.1
        (0.9, 0.0, 0.0),  # b <= 0 -> always 0.0, regardless of p
    ],
)
def test_kelly_fraction_hand_computed_values(p, b, expected):
    assert FractionalKellySizer.kelly_fraction(p, b) == pytest.approx(expected)


class _FakePerformanceStore:
    def __init__(self, history: PerformanceHistory):
        self._history = history
        self.last_query_kwargs: dict | None = None

    def query(self, **kwargs) -> PerformanceHistory:
        self.last_query_kwargs = kwargs
        return self._history


def test_insufficient_sample_size_returns_zero_and_exact_rejection_reason():
    store = _FakePerformanceStore(
        PerformanceHistory(sample_size=10, win_rate=0.7, avg_win_loss_ratio=2.0)
    )
    sizer = FractionalKellySizer(store, kelly_fraction_multiplier=1.0, kelly_min_sample_size=30)
    signal = make_signal(stop_loss=90.0)
    context = make_context()

    quantity, reason = sizer.compute_base_quantity(signal, context)
    assert quantity == 0.0
    assert reason == RejectionReason.INSUFFICIENT_SAMPLE_FOR_KELLY


def test_sufficient_sample_computes_quantity_scaled_by_fraction_multiplier():
    # p=0.6, b=2.0 -> f* = 0.4; multiplier 0.5 -> effective fraction 0.2
    store = _FakePerformanceStore(
        PerformanceHistory(sample_size=50, win_rate=0.6, avg_win_loss_ratio=2.0)
    )
    sizer = FractionalKellySizer(store, kelly_fraction_multiplier=0.5, kelly_min_sample_size=30)
    signal = make_signal(entry_price=100.0, stop_loss=90.0)
    context = make_context(equity=10_000.0)

    quantity, reason = sizer.compute_base_quantity(signal, context)
    assert reason is None
    # risk_amount = 10,000 * 0.2 = 2,000; stop_distance = 10 -> quantity = 200
    assert quantity == pytest.approx(200.0)


def test_zero_edge_rejects_as_position_size_too_small():
    store = _FakePerformanceStore(
        PerformanceHistory(sample_size=50, win_rate=0.4, avg_win_loss_ratio=1.0)
    )
    sizer = FractionalKellySizer(store, kelly_fraction_multiplier=1.0, kelly_min_sample_size=30)
    signal = make_signal(stop_loss=90.0)
    context = make_context()

    quantity, reason = sizer.compute_base_quantity(signal, context)
    assert quantity == 0.0
    assert reason == RejectionReason.POSITION_SIZE_TOO_SMALL


def test_queries_performance_store_with_regime_and_bucket_from_context():
    store = _FakePerformanceStore(
        PerformanceHistory(sample_size=50, win_rate=0.6, avg_win_loss_ratio=2.0)
    )
    sizer = FractionalKellySizer(store, kelly_fraction_multiplier=1.0, kelly_min_sample_size=30)
    signal = make_signal(strategy_id="ema_cross@1.0.0", signal_strength=0.9, stop_loss=90.0)
    context = make_context()

    sizer.compute_base_quantity(signal, context)

    assert store.last_query_kwargs == {
        "strategy_id": "ema_cross@1.0.0",
        "regime": Regime.BULL_TREND,
        "vol_regime": VolRegime.NORMAL_VOL,
        "signal_strength_bucket": "high",  # 0.9 > 0.66
    }
