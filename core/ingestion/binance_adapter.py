"""
Binance REST implementation of ExchangeAdapter. Klines endpoint docs:
https://binance-docs.github.io/apidocs/spot/en/#kline-candlestick-data

earliest_available() uses Binance's documented trick of requesting
klines starting at epoch 0 with limit=1 — the exchange clamps this to
the symbol's actual first candle rather than erroring, which is cheaper
than a binary search and doesn't require a listing-date endpoint
Binance doesn't have.
"""

from datetime import UTC, datetime

import requests
import structlog

from core.ingestion.errors import FatalIngestionError, RetryableIngestionError
from core.ingestion.exchange_adapter import ExchangeAdapter
from core.ingestion.types import RateLimitConfig, RawCandle

logger = structlog.get_logger(__name__)

BASE_URL = "https://api.binance.com"

_TIMEFRAME_TO_BINANCE_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


class BinanceAdapter(ExchangeAdapter):
    exchange_name = "binance"
    # Binance's spot klines endpoint costs weight 2, limit is 1200
    # weight/minute for the whole account — see Binance API docs.
    rate_limit_config = RateLimitConfig(
        requests_per_window=1200, window_seconds=60, weight_per_request=2
    )

    def __init__(self, session: requests.Session | None = None, timeout_seconds: float = 10.0):
        self._session = session or requests.Session()
        self._timeout = timeout_seconds

    def normalize_symbol(self, symbol: str) -> str:
        return symbol.replace("/", "").replace("-", "").upper()

    def fetch_klines(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        limit: int,
    ) -> list[RawCandle]:
        interval = self._interval(timeframe)
        params = {
            "symbol": self.normalize_symbol(symbol),
            "interval": interval,
            "startTime": int(start_time.timestamp() * 1000),
            "endTime": int(end_time.timestamp() * 1000),
            "limit": limit,
        }
        rows = self._get("/api/v3/klines", params)
        return [self._row_to_candle(row) for row in rows]

    def earliest_available(self, symbol: str, timeframe: str) -> datetime | None:
        interval = self._interval(timeframe)
        params = {
            "symbol": self.normalize_symbol(symbol),
            "interval": interval,
            "startTime": 0,
            "limit": 1,
        }
        rows = self._get("/api/v3/klines", params)
        if not rows:
            return None
        return self._row_to_candle(rows[0]).open_time

    def _interval(self, timeframe: str) -> str:
        if timeframe not in _TIMEFRAME_TO_BINANCE_INTERVAL:
            raise FatalIngestionError(f"unsupported timeframe for Binance: {timeframe!r}")
        return _TIMEFRAME_TO_BINANCE_INTERVAL[timeframe]

    def _get(self, path: str, params: dict) -> list:
        url = f"{BASE_URL}{path}"
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
        except requests.exceptions.Timeout as exc:
            raise RetryableIngestionError(f"timeout calling {path}") from exc
        except requests.exceptions.RequestException as exc:
            raise RetryableIngestionError(f"network error calling {path}: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableIngestionError(
                f"{path} returned {response.status_code}: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise FatalIngestionError(
                f"{path} returned {response.status_code}: {response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise FatalIngestionError(f"{path} returned malformed JSON") from exc
        if not isinstance(data, list):
            raise FatalIngestionError(f"{path} returned unexpected response shape: {data!r}")
        return data

    @staticmethod
    def _row_to_candle(row: list) -> RawCandle:
        # Binance kline row shape:
        # [open_time, open, high, low, close, volume, close_time, ...]
        try:
            open_time = datetime.fromtimestamp(row[0] / 1000, tz=UTC)
            close_time = datetime.fromtimestamp(row[6] / 1000, tz=UTC)
            return RawCandle(
                open_time=open_time,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                close_time=close_time,
                is_closed=True,
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise FatalIngestionError(f"malformed kline row: {row!r}") from exc
