"""
The first REAL implementation of the MarketDataSource Protocol
PaperExecutionAdapter depends on (core/execution/paper_execution_adapter.py)
— step 6 of the spec. PaperExecutionAdapter itself needs no changes at
all: it was already written against that Protocol structurally, with
only a fake satisfying it in Stage 1 tests until now. This is exactly
the payoff of that Protocol boundary existing in the first place.

Owns a WebSocketConnection + MarketDataNormalizer, maintaining a live
"last known price per symbol" cache updated as normalized ticks arrive
on the feed. get_last_price() raises KeyError for a symbol nothing has
been received for yet — never returns a stale default (e.g. 0.0) that
a paper fill could silently use as a real price.

Thread-safety note: _on_message runs on WebSocketConnection's own
background thread; get_last_price() is called from whatever thread
PaperExecutionAdapter runs on. A single dict key assignment/lookup is
atomic under CPython's GIL, so the plain `dict` cache here needs no
extra lock — this is a narrow, specific case where that guarantee
applies, not a general license to skip locking elsewhere.
"""

import json
from collections.abc import Callable
from typing import Any

import structlog

from core.marketdata.market_data_normalizer import MarketDataNormalizer
from core.marketdata.websocket_connection import WebSocketConnection

logger = structlog.get_logger(__name__)


class LiveMarketDataSource:
    def __init__(
        self,
        url: str,
        heartbeat_timeout_s: float = 30.0,
        connection_factory: Callable[..., WebSocketConnection] | None = None,
        **connection_kwargs: Any,
    ) -> None:
        self._normalizer = MarketDataNormalizer()
        self._last_prices: dict[str, float] = {}
        factory = connection_factory or WebSocketConnection
        self._connection = factory(
            url=url,
            on_message=self._on_message,
            heartbeat_timeout_s=heartbeat_timeout_s,
            **connection_kwargs,
        )

    def start(self) -> None:
        self._connection.connect()

    def stop(self) -> None:
        self._connection.close()

    def is_connected(self) -> bool:
        return self._connection.is_alive()

    def get_last_price(self, symbol: str) -> float:
        if symbol not in self._last_prices:
            raise KeyError(f"no market data received yet for symbol: {symbol}")
        return self._last_prices[symbol]

    def _on_message(self, raw_message: str) -> None:
        try:
            raw_payload = json.loads(raw_message)
        except (TypeError, ValueError):
            logger.warning("market_data_message_not_json", message=raw_message[:200])
            return

        try:
            tick = self._normalizer.normalize(raw_payload)
        except ValueError as exc:
            logger.warning("market_data_normalize_failed", error=str(exc))
            return

        self._last_prices[tick.symbol] = tick.price
