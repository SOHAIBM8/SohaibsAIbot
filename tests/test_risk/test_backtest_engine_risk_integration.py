"""
Wires RiskEngine in as BacktestEngine's real position_sizer and runs an
actual backtest, confirming trades get sized/rejected as expected
end-to-end (spec section 6) — against real Postgres, not mocks.
"""

import pandas as pd
import pytest
from sqlalchemy import text

from core.backtest_engine import BacktestEngine
from core.db import SessionLocal
from core.execution_model import ExecutionModel
from core.ingestion.event_bus import PostgresEventBus
from core.regime_config import RegimeDetectorConfig
from core.regime_detector import RegimeDetector
from core.risk.circuit_breaker import CircuitBreaker
from core.risk.drawdown_monitor import DrawdownMonitor
from core.risk.exposure_tracker import ExposureTracker
from core.risk.kill_switch import KillSwitch
from core.risk.loss_limit_tracker import LossLimitTracker
from core.risk.position_sizing_strategies import VolatilityAdjustedSizer
from core.risk.risk_config import RiskConfig
from core.risk.risk_engine import RiskEngine
from core.strategy_base import Regime, Signal, StrategyBase, StrategyMeta

SCOPE = "test_backtest_risk"


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


def make_strategy(name, trigger_column, stop_loss=95.0):
    class _TestStrategy(StrategyBase):
        meta = StrategyMeta(
            name=name,
            version="1.0.0",
            author="test",
            created_at=None,
            description="test fixture",
            parameters={},
            compatible_pipeline_versions=["features_v1"],
            works_best_in=[Regime.SIDEWAYS],
        )
        required_features = [trigger_column]
        min_lookback = 0

        def generate_signal(self, feature_window) -> Signal:
            if feature_window.get(trigger_column) == 1:
                return Signal(
                    direction=1,
                    entry_price=feature_window.get("close"),
                    stop_loss=stop_loss,
                    take_profit=None,
                    strategy_id=self.meta.strategy_id,
                    signal_strength=0.8,
                    reasons=["test trigger fired"],
                )
            return Signal(
                direction=0,
                entry_price=feature_window.get("close"),
                stop_loss=None,
                take_profit=None,
                strategy_id=self.meta.strategy_id,
                signal_strength=0.0,
            )

        def validate(self, feature_registry):
            return []

    return _TestStrategy()


def base_columns(n, trigger_bar=1):
    open_ = [100.0 + 3 * i for i in range(n)]
    # risk_decision_log.bar_time is TIMESTAMPTZ — a real datetime index
    # is required once a DB-writing sizer (RiskEngine) is in the loop,
    # unlike the plain integer index the DB-free BacktestEngine tests
    # get away with.
    index = pd.date_range("2024-06-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": open_,
            "high": [o + 2 for o in open_],
            "low": [o - 1 for o in open_],
            "close": [o + 1 for o in open_],
            "ema_20": [100.0] * n,
            "ema_50": [100.0] * n,
            "adx_14": [10.0] * n,
            "atr_14": [2.0] * n,
            "atr_percentile_90": [0.5] * n,
            "buy_trigger": [0] * n,
        },
        index=index,
    )
    df.loc[df.index[trigger_bar], "buy_trigger"] = 1
    return df


def make_risk_engine(db, config=None, kill_switch=None):
    config = config or RiskConfig(risk_config_id="test_backtest_default")
    return RiskEngine(
        config=config,
        kill_switch=kill_switch or KillSwitch(db, scope=SCOPE),
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
        sizing_strategy=VolatilityAdjustedSizer(risk_fraction=0.01),
        event_bus=PostgresEventBus(),
        db_session=db,
    )


