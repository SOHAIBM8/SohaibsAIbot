"""
SignalScanner against real local Postgres (raw_ohlcv/signal_log rows
are real) with real indicator computation (core.indicators.register's
default registry, extended with one deterministic test-only "trigger"
feature) — no mocks, matching this project's established discipline.

Determinism strategy: real strategies (EMACrossStrategy/
RSIMeanReversionStrategy) fire on organic crossovers/RSI extremes that
are awkward to force deterministically from synthetic OHLCV. Instead,
a minimal fake strategy (same established pattern as
tests/test_backtest_engine.py's make_strategy) requires a "trigger"
feature registered directly on the FeatureRegistry instance under
test, set to fire exactly on the last bar — this exercises the real
SignalScanner machinery (real indicator computation for regime
classification, real eligibility check, real signal_log persistence
and idempotency, real event publishing) without depending on whether
a real strategy's internal math happens to cross on a given synthetic
series.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.indicators.register import build_default_registry
from core.ingestion.event_bus import EventBus, EventHandler
from core.regime_config import RegimeDetectorConfig
from core.regime_detector import RegimeDetector
from core.signals.events import TradeSignalGenerated
from core.signals.signal_scanner import SignalScanner
from core.strategy_base import Regime, Signal, StrategyBase, StrategyMeta

EXCHANGE = "fake"
SYMBOL = "TEST_SCANNER/USDT"
TIMEFRAME = "1h"


class _RecordingEventBus(EventBus):
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        raise NotImplementedError


def _make_trigger_strategy(name: str, direction: int = 1, regime=Regime.SIDEWAYS):
    class _TriggerStrategy(StrategyBase):
        meta = StrategyMeta(
            name=name,
            version="1.0.0",
            author="test",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            description="test fixture",
            parameters={},
            compatible_pipeline_versions=["features_v1"],
            works_best_in=[regime],
        )
        required_features = ["close", "trigger"]
        min_lookback = 0

        def generate_signal(self, feature_window) -> Signal:
            if feature_window.get("trigger") == 1:
                return Signal(
                    direction=direction,
                    entry_price=feature_window.get("close"),
                    stop_loss=None,
                    take_profit=None,
                    strategy_id=self.meta.strategy_id,
                    signal_strength=0.8,
                    reasons=["trigger fired"],
                )
            return Signal(
                direction=0,
                entry_price=feature_window.get("close"),
                stop_loss=None,
                take_profit=None,
                strategy_id=self.meta.strategy_id,
                signal_strength=0.0,
                rejected_reasons=["trigger not set"],
            )

        def validate(self, feature_registry):
            return []

    return _TriggerStrategy()


def _registry_with_trigger(trigger_on_last_bar: bool = True):
    from core.feature_store import FeatureDefinition

    registry = build_default_registry()

    def _trigger_formula(df: pd.DataFrame) -> pd.Series:
        values = [0] * len(df)
        if trigger_on_last_bar and len(df) > 0:
            values[-1] = 1
        return pd.Series(values, index=df.index)

    registry.register(
        FeatureDefinition(
            name="trigger",
            version="v1",
            formula=_trigger_formula,
            parameters={},
            dependencies=[],
            last_updated=datetime.now(UTC).isoformat(),
        )
    )
    return registry


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text("DELETE FROM raw_ohlcv WHERE exchange = :e AND symbol = :s AND timeframe = :t"),
            {"e": EXCHANGE, "s": SYMBOL, "t": TIMEFRAME},
        )
        session.execute(text("DELETE FROM signal_log WHERE symbol = :s"), {"s": SYMBOL})
        session.commit()
        session.close()


def _seed_flat_candles(db, n: int = 150) -> None:
    """Low-noise, non-trending series — real ADX stays comfortably
    below RegimeDetectorConfig's default trend threshold (20.0), so
    RegimeDetector reliably reports SIDEWAYS, matching the fake
    strategy's works_best_in above."""
    rng = np.random.default_rng(seed=7)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    close = 100 + rng.normal(0, 0.05, n)  # flat: no cumulative drift
    for i in range(n):
        open_time = start + timedelta(hours=i)
        price = float(close[i])
        db.execute(
            text("""
                INSERT INTO raw_ohlcv
                    (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
                VALUES (:exchange, :symbol, :timeframe, :open_time, :open, :high, :low,
                        :close, :volume)
                ON CONFLICT DO NOTHING
                """),
            {
                "exchange": EXCHANGE,
                "symbol": SYMBOL,
                "timeframe": TIMEFRAME,
                "open_time": open_time,
                "open": price,
                "high": price + 0.1,
                "low": price - 0.1,
                "close": price,
                "volume": 10.0,
            },
        )
    db.commit()


def test_insufficient_history_returns_empty_and_does_not_raise(db):
    _seed_flat_candles(db, n=10)
    strategy = _make_trigger_strategy("insufficient_history_probe")
    scanner = SignalScanner(
        db=db,
        feature_registry=_registry_with_trigger(),
        strategies=[strategy],
        regime_detector=RegimeDetector(RegimeDetectorConfig()),
        exchange=EXCHANGE,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        lookback_bars=250,
    )

    results = scanner.run_once()

    assert results == []


