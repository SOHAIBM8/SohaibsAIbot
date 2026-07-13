"""
Shared writer for raw_ohlcv. ON CONFLICT DO NOTHING everywhere — closed
candles are immutable, so a re-ingested range is a silent no-op rather
than an overwrite, which is what makes backfill/incremental/gap-repair
idempotent by construction (spec 4.5 step 3, section 6).
"""

from typing import cast

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from core.ingestion.types import RawCandle


def upsert_candles(
    db: Session,
    exchange: str,
    symbol: str,
    timeframe: str,
    candles: list[RawCandle],
    source_run_id: int | None,
) -> int:
    """Returns the number of rows actually inserted (excludes rows that
    already existed and were skipped by ON CONFLICT DO NOTHING)."""
    if not candles:
        return 0
    inserted = 0
    for candle in candles:
        result = cast(
            CursorResult,
            db.execute(
                text("""
                INSERT INTO raw_ohlcv (
                    exchange, symbol, timeframe, open_time, open, high, low, close,
                    volume, is_closed, source_run_id
                ) VALUES (
                    :exchange, :symbol, :timeframe, :open_time, :open, :high, :low, :close,
                    :volume, :is_closed, :source_run_id
                )
                ON CONFLICT (exchange, symbol, timeframe, open_time) DO NOTHING
                """),
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "open_time": candle.open_time,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "is_closed": candle.is_closed,
                    "source_run_id": source_run_id,
                },
            ),
        )
        inserted += result.rowcount
    db.commit()
    return inserted
