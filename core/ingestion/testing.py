"""
Test doubles for the ingestion component. Lives in core/ingestion/
(not tests/) so it's importable as a normal dependency of the test
suite without sys.path games, but it is NOT wired into any production
code path — nothing outside tests/ should import FakeExchangeAdapter.

FakeExchangeAdapter returns deterministic, scripted candle sequences
(including deliberately malformed ones) so BackfillService,
IncrementalUpdateService, GapDetectionService, GapRepairService, and
DataQualityService can all be tested without a real network call,
per spec section 7.
"""

from datetime import datetime

from core.ingestion.errors import FatalIngestionError, RetryableIngestionError
from core.ingestion.exchange_adapter import ExchangeAdapter
from core.ingestion.types import RateLimitConfig, RawCandle


class FakeExchangeAdapter(ExchangeAdapter):
    exchange_name = "fake"
    rate_limit_config = RateLimitConfig(
        requests_per_window=1000, window_seconds=1, weight_per_request=1
    )

    def __init__(
        self,
        candles: list[RawCandle] | None = None,
        earliest: datetime | None = None,
        fail_next_n_calls: int = 0,
        fail_with: type[Exception] = RetryableIngestionError,
    ):
        """`candles` is the adapter's full, ordered "exchange history" —
        fetch_klines slices out whatever falls in [start_time, end_time].
        `fail_next_n_calls` lets a test script transient failures
        (e.g. to exercise RetryPolicy) before real data comes back."""
        self._candles = sorted(candles or [], key=lambda c: c.open_time)
        self._earliest = earliest
        self._remaining_failures = fail_next_n_calls
        self._fail_with = fail_with
        self.calls: list[tuple[str, str, datetime, datetime]] = []

    def normalize_symbol(self, symbol: str) -> str:
        return symbol.replace("/", "").upper()

    def fetch_klines(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        limit: int,
    ) -> list[RawCandle]:
        self.calls.append((symbol, timeframe, start_time, end_time))
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise self._fail_with("scripted failure")

        matches = [c for c in self._candles if start_time <= c.open_time <= end_time]
        return matches[:limit]

    def earliest_available(self, symbol: str, timeframe: str) -> datetime | None:
        if self._earliest is not None:
            return self._earliest
        if self._candles:
            return self._candles[0].open_time
        return None


class AlwaysFatalAdapter(ExchangeAdapter):
    """Every call raises FatalIngestionError — used to test that fatal
    errors are never retried and propagate cleanly."""

    exchange_name = "always_fatal"
    rate_limit_config = RateLimitConfig(requests_per_window=1000, window_seconds=1)

    def normalize_symbol(self, symbol: str) -> str:
        return symbol

    def fetch_klines(
        self, symbol: str, timeframe: str, start_time: datetime, end_time: datetime, limit: int
    ) -> list[RawCandle]:
        raise FatalIngestionError("always fails")

    def earliest_available(self, symbol: str, timeframe: str) -> datetime | None:
        raise FatalIngestionError("always fails")