def test_eligible_strategy_with_trigger_produces_a_signal_and_publishes_event(db):
    _seed_flat_candles(db, n=150)
    strategy = _make_trigger_strategy("eligible_trigger_probe", direction=1)
    bus = _RecordingEventBus()
    scanner = SignalScanner(
        db=db,
        feature_registry=_registry_with_trigger(trigger_on_last_bar=True),
        strategies=[strategy],
        regime_detector=RegimeDetector(RegimeDetectorConfig()),
        exchange=EXCHANGE,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        event_bus=bus,
        lookback_bars=150,
    )

    results = scanner.run_once()

    assert len(results) == 1
    assert results[0].strategy_id == strategy.meta.strategy_id
    assert results[0].direction == 1
    assert results[0].newly_recorded is True

    assert len(bus.published) == 1
    event = bus.published[0]
    assert isinstance(event, TradeSignalGenerated)
    assert event.strategy_id == strategy.meta.strategy_id
    assert event.symbol == SYMBOL
    assert event.direction == 1
    assert event.regime_trend == "sideways"

    row = (
        db.execute(
            text("SELECT * FROM signal_log WHERE strategy_id = :s"),
            {"s": strategy.meta.strategy_id},
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert row["direction"] == 1
    assert row["regime"] == "sideways"


def test_no_trigger_produces_no_directional_signal_but_still_logs(db):
    _seed_flat_candles(db, n=150)
    strategy = _make_trigger_strategy("no_trigger_probe")
    bus = _RecordingEventBus()
    scanner = SignalScanner(
        db=db,
        feature_registry=_registry_with_trigger(trigger_on_last_bar=False),
        strategies=[strategy],
        regime_detector=RegimeDetector(RegimeDetectorConfig()),
        exchange=EXCHANGE,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        event_bus=bus,
        lookback_bars=150,
    )

    results = scanner.run_once()

    assert results == []
    assert bus.published == []

    row = (
        db.execute(
            text("SELECT direction FROM signal_log WHERE strategy_id = :s"),
            {"s": strategy.meta.strategy_id},
        )
        .mappings()
        .first()
    )
    assert row is not None  # still logged, per "log everything" discipline
    assert row["direction"] == 0


def test_ineligible_regime_never_calls_generate_signal_but_still_logs(db):
    _seed_flat_candles(db, n=150)
    # Eligible only in a trend regime — the flat fixture data classifies
    # SIDEWAYS, so this strategy is never eligible.
    strategy = _make_trigger_strategy("ineligible_probe", regime=Regime.BULL_TREND)
    scanner = SignalScanner(
        db=db,
        feature_registry=_registry_with_trigger(trigger_on_last_bar=True),
        strategies=[strategy],
        regime_detector=RegimeDetector(RegimeDetectorConfig()),
        exchange=EXCHANGE,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        lookback_bars=150,
    )

    results = scanner.run_once()

    assert results == []
    row = (
        db.execute(
            text("SELECT rejected_reasons FROM signal_log WHERE strategy_id = :s"),
            {"s": strategy.meta.strategy_id},
        )
        .mappings()
        .first()
    )
    assert row is not None
    assert "not eligible for current regime" in row["rejected_reasons"]


def test_running_twice_for_the_same_bar_does_not_duplicate(db):
    _seed_flat_candles(db, n=150)
    strategy = _make_trigger_strategy("idempotency_probe", direction=1)
    bus = _RecordingEventBus()
    scanner = SignalScanner(
        db=db,
        feature_registry=_registry_with_trigger(trigger_on_last_bar=True),
        strategies=[strategy],
        regime_detector=RegimeDetector(RegimeDetectorConfig()),
        exchange=EXCHANGE,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        event_bus=bus,
        lookback_bars=150,
    )

    first = scanner.run_once()
    second = scanner.run_once()

    assert len(first) == 1
    assert second == []  # same bar already processed — safe no-op
    assert len(bus.published) == 1  # never double-published

    count = db.execute(
        text("SELECT count(*) FROM signal_log WHERE strategy_id = :s"),
        {"s": strategy.meta.strategy_id},
    ).scalar_one()
    assert count == 1


def test_is_due_true_on_first_call_then_respects_interval(db):
    strategy = _make_trigger_strategy("due_probe")
    scanner = SignalScanner(
        db=db,
        feature_registry=_registry_with_trigger(),
        strategies=[strategy],
        regime_detector=RegimeDetector(RegimeDetectorConfig()),
        exchange=EXCHANGE,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        interval_seconds=3600.0,
    )
    now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

    assert scanner.is_due(now) is True
    scanner.run_once(now)
    assert scanner.is_due(now) is False
    assert scanner.is_due(now + timedelta(hours=2)) is True