def test_backtest_with_risk_engine_produces_approved_and_logged_trades(db):
    risk_engine = make_risk_engine(db)
    engine = BacktestEngine(
        strategies=[make_strategy("test_risk_bt", "buy_trigger")],
        regime_detector=RegimeDetector(RegimeDetectorConfig(min_confirmation_bars=1)),
        position_sizer=risk_engine,
        execution_model=ExecutionModel(fee_bps=0, slippage_bps=0),
        initial_capital=10_000.0,
    )

    df = base_columns(n=8, trigger_bar=1)
    result = engine.run(df)

    assert len(result.trades) == 1
    assert result.trades[0].quantity > 0

    rows = (
        db.execute(
            text(
                "SELECT approved_quantity, rejection_reason FROM risk_decision_log "
                "WHERE strategy_id = 'test_risk_bt@1.0.0'"
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1
    assert rows[0]["rejection_reason"] is None
    assert float(rows[0]["approved_quantity"]) == pytest.approx(result.trades[0].quantity)


def test_backtest_with_kill_switch_engaged_produces_no_trades(db):
    kill_switch = KillSwitch(db, scope=SCOPE)
    kill_switch.engage(reason="pre-engaged for test", engaged_by="tester")
    risk_engine = make_risk_engine(db, kill_switch=kill_switch)

    engine = BacktestEngine(
        strategies=[make_strategy("test_risk_bt_halted", "buy_trigger")],
        regime_detector=RegimeDetector(RegimeDetectorConfig(min_confirmation_bars=1)),
        position_sizer=risk_engine,
        execution_model=ExecutionModel(fee_bps=0, slippage_bps=0),
        initial_capital=10_000.0,
    )

    df = base_columns(n=8, trigger_bar=1)
    result = engine.run(df)

    assert len(result.trades) == 0

    rows = (
        db.execute(
            text(
                "SELECT rejection_reason FROM risk_decision_log "
                "WHERE strategy_id = 'test_risk_bt_halted@1.0.0'"
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1
    assert rows[0]["rejection_reason"] == "kill_switch_active"


def test_backtest_with_data_quality_ok_false_produces_no_trades(db):
    """docs/gap_audit_report.md P0 #2: RiskContext.data_quality_ok was
    hardcoded True in every RiskContext BacktestEngine ever built —
    RejectionReason.DATA_QUALITY_FAILED was reachable in RiskEngine but
    never actually reachable end-to-end from a real backtest run. Same
    shape as the kill-switch test above, proving the wiring now works."""
    risk_engine = make_risk_engine(db)
    engine = BacktestEngine(
        strategies=[make_strategy("test_risk_bt_dq", "buy_trigger")],
        regime_detector=RegimeDetector(RegimeDetectorConfig(min_confirmation_bars=1)),
        position_sizer=risk_engine,
        execution_model=ExecutionModel(fee_bps=0, slippage_bps=0),
        initial_capital=10_000.0,
        data_quality_ok=False,
        data_quality_reason="test: simulated bad ingested data",
    )

    df = base_columns(n=8, trigger_bar=1)
    result = engine.run(df)

    assert len(result.trades) == 0

    rows = (
        db.execute(
            text(
                "SELECT rejection_reason FROM risk_decision_log "
                "WHERE strategy_id = 'test_risk_bt_dq@1.0.0'"
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1
    assert rows[0]["rejection_reason"] == "data_quality_failed"


def test_backtest_data_quality_ok_true_by_default_preserves_old_behavior(db):
    """No caller passes data_quality_ok — confirms the default keeps
    existing callers/tests working exactly as before this change."""
    risk_engine = make_risk_engine(db)
    engine = BacktestEngine(
        strategies=[make_strategy("test_risk_bt_dq_default", "buy_trigger")],
        regime_detector=RegimeDetector(RegimeDetectorConfig(min_confirmation_bars=1)),
        position_sizer=risk_engine,
        execution_model=ExecutionModel(fee_bps=0, slippage_bps=0),
        initial_capital=10_000.0,
    )

    df = base_columns(n=8, trigger_bar=1)
    result = engine.run(df)

    assert len(result.trades) == 1
