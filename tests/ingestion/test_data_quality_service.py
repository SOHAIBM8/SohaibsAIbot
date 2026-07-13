from datetime import timedelta

from sqlalchemy import text

from core.ingestion.config import IngestionConfig
from core.ingestion.data_quality_service import DataQualityService
from core.ingestion.testing import FakeExchangeAdapter
from tests.ingestion.conftest import hourly_candles, make_candle


def _insert_raw(db, exchange, symbol, timeframe, candle):
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES (:exchange, :symbol, :timeframe, :open_time, :open, :high, :low, :close, :volume)
            ON CONFLICT DO NOTHING
            """),
        {
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "open_time": candle.open_time,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        },
    )
    db.commit()


def test_clean_data_produces_no_issues(db, now):
    start = now - timedelta(hours=20)
    for candle in hourly_candles(start, 15):
        _insert_raw(db, "fake", "BTC/USDT", "1h", candle)

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert report.candles_checked == 15
    assert report.issues == []


def test_duplicate_candle_check_runs_and_finds_none(db, now):
    # Duplicates are structurally impossible given the PK on
    # (exchange, symbol, timeframe, open_time) — this just confirms the
    # check actually runs and correctly reports zero for clean data.
    start = now - timedelta(hours=5)
    for candle in hourly_candles(start, 3):
        _insert_raw(db, "fake", "BTC/USDT", "1h", candle)

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert "duplicate_candles" in report.checks_run
    assert not any(i.check == "duplicate_candles" for i in report.issues)


def test_invalid_ohlc_detected(db, now):
    start = now - timedelta(hours=5)
    bad = make_candle(start)
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES ('fake', 'BTC/USDT', '1h', :open_time, 100, 90, 95, 98, 10)
            """),
        {"open_time": bad.open_time},
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert any(i.check == "invalid_ohlc" for i in report.issues)


def test_timestamp_misalignment_detected(db, now):
    misaligned_time = (now - timedelta(hours=5)).replace(minute=17)
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES ('fake', 'BTC/USDT', '1h', :open_time, 100, 101, 99, 100, 10)
            """),
        {"open_time": misaligned_time},
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert any(i.check == "timestamp_alignment" for i in report.issues)


def test_volume_anomaly_detected(db, now):
    start = now - timedelta(hours=20)
    normal = hourly_candles(start, 19, price=100)
    for c in normal:
        _insert_raw(db, "fake", "BTC/USDT", "1h", c)
    # one wildly anomalous volume candle
    spike = make_candle(start + timedelta(hours=19), price=100, volume=100_000)
    _insert_raw(db, "fake", "BTC/USDT", "1h", spike)

    report = DataQualityService(db, IngestionConfig(volume_anomaly_zscore_threshold=3.0)).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    assert any(i.check == "volume_anomalies" for i in report.issues)


def test_cross_check_against_exchange_detects_drift(db, now):
    start = now - timedelta(hours=3)
    candle = make_candle(start, price=100)
    _insert_raw(db, "fake", "BTC/USDT", "1h", candle)

    # exchange "actually" has a different close than what's stored
    drifted = make_candle(start, price=100)
    from dataclasses import replace

    drifted = replace(drifted, close=candle.close + 50)
    adapter = FakeExchangeAdapter(candles=[drifted])

    report = DataQualityService(db, IngestionConfig(), adapter=adapter).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    assert report.cross_check_diffs >= 1
    assert any(i.check == "cross_check_exchange" for i in report.issues)


def test_report_is_persisted(db, now):
    start = now - timedelta(hours=2)
    for c in hourly_candles(start, 2):
        _insert_raw(db, "fake", "BTC/USDT", "1h", c)

    DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    row = db.execute(text("SELECT * FROM data_quality_report")).mappings().first()
    assert row is not None
    assert row["candles_checked"] == 2
