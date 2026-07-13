from datetime import UTC, datetime

from core.ingestion.candle_validator import validate_candles
from core.ingestion.types import RawCandle
from tests.ingestion.conftest import make_candle


def test_valid_candle_passes():
    now = datetime(2024, 1, 1, 12, tzinfo=UTC)
    candle = make_candle(datetime(2024, 1, 1, 10, tzinfo=UTC))
    result = validate_candles([candle], "1h", now)
    assert result.valid == [candle]
    assert result.failures == []


def test_bad_high_ordering_rejected():
    now = datetime(2024, 1, 1, 12, tzinfo=UTC)
    candle = RawCandle(
        open_time=datetime(2024, 1, 1, 10, tzinfo=UTC),
        open=100,
        high=99,
        low=95,
        close=98,  # high below open
        volume=10,
        close_time=datetime(2024, 1, 1, 10, 59, tzinfo=UTC),
    )
    result = validate_candles([candle], "1h", now)
    assert result.valid == []
    assert "high" in result.failures[0].reason


def test_bad_low_ordering_rejected():
    now = datetime(2024, 1, 1, 12, tzinfo=UTC)
    candle = RawCandle(
        open_time=datetime(2024, 1, 1, 10, tzinfo=UTC),
        open=100,
        high=105,
        low=101,
        close=98,  # low above close
        volume=10,
        close_time=datetime(2024, 1, 1, 10, 59, tzinfo=UTC),
    )
    result = validate_candles([candle], "1h", now)
    assert result.valid == []
    assert "low" in result.failures[0].reason


def test_misaligned_timestamp_rejected():
    now = datetime(2024, 1, 1, 12, tzinfo=UTC)
    candle = make_candle(datetime(2024, 1, 1, 10, 15, tzinfo=UTC))  # not on the hour
    result = validate_candles([candle], "1h", now)
    assert result.valid == []
    assert "aligned" in result.failures[0].reason


def test_forming_candle_rejected():
    now = datetime(2024, 1, 1, 10, 30, tzinfo=UTC)
    candle = RawCandle(
        open_time=datetime(2024, 1, 1, 10, tzinfo=UTC),
        open=100,
        high=101,
        low=99,
        close=100.5,
        volume=10,
        close_time=datetime(2024, 1, 1, 11, tzinfo=UTC),  # closes in the future
    )
    result = validate_candles([candle], "1h", now)
    assert result.valid == []
    assert "not yet closed" in result.failures[0].reason


def test_negative_volume_rejected():
    now = datetime(2024, 1, 1, 12, tzinfo=UTC)
    candle = RawCandle(
        open_time=datetime(2024, 1, 1, 10, tzinfo=UTC),
        open=100,
        high=101,
        low=99,
        close=100.5,
        volume=-1,
        close_time=datetime(2024, 1, 1, 10, 59, tzinfo=UTC),
    )
    result = validate_candles([candle], "1h", now)
    assert result.valid == []
    assert "volume" in result.failures[0].reason


def test_duplicate_open_time_within_batch_rejected():
    now = datetime(2024, 1, 1, 12, tzinfo=UTC)
    c1 = make_candle(datetime(2024, 1, 1, 10, tzinfo=UTC))
    c2 = make_candle(datetime(2024, 1, 1, 10, tzinfo=UTC), price=200.0)
    result = validate_candles([c1, c2], "1h", now)
    assert result.valid == [c1]
    assert "duplicate" in result.failures[0].reason
