"""
Tests run against real local Postgres. Candles are seeded via the real
upsert_candles() write path (core/ingestion/raw_ohlcv_store.py), not
raw SQL, so these tests exercise the exact same table shape production
ingestion produces.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core.db import SessionLocal
from core.ingestion.ohlcv_reader import OHLCVReader
from core.ingestion.raw_ohlcv_store import upsert_candles
from core.ingestion.types import RawCandle

EXCHANGE = "test_exchange"
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM raw_ohlcv WHERE exchange = :e"), {"e": EXCHANGE})
        session.commit()
        session.close()


def _candle(open_time, close=100.0):
    return RawCandle(
        open_time=open_time,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=10.0,
        close_time=open_time + timedelta(hours=1),
    )


def test_list_candles_returns_chronological_order(db):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    candles = [_candle(base + timedelta(hours=i), close=100.0 + i) for i in range(3)]
    upsert_candles(db, EXCHANGE, SYMBOL, TIMEFRAME, candles, source_run_id=None)

    result = OHLCVReader(db).list_candles(EXCHANGE, SYMBOL, TIMEFRAME, limit=10)

    assert [c.close for c in result] == [100.0, 101.0, 102.0]
    assert result[0].open_time < result[-1].open_time


def test_list_candles_respects_limit_keeping_the_most_recent(db):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    candles = [_candle(base + timedelta(hours=i), close=100.0 + i) for i in range(5)]
    upsert_candles(db, EXCHANGE, SYMBOL, TIMEFRAME, candles, source_run_id=None)

    result = OHLCVReader(db).list_candles(EXCHANGE, SYMBOL, TIMEFRAME, limit=2)

    assert [c.close for c in result] == [103.0, 104.0]


def test_list_candles_filters_by_start_and_end(db):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    candles = [_candle(base + timedelta(hours=i), close=100.0 + i) for i in range(5)]
    upsert_candles(db, EXCHANGE, SYMBOL, TIMEFRAME, candles, source_run_id=None)

    result = OHLCVReader(db).list_candles(
        EXCHANGE,
        SYMBOL,
        TIMEFRAME,
        start=base + timedelta(hours=1),
        end=base + timedelta(hours=3),
        limit=10,
    )

    assert [c.close for c in result] == [101.0, 102.0, 103.0]


def test_list_candles_scopes_by_exchange_symbol_timeframe(db):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    upsert_candles(db, EXCHANGE, SYMBOL, TIMEFRAME, [_candle(base)], source_run_id=None)
    upsert_candles(db, EXCHANGE, "ETH/USDT", TIMEFRAME, [_candle(base)], source_run_id=None)

    result = OHLCVReader(db).list_candles(EXCHANGE, SYMBOL, TIMEFRAME, limit=10)

    assert all(c.symbol == SYMBOL for c in result)


def test_list_candles_returns_empty_for_unknown_symbol(db):
    result = OHLCVReader(db).list_candles(EXCHANGE, "NO/SUCH", TIMEFRAME, limit=10)
    assert result == []
