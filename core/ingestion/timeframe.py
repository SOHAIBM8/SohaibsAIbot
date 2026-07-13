"""
Timeframe string <-> interval conversion, shared by the candle
validator (boundary alignment), gap detection (expected-timestamp
generation), and the services that need to advance a watermark by
"one bar". Centralized here so "1h" means exactly one thing everywhere
in the ingestion component.
"""

from datetime import datetime, timedelta

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


def timeframe_to_timedelta(timeframe: str) -> timedelta:
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unknown timeframe: {timeframe!r}")
    return timedelta(seconds=TIMEFRAME_SECONDS[timeframe])


def is_aligned(open_time: datetime, timeframe: str) -> bool:
    """True if open_time falls exactly on a timeframe boundary, e.g. a
    1h candle's open_time must land on the hour (minute=second=0)."""
    seconds = TIMEFRAME_SECONDS[timeframe]
    epoch_seconds = int(open_time.timestamp())
    return epoch_seconds % seconds == 0
