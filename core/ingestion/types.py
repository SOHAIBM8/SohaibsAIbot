"""
Core value types shared across the ingestion component. Kept separate
from any single service so BackfillService, GapRepairService, and
DataQualityService all speak the same candle/rate-limit vocabulary
instead of each defining their own.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RawCandle:
    """One OHLCV bar exactly as returned by an exchange, before
    validation. `close_time` is what CandleValidator uses to reject a
    still-forming candle — it is NOT the same as the next candle's
    open_time in every exchange's API, so it's carried explicitly
    rather than derived from timeframe + open_time."""

    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: datetime
    is_closed: bool = True


@dataclass(frozen=True)
class RateLimitConfig:
    """Per-exchange rate limit shape. Binance, Bybit, MEXC all define
    this differently (weight-per-endpoint vs. flat request count) —
    never hardcode a single global constant for it."""

    requests_per_window: int
    window_seconds: float
    weight_per_request: int = 1
