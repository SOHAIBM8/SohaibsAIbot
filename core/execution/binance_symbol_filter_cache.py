"""
Fetches and caches GET /exchangeInfo, extracting LOT_SIZE/PRICE_FILTER/
MIN_NOTIONAL per symbol (decision #5: every order pre-validated against
these before submission). Binance itself would reject a violating
order anyway, but failing fast locally avoids burning a signed
request/rate-limit budget and a real API round trip on an order we
already know is invalid — and avoids the ambiguous-failure handling
entirely for a class of failure that's fully knowable in advance.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import requests
import structlog

from core.ingestion.errors import FatalIngestionError, RetryableIngestionError

logger = structlog.get_logger(__name__)


@dataclass
class SymbolFilters:
    symbol: str
    min_qty: float
    max_qty: float
    step_size: float
    min_price: float
    max_price: float
    tick_size: float
    min_notional: float
    fetched_at: datetime


class FilterViolationError(ValueError):
    """Raised by validate() — a candidate order violates a cached
    filter. BinanceExecutionAdapter catches this and rejects the order
    locally, never submitting it to the exchange."""


class SymbolFilterCache:
    def __init__(
        self,
        base_url: str,
        session: requests.Session | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._base_url = base_url
        self._session = session or requests.Session()
        self._timeout = timeout_seconds
        self._cache: dict[str, SymbolFilters] = {}

    def refresh(self, symbols: list[str] | None = None) -> None:
        """Fetch /exchangeInfo and (re)populate the cache. symbols=None
        caches every symbol Binance returns; passing a narrower list
        (the tracked-instrument set) keeps parsing cheap."""
        try:
            response = self._session.get(
                f"{self._base_url}/api/v3/exchangeInfo", timeout=self._timeout
            )
        except requests.exceptions.RequestException as exc:
            raise RetryableIngestionError(f"exchangeInfo fetch failed: {exc}") from exc

        if response.status_code >= 400:
            raise RetryableIngestionError(
                f"exchangeInfo returned {response.status_code}: {response.text[:200]}"
            )

        data = response.json()
        now = datetime.now(UTC)
        for entry in data.get("symbols", []):
            symbol = entry["symbol"]
            if symbols is not None and symbol not in symbols:
                continue
            self._cache[symbol] = self._parse_filters(symbol, entry.get("filters", []), now)

    def get(self, symbol: str) -> SymbolFilters:
        if symbol not in self._cache:
            raise KeyError(f"no cached filters for symbol={symbol!r} — call refresh() first")
        return self._cache[symbol]

    def validate(self, symbol: str, quantity: float, price: float | None) -> None:
        """Raises FilterViolationError if quantity/price/notional violate
        the symbol's cached filters. price=None for a MARKET order —
        MIN_NOTIONAL can't be checked without a reference price, so
        it's skipped for market orders; Binance validates a market
        order's notional against the live book at submission time,
        which this static cache can't replicate."""
        filters = self.get(symbol)

        if not (filters.min_qty <= quantity <= filters.max_qty):
            raise FilterViolationError(
                f"{symbol}: quantity {quantity} outside [{filters.min_qty}, {filters.max_qty}]"
            )
        if not _is_step_multiple(quantity - filters.min_qty, filters.step_size):
            raise FilterViolationError(
                f"{symbol}: quantity {quantity} is not a multiple of step_size {filters.step_size}"
            )

        if price is None:
            return

        if not (filters.min_price <= price <= filters.max_price):
            raise FilterViolationError(
                f"{symbol}: price {price} outside [{filters.min_price}, {filters.max_price}]"
            )
        if not _is_step_multiple(price - filters.min_price, filters.tick_size):
            raise FilterViolationError(
                f"{symbol}: price {price} is not a multiple of tick_size {filters.tick_size}"
            )
        notional = quantity * price
        if notional < filters.min_notional:
            raise FilterViolationError(
                f"{symbol}: notional {notional} is below min_notional {filters.min_notional}"
            )

    @staticmethod
    def _parse_filters(
        symbol: str, filters_list: list[dict], fetched_at: datetime
    ) -> SymbolFilters:
        by_type = {f["filterType"]: f for f in filters_list}
        lot = by_type.get("LOT_SIZE", {})
        price_filter = by_type.get("PRICE_FILTER", {})
        # Binance renamed MIN_NOTIONAL -> NOTIONAL on some symbols;
        # accept either so a mid-migration exchangeInfo response still
        # parses correctly instead of silently defaulting to 0.
        min_notional = by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL") or {}
        try:
            return SymbolFilters(
                symbol=symbol,
                min_qty=float(lot.get("minQty", 0)),
                max_qty=float(lot.get("maxQty", float("inf"))),
                step_size=float(lot.get("stepSize", 0)) or 1e-8,
                min_price=float(price_filter.get("minPrice", 0)),
                max_price=float(price_filter.get("maxPrice", float("inf"))),
                tick_size=float(price_filter.get("tickSize", 0)) or 1e-8,
                min_notional=float(min_notional.get("minNotional", 0)),
                fetched_at=fetched_at,
            )
        except (TypeError, ValueError) as exc:
            raise FatalIngestionError(
                f"malformed exchangeInfo filters for {symbol}: {exc}"
            ) from exc


def _is_step_multiple(value: float, step: float, tolerance: float = 1e-8) -> bool:
    if step <= 0:
        return True
    remainder = value % step
    return remainder < tolerance or (step - remainder) < tolerance
