"""
Shared reader/writer for ingestion_watermark — every service that
tracks per-instrument progress (backfill, incremental, gap detection)
goes through these two functions rather than each writing its own
upsert SQL.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class Watermark:
    exchange: str
    symbol: str
    timeframe: str
    earliest_available_at: datetime | None
    last_ingested_open_time: datetime | None
    backfill_complete: bool
    last_gap_scan_at: datetime | None
    last_data_quality_check_at: datetime | None


def get_watermark(db: Session, exchange: str, symbol: str, timeframe: str) -> Watermark | None:
    row = (
        db.execute(
            text("""
            SELECT exchange, symbol, timeframe, earliest_available_at,
                   last_ingested_open_time, backfill_complete, last_gap_scan_at,
                   last_data_quality_check_at
            FROM ingestion_watermark
            WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
            """),
            {"exchange": exchange, "symbol": symbol, "timeframe": timeframe},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return Watermark(**dict(row))


def upsert_watermark(
    db: Session,
    exchange: str,
    symbol: str,
    timeframe: str,
    *,
    earliest_available_at: datetime | None = None,
    last_ingested_open_time: datetime | None = None,
    backfill_complete: bool | None = None,
    last_gap_scan_at: datetime | None = None,
    last_data_quality_check_at: datetime | None = None,
) -> None:
    """Partial update: any field left as None is left unchanged on an
    existing row (via COALESCE), but seeds sensible defaults on first
    insert. Callers pass only the fields their service is responsible
    for advancing."""
    db.execute(
        text("""
            INSERT INTO ingestion_watermark (
                exchange, symbol, timeframe, earliest_available_at,
                last_ingested_open_time, backfill_complete, last_gap_scan_at,
                last_data_quality_check_at, updated_at
            ) VALUES (
                :exchange, :symbol, :timeframe, :earliest_available_at,
                :last_ingested_open_time, COALESCE(:backfill_complete, FALSE),
                :last_gap_scan_at, :last_data_quality_check_at, now()
            )
            ON CONFLICT (exchange, symbol, timeframe) DO UPDATE SET
                earliest_available_at = COALESCE(
                    EXCLUDED.earliest_available_at, ingestion_watermark.earliest_available_at
                ),
                last_ingested_open_time = COALESCE(
                    EXCLUDED.last_ingested_open_time, ingestion_watermark.last_ingested_open_time
                ),
                backfill_complete = COALESCE(
                    :backfill_complete, ingestion_watermark.backfill_complete
                ),
                last_gap_scan_at = COALESCE(
                    EXCLUDED.last_gap_scan_at, ingestion_watermark.last_gap_scan_at
                ),
                last_data_quality_check_at = COALESCE(
                    EXCLUDED.last_data_quality_check_at,
                    ingestion_watermark.last_data_quality_check_at
                ),
                updated_at = now()
            """),
        {
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "earliest_available_at": earliest_available_at,
            "last_ingested_open_time": last_ingested_open_time,
            "backfill_complete": backfill_complete,
            "last_gap_scan_at": last_gap_scan_at,
            "last_data_quality_check_at": last_data_quality_check_at,
        },
    )
    db.commit()
