"""
ExchangeAdapter is the seam between "the ingestion pipeline" and "a
specific exchange's REST API". BinanceAdapter is the first
implementation; BybitAdapter/MEXCAdapter are later files implementing
this same interface — no other component (BackfillService,
GapRepairService, DataQualityService, ...) should need to change when
a new exchange is added.

Adapters are responsible for translating exchange-specific errors
(HTTP status codes, response shapes) into the RetryableIngestionError /
FatalIngestionError taxonomy — everything downstream of an adapter call
treats that taxonomy as the only thing it needs to understand.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from core.ingestion.types import RateLimitConfig, RawCandle


class ExchangeAdapter(ABC):
    exchange_name: str
    rate_limit_config: RateLimitConfig

    @abstractmethod
    def fetch_klines(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        limit: int,
    ) -> list[RawCandle]:
        """Fetch candles in [start_time, end_time], oldest first, up to
        `limit` candles. Raises RetryableIngestionError or
        FatalIngestionError on failure — never returns a partial batch
        silently."""

    @abstractmethod
    def earliest_available(self, symbol: str, timeframe: str) -> datetime | None:
        """Discover the earliest candle the exchange can return.
        Returns None if undiscoverable — the caller falls back to the
        configured default backfill window."""

    @abstractmethod
    def normalize_symbol(self, symbol: str) -> str:
        """Convert a canonical symbol (e.g. "BTC/USDT") into this
        exchange's wire format (e.g. "BTCUSDT")."""
