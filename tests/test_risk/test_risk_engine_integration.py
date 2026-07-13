"""
Full five-layer RiskEngine pipeline against real Postgres (risk_config,
kill_switch_state, circuit_breaker_event_log, risk_decision_log — not
mocked). One test per RejectionReason value, per the spec's testing
strategy, plus property-style invariant tests over randomized inputs.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.feature_store import FeatureWindow
from core.ingestion.event_bus import PostgresEventBus
from core.portfolio import PortfolioView, PositionView, Trade
from core.regime_detector import RegimeState
from core.risk.circuit_breaker import CircuitBreaker
from core.risk.drawdown_monitor import DrawdownMonitor
from core.risk.exposure_tracker import ExposureTracker
from core.risk.kill_switch import KillSwitch
from core.risk.loss_limit_tracker import LossLimitTracker
from core.risk.position_sizing_strategies import (
    FractionalKellySizer,
    PerformanceHistory,
    VolatilityAdjustedSizer,
)
from core.risk.rejection_reason import RejectionReason
from core.risk.risk_config import RiskConfig
from core.risk.risk_context import RiskContext
from core.risk.risk_engine import RiskEngine
from core.strategy_base import Regime, Signal, VolRegime

SCOPE = "test_risk_engine"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM risk_decision_log WHERE strategy_id LIKE 'test_%'"))
        session.execute(text(f"DELETE FROM kill_switch_state WHERE scope = '{SCOPE}'"))
        session.execute(text("DELETE FROM risk_config WHERE risk_config_id LIKE 'test_%'"))
        session.execute(
            text("DELETE FROM circuit_breaker_event_log WHERE breaker_name = 'test_breaker'")
        )
        session.commit()
        session.close()


def make_config(**overrides) -> RiskConfig:
    defaults = dict(
        risk_config_id="test_default",
        daily_loss_limit_pct=0.03,
        weekly_loss_limit_pct=0.08,
        drawdown_tier_1_pct=0.10,
        drawdown_tier_1_factor=0.5,
        drawdown_tier_2_pct=0.15,
        drawdown_tier_3_pct=0.25,
        max_gross_exposure_pct=1.0,
        max_net_exposure_pct=0.5,
        max_concurrent_positions=5,
        max_same_symbol_directional_exposure_pct=0.2,
        circuit_breaker_atr_percentile_threshold=0.95,
        circuit_breaker_confirmation_bars=3,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def make_engine(db, config=None, sizing_strategy=None) -> RiskEngine:
    config = config or make_config()
    return RiskEngine(
        config=config,
        kill_switch=KillSwitch(db, scope=SCOPE),
        circuit_breakers=[
            CircuitBreaker(
                "test_breaker",
                threshold=config.circuit_breaker_atr_percentile_threshold,
                confirmation_bars=config.circuit_breaker_confirmation_bars,
            )
        ],
        loss_limit_tracker=LossLimitTracker(
            config.daily_loss_limit_pct, config.weekly_loss_limit_pct
        ),
        drawdown_monitor=DrawdownMonitor(
            config.drawdown_tier_1_pct,
            config.drawdown_tier_1_factor,
            config.drawdown_tier_2_pct,
            config.drawdown_tier_3_pct,
        ),
        exposure_tracker=ExposureTracker(
            config.max_gross_exposure_pct,
            config.max_net_exposure_pct,
            config.max_concurrent_positions,
            config.max_same_symbol_directional_exposure_pct,
        ),
        sizing_strategy=sizing_strategy or VolatilityAdjustedSizer(risk_fraction=0.01),
        event_bus=PostgresEventBus(),
        db_session=db,
    )


def make_signal(
    entry_price=100.0, stop_loss=90.0, strategy_id="test_s1", signal_strength=0.6
) -> Signal:
    return Signal(
        direction=1,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=None,
        strategy_id=strategy_id,
        signal_strength=signal_strength,
    )


def make_context(
    equity=10_000.0,
    peak_equity=None,
    feature_values=None,
    open_positions=None,
    trade_history=None,
    data_quality_ok=True,
    data_quality_reason=None,
    as_of=None,
) -> RiskContext:
    as_of = as_of or datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    window = FeatureWindow(
        symbol="BTC/USDT",
        timeframe="1h",
        as_of=as_of,
        values=(
            feature_values
            if feature_values is not None
            else {"atr_14": 5.0, "atr_percentile_90": 0.5}
        ),
    )
    regime_state = RegimeState(
        trend=Regime.BULL_TREND, trend_confidence=0.8, vol=VolRegime.NORMAL_VOL, vol_confidence=0.5
    )
    portfolio_view = PortfolioView(
        equity=equity,
        peak_equity=peak_equity if peak_equity is not None else equity,
        open_positions=open_positions or [],
        trade_history=trade_history or [],
    )
    return RiskContext(
        equity=equity,
        feature_window=window,
        regime_state=regime_state,
        portfolio_view=portfolio_view,
        data_quality_ok=data_quality_ok,
        data_quality_reason=data_quality_reason,
        as_of=as_of,
    )


def make_trade(pnl: float, exit_time: datetime) -> Trade:
    return Trade(
        strategy_id="test_s1",
        direction=1,
        entry_time=exit_time - timedelta(hours=1),
        exit_time=exit_time,
        entry_price=100.0,
        exit_price=100.0 + pnl,
        quantity=1.0,
        fees_paid=0.0,
        pnl=pnl,
        pnl_pct=pnl / 100.0,
        r_multiple=None,
        exit_reason="manual",
        regime_at_entry="bull_trend",
    )


def make_position(direction=1, entry_price=1_000.0, quantity=1.0) -> PositionView:
    return PositionView(
        strategy_id="other",
        direction=direction,
        entry_price=entry_price,
        quantity=quantity,
        unrealized_pnl=0.0,
    )


# --- one test per RejectionReason value -------------------------------------


def test_kill_switch_active(db):
    engine = make_engine(db)
    engine.kill_switch.engage(reason="manual halt", engaged_by="tester")

    decision = engine.size(make_signal(), make_context())
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.KILL_SWITCH_ACTIVE


def test_circuit_breaker_active(db):
    engine = make_engine(db)
    context = make_context(feature_values={"atr_14": 5.0, "atr_percentile_90": 0.99})

    decision = engine.size(make_signal(), context)
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.CIRCUIT_BREAKER_ACTIVE


def test_max_daily_loss_reached(db):
    engine = make_engine(db)
    as_of = datetime(2024, 6, 5, 12, 0, tzinfo=UTC)
    trades = [make_trade(-400.0, as_of - timedelta(hours=1))]  # -4%, breaches 3% daily
    context = make_context(equity=9_600.0, trade_history=trades, as_of=as_of)

    decision = engine.size(make_signal(), context)
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.MAX_DAILY_LOSS_REACHED


def test_max_weekly_loss_reached(db):
    engine = make_engine(db)
    monday = datetime(2024, 6, 3, 10, 0, tzinfo=UTC)
    tuesday = datetime(2024, 6, 4, 10, 0, tzinfo=UTC)
    wednesday_asof = datetime(2024, 6, 5, 10, 0, tzinfo=UTC)
    trades = [
        make_trade(-300.0, monday),
        make_trade(-300.0, tuesday),
        # today alone: -200 / (10,000-800+200) starting ~9,400 = -2.1%, under the 3% daily limit
        make_trade(-200.0, wednesday_asof - timedelta(hours=1)),
    ]
    # week total: -800 / 10,000 starting = -8%, exactly at (and breaching) the 8% weekly limit
    context = make_context(equity=10_000.0 - 800.0, trade_history=trades, as_of=wednesday_asof)

    decision = engine.size(make_signal(), context)
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.MAX_WEEKLY_LOSS_REACHED


def test_max_drawdown_reached(db):
    engine = make_engine(db)
    context = make_context(equity=8_000.0, peak_equity=10_000.0)  # -20%, tier 2

    decision = engine.size(make_signal(), context)
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.MAX_DRAWDOWN_REACHED


def test_max_drawdown_reached_tier_3_engages_kill_switch(db):
    engine = make_engine(db)
    context = make_context(equity=7_000.0, peak_equity=10_000.0)  # -30%, tier 3

    decision = engine.size(make_signal(), context)
    assert decision.rejection_reason == RejectionReason.MAX_DRAWDOWN_REACHED
    assert engine.kill_switch.is_engaged() is True


def test_data_quality_failed(db):
    engine = make_engine(db)
    context = make_context(data_quality_ok=False, data_quality_reason="stale candle")

    decision = engine.size(make_signal(), context)
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.DATA_QUALITY_FAILED


def test_exposure_limit_exceeded(db):
    engine = make_engine(db)
    # A single unhedged 0.6x-equity long breaches the 0.5x net cap.
    positions = [make_position(direction=1, entry_price=6_000.0, quantity=1.0)]
    context = make_context(equity=10_000.0, open_positions=positions)

    decision = engine.size(make_signal(), context)
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.EXPOSURE_LIMIT_EXCEEDED


def test_max_open_positions(db):
    engine = make_engine(db)
    positions = [make_position(entry_price=10.0, quantity=1.0) for _ in range(5)]
    context = make_context(equity=10_000.0, open_positions=positions)

    decision = engine.size(make_signal(), context)
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.MAX_OPEN_POSITIONS


def test_correlation_limit(db):
    engine = make_engine(db)
    # Hedged (net ~0, gross under cap) but same-direction concentration breaches 0.2x.
    positions = [
        make_position(direction=1, entry_price=2_500.0, quantity=1.0),
        make_position(direction=-1, entry_price=2_500.0, quantity=1.0),
    ]
    context = make_context(equity=10_000.0, open_positions=positions)

    decision = engine.size(make_signal(), context)  # proposed direction = 1 (long)
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.CORRELATION_LIMIT


def test_position_size_too_small(db):
    engine = make_engine(db)
    signal = make_signal(stop_loss=None)  # no stop
    context = make_context(feature_values={})  # and no atr_14 feature -> can't size

    decision = engine.size(signal, context)
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.POSITION_SIZE_TOO_SMALL


class _FakePerformanceStore:
    def __init__(self, history: PerformanceHistory):
        self._history = history

    def query(self, **kwargs) -> PerformanceHistory:
        return self._history


def test_insufficient_sample_for_kelly(db):
    store = _FakePerformanceStore(
        PerformanceHistory(sample_size=5, win_rate=0.7, avg_win_loss_ratio=2.0)
    )
    sizer = FractionalKellySizer(store, kelly_fraction_multiplier=1.0, kelly_min_sample_size=30)
    engine = make_engine(db, sizing_strategy=sizer)

    decision = engine.size(make_signal(), make_context())
    assert decision.approved_quantity == 0.0
    assert decision.rejection_reason == RejectionReason.INSUFFICIENT_SAMPLE_FOR_KELLY


# --- approvals actually persist and reflect the correct config -------------


def test_approved_decision_is_logged_to_risk_decision_log(db):
    engine = make_engine(db)
    decision = engine.size(make_signal(), make_context())
    assert decision.rejection_reason is None
    assert decision.approved_quantity > 0

    row = (
        db.execute(
            text(
                "SELECT approved_quantity, rejection_reason, risk_config_id "
                "FROM risk_decision_log WHERE strategy_id = 'test_s1' ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert float(row["approved_quantity"]) == pytest.approx(decision.approved_quantity)
    assert row["rejection_reason"] is None
    assert row["risk_config_id"] == "test_default"


def test_rejected_decision_is_also_logged(db):
    engine = make_engine(db)
    engine.kill_switch.engage(reason="halt", engaged_by="tester")
    engine.size(make_signal(), make_context())

    row = (
        db.execute(
            text(
                "SELECT rejection_reason FROM risk_decision_log "
                "WHERE strategy_id = 'test_s1' ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    assert row["rejection_reason"] == "kill_switch_active"


def test_drawdown_tier_1_throttles_rather_than_rejects(db):
    engine = make_engine(db)
    context = make_context(equity=9_000.0, peak_equity=10_000.0)  # exactly -10%, tier 1

    decision = engine.size(make_signal(), context)
    assert decision.rejection_reason is None
    assert decision.approved_quantity == pytest.approx(decision.proposed_quantity * 0.5)


# --- property-style invariants ----------------------------------------------


def test_approved_quantity_never_exceeds_the_configured_hard_cap(db):
    config = make_config(risk_config_id="test_property_cap")
    engine = make_engine(
        db, config=config, sizing_strategy=VolatilityAdjustedSizer(risk_fraction=0.5)
    )
    rng = random.Random(42)

    for _ in range(50):
        equity = rng.uniform(1_000.0, 50_000.0)
        entry_price = rng.uniform(1.0, 1_000.0)
        stop_loss = entry_price * rng.uniform(0.5, 0.99)
        signal = make_signal(entry_price=entry_price, stop_loss=stop_loss)
        context = make_context(equity=equity)

        decision = engine.size(signal, context)
        hard_cap_quantity = (equity * config.max_same_symbol_directional_exposure_pct) / entry_price
        assert decision.approved_quantity <= hard_cap_quantity + 1e-9


def test_nothing_is_ever_approved_while_kill_switch_is_engaged(db):
    engine = make_engine(db)
    engine.kill_switch.engage(reason="halt", engaged_by="tester")
    rng = random.Random(7)

    for _ in range(50):
        equity = rng.uniform(1_000.0, 50_000.0)
        entry_price = rng.uniform(1.0, 1_000.0)
        stop_loss = entry_price * rng.uniform(0.5, 0.99)
        signal = make_signal(entry_price=entry_price, stop_loss=stop_loss)
        context = make_context(equity=equity)

        decision = engine.size(signal, context)
        assert decision.approved_quantity == 0.0
        assert decision.rejection_reason == RejectionReason.KILL_SWITCH_ACTIVE
