"""
SignalPerformanceStore against real local Postgres (docker compose up -d
+ schema.sql applied), not mocks — proves the SQL bucketing/filtering
logic actually matches ConfidenceEngine._bucket()'s Python thresholds,
which a mock could never catch a drift in.

Every test cleans up the signal_log rows it inserts so the suite can be
re-run against the same database without accumulating junk.
"""

import json

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.signal_performance_store import SignalPerformanceStore
from core.strategy_base import Regime, VolRegime

_MARKER = "test_signal_performance_store_marker"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM signal_log WHERE strategy_id = :s"), {"s": _MARKER})
        session.commit()
        session.close()


def _insert_signal(db, regime, vol_regime, signal_strength, pnl=None):
    outcome = json.dumps({"pnl": pnl, "exit_reason": "take_profit"}) if pnl is not None else None
    db.execute(
        text("""
            INSERT INTO signal_log
                (symbol, bar_time, strategy_id, regime, vol_regime, direction,
                 signal_strength, outcome)
            VALUES
                ('BTC/USDT', now(), :strategy_id, :regime, :vol_regime, 1,
                 :signal_strength, CAST(:outcome AS JSONB))
            """),
        {
            "strategy_id": _MARKER,
            "regime": regime,
            "vol_regime": vol_regime,
            "signal_strength": signal_strength,
            "outcome": outcome,
        },
    )
    db.commit()


def test_no_matching_rows_returns_zero_sample_and_zero_win_rate(db):
    store = SignalPerformanceStore(db)

    history = store.query(_MARKER, Regime.BULL_TREND, VolRegime.NORMAL_VOL, "high")

    assert history.sample_size == 0
    assert history.win_rate == 0.0


def test_counts_only_rows_with_a_resolved_outcome(db):
    _insert_signal(db, "bull_trend", "normal_vol", 0.9, pnl=10.0)
    _insert_signal(db, "bull_trend", "normal_vol", 0.9, pnl=None)  # not yet resolved

    store = SignalPerformanceStore(db)
    history = store.query(_MARKER, Regime.BULL_TREND, VolRegime.NORMAL_VOL, "high")

    assert history.sample_size == 1


def test_win_rate_reflects_positive_pnl_fraction(db):
    _insert_signal(db, "bull_trend", "normal_vol", 0.9, pnl=10.0)
    _insert_signal(db, "bull_trend", "normal_vol", 0.9, pnl=-5.0)
    _insert_signal(db, "bull_trend", "normal_vol", 0.9, pnl=1.0)

    store = SignalPerformanceStore(db)
    history = store.query(_MARKER, Regime.BULL_TREND, VolRegime.NORMAL_VOL, "high")

    assert history.sample_size == 3
    assert history.win_rate == pytest.approx(2 / 3)


def test_filters_by_regime(db):
    _insert_signal(db, "bull_trend", "normal_vol", 0.9, pnl=10.0)
    _insert_signal(db, "bear_trend", "normal_vol", 0.9, pnl=10.0)

    store = SignalPerformanceStore(db)
    history = store.query(_MARKER, Regime.BULL_TREND, VolRegime.NORMAL_VOL, "high")

    assert history.sample_size == 1


def test_filters_by_vol_regime(db):
    """Both regime axes must be filtered independently — high-vol and
    low-vol outcomes must never be blended into one win rate."""
    _insert_signal(db, "bull_trend", "high_vol", 0.9, pnl=10.0)
    _insert_signal(db, "bull_trend", "low_vol", 0.9, pnl=-10.0)

    store = SignalPerformanceStore(db)
    history = store.query(_MARKER, Regime.BULL_TREND, VolRegime.HIGH_VOL, "high")

    assert history.sample_size == 1
    assert history.win_rate == 1.0


@pytest.mark.parametrize(
    "signal_strength,expected_bucket",
    [(0.9, "high"), (0.67, "high"), (0.66, "medium"), (0.5, "medium"), (0.33, "low"), (0.1, "low")],
)
def test_bucket_boundaries_match_confidence_engine_exactly(db, signal_strength, expected_bucket):
    """Reproduces ConfidenceEngine._bucket()'s exact thresholds against
    a real inserted row — proves the SQL CASE expression hasn't drifted
    from the Python implementation it's a copy of."""
    _insert_signal(db, "bull_trend", "normal_vol", signal_strength, pnl=10.0)

    store = SignalPerformanceStore(db)
    history = store.query(_MARKER, Regime.BULL_TREND, VolRegime.NORMAL_VOL, expected_bucket)

    assert history.sample_size == 1

    other_bucket = "low" if expected_bucket != "low" else "high"
    history_wrong_bucket = store.query(
        _MARKER, Regime.BULL_TREND, VolRegime.NORMAL_VOL, other_bucket
    )
    assert history_wrong_bucket.sample_size == 0
