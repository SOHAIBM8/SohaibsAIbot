"""
Raw feed payload -> normalized internal event. Kept separate so
Stage 2's real exchange payloads plug in here without touching
anything downstream — same reasoning as pandas_ta_adapter.py being the
only file that knows about an external shape (core/indicators/
pandas_ta_adapter.py's docstring).

Stage 1's feed is fake/simulated (see websocket_connection.py's
docstring) — there is no real exchange payload shape to match yet, so
this normalizer defines ITS OWN minimal wire format: a JSON object with
symbol/price/timestamp. Malformed or incomplete payloads raise
ValueError rather than silently producing a tick with a missing/
default field a downstream paper fill could use in a real trade.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NormalizedTick:
    symbol: str
    price: float
    timestamp: datetime


class MarketDataNormalizer:
    def normalize(self, raw_payload: dict) -> NormalizedTick:
        missing = [key for key in ("symbol", "price", "timestamp") if key not in raw_payload]
        if missing:
            raise ValueError(f"market data payload missing required field(s): {missing}")

        symbol = raw_payload["symbol"]
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"invalid symbol in market data payload: {symbol!r}")

        try:
            price = float(raw_payload["price"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid price in market data payload: {raw_payload['price']!r}"
            ) from exc
        if price <= 0:
            raise ValueError(f"non-positive price in market data payload: {price}")

        try:
            timestamp = datetime.fromisoformat(raw_payload["timestamp"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid timestamp in market data payload: {raw_payload['timestamp']!r}"
            ) from exc

        return NormalizedTick(symbol=symbol, price=price, timestamp=timestamp)
