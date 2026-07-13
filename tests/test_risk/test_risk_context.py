from datetime import UTC, datetime

from core.feature_store import FeatureWindow
from core.regime_detector import RegimeState
from core.risk.rejection_reason import RejectionReason, ThrottleReason
from core.risk.risk_context import RiskContext
from core.risk.risk_decision import LayerResult, SizingDecision
from core.strategy_base import Regime, VolRegime


def _regime_state() -> RegimeState:
    return RegimeState(
        trend=Regime.BULL_TREND,
        trend_confidence=0.8,
        vol=VolRegime.NORMAL_VOL,
        vol_confidence=0.5,
        reasons=["ema20>ema50, adx=25.0"],
    )


def test_risk_context_holds_all_fields():
    window = FeatureWindow(symbol="BTC/USDT", timeframe="1h", as_of=datetime.now(UTC), values={})
    from core.portfolio import PortfolioView

    portfolio_view = PortfolioView(
        equity=10_000.0, peak_equity=10_000.0, open_positions=[], trade_history=[]
    )

    context = RiskContext(
        equity=10_000.0,
        feature_window=window,
        regime_state=_regime_state(),
        portfolio_view=portfolio_view,
        data_quality_ok=True,
        data_quality_reason=None,
        as_of=window.as_of,
    )

    assert context.equity == 10_000.0
    assert context.feature_window is window
    assert context.regime_state.trend == Regime.BULL_TREND
    assert context.portfolio_view is portfolio_view
    assert context.data_quality_ok is True
    assert context.data_quality_reason is None


def test_sizing_decision_defaults_to_empty_throttles_and_layers():
    decision = SizingDecision(approved_quantity=1.5, proposed_quantity=1.5)
    assert decision.rejection_reason is None
    assert decision.throttle_reasons == []
    assert decision.layer_results == []


def test_sizing_decision_holds_rejection_and_throttles():
    decision = SizingDecision(
        approved_quantity=0.0,
        proposed_quantity=2.0,
        rejection_reason=RejectionReason.KILL_SWITCH_ACTIVE,
        throttle_reasons=[ThrottleReason.DRAWDOWN_TIER_REDUCTION],
        layer_results=[
            LayerResult(layer_name="gate", passed=False, multiplier=0.0, reason="kill switch")
        ],
    )
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.KILL_SWITCH_ACTIVE
    assert decision.throttle_reasons == [ThrottleReason.DRAWDOWN_TIER_REDUCTION]
    assert decision.layer_results[0].layer_name == "gate"
    assert decision.layer_results[0].passed is False
