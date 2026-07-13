from datetime import UTC, datetime, timedelta

import pytest

from core.ingestion.timeframe import is_aligned, timeframe_to_timedelta


def test_timeframe_to_timedelta():
    assert timeframe_to_timedelta("1h") == timedelta(hours=1)
    assert timeframe_to_timedelta("1m") == timedelta(minutes=1)
    assert timeframe_to_timedelta("1d") == timedelta(days=1)


def test_unknown_timeframe_raises():
    with pytest.raises(ValueError):
        timeframe_to_timedelta("3h")


def test_is_aligned_true_on_boundary():
    assert is_aligned(datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC), "1h") is True


def test_is_aligned_false_off_boundary():
    assert is_aligned(datetime(2024, 1, 1, 10, 15, 0, tzinfo=UTC), "1h") is False


def test_daily_alignment_requires_midnight():
    assert is_aligned(datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC), "1d") is True
    assert is_aligned(datetime(2024, 1, 1, 1, 0, 0, tzinfo=UTC), "1d") is False
