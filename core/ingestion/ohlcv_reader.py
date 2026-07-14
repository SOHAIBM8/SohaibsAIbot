"""
Read-only access to raw_ohlcv — added for the dashboard's Live Market
page (docs/dashboard_ui_spec.md section 9). No reader class existed
for this table before now: everything under core/ingestion/ is a
write-path orchestrator (backfill_service.py, incremental_update_service.py,
raw_ohlcv_store.py's upsert_candles). This is new read-only SQL over
an already-real, already-populated hypertable — the ingestion pipeline
genuinely writes real exchange data here, unlike the fake Stage-1 feed
core/marketdata/live_market_data_source.py streams from (see
api/routes/market.py's module docstring for why a live WebSocket
ticker isn't built this step).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session


@dataclass
class CandleRecord:
    exchange: str
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class OHLCVReader:
    def __init__(self, db: Session):
        self.db = db

    def list_candles(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[CandleRecord]:
        conditions = ["exchange = :exchange", "symbol = :symbol", "timeframe = :timeframe"]
        params: dict[str, object] = {
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": limit,
        }
        if start is not None:
            conditions.append("open_time >= :start")
            params["start"] = start
        if end is not None:
            conditions.append("open_time <= :end")
            params["end"] = end

        rows = (
            self.db.execute(
                text(f"""
                    SELECT exchange, symbol, timeframe, open_time, open, high, low, close, volume
                    FROM raw_ohlcv
                    WHERE {" AND ".join(conditions)}
                    ORDER BY open_time DESC
                    LIMIT :limit
                    """),
                params,
            )
            .mappings()
            .all()
        )
        # Most-recent-first from the query (so LIMIT keeps the latest N
        # candles), reversed here to chronological order for charting.
        return list(reversed([self._row_to_candle(row) for row in rows]))

    @staticmethod
    def _row_to_candle(row: RowMapping) -> CandleRecord:
        return CandleRecord(
            exchange=row["exchange"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            open_time=row["open_time"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
