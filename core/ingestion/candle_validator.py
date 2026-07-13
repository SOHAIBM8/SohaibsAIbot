"""
Pure validation rules for candles (spec 4.4) — no I/O, so both
ingestion-time validation (BackfillService, IncrementalUpdateService,
GapRepairService) and after-the-fact validation (DataQualityService,
re-checking rows already in raw_ohlcv) can share this exact logic.
Every rejection carries a reason; nothing is ever silently dropped.
"""

from dataclasses import dataclass, field
from datetime import datetime

from core.ingestion.timeframe import is_aligned
from core.ingestion.types import RawCandle


@dataclass
class ValidationFailure:
    open_time: datetime
    reason: str


@dataclass
class ValidationResult:
    valid: list[RawCandle] = field(default_factory=list)
    failures: list[ValidationFailure] = field(default_factory=list)


def validate_candles(candles: list[RawCandle], timeframe: str, now: datetime) -> ValidationResult:
    result = ValidationResult()
    seen_open_times: set[datetime] = set()

    for candle in candles:
        reason = _validate_one(candle, timeframe, now, seen_open_times)
        if reason is None:
            result.valid.append(candle)
            seen_open_times.add(candle.open_time)
        else:
            result.failures.append(ValidationFailure(open_time=candle.open_time, reason=reason))

    return result


def _validate_one(
    candle: RawCandle, timeframe: str, now: datetime, seen_open_times: set[datetime]
) -> str | None:
    if candle.open_time in seen_open_times:
        return "duplicate open_time within batch"
    if not candle.is_closed or candle.close_time >= now:
        return "candle not yet closed"
    if candle.high < max(candle.open, candle.close):
        return "high below max(open, close)"
    if candle.low > min(candle.open, candle.close):
        return "low above min(open, close)"
    if candle.high < candle.low:
        return "high below low"
    if candle.volume < 0:
        return "negative volume"
    if not is_aligned(candle.open_time, timeframe):
        return f"open_time not aligned to {timeframe} boundary"
    return None
