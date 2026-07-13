"""
GapDetectionService (spec 4.7). Compares the set of expected bar
timestamps (generated at the timeframe's interval from
earliest_available_at to last_ingested_open_time) against what's
actually in raw_ohlcv; any missing timestamp becomes/updates a pending
row in ingestion_gap. Consecutive missing timestamps are collapsed into
a single (gap_start, gap_end) range rather than one row per bar.

Design note: the ingestion_run_log.run_type CHECK constraint only
allows 'backfill' | 'incremental' | 'gap_repair' | 'data_quality' — gap
detection isn't in that enum (spec section 3.4). Rather than widen a
constraint the spec didn't ask for, this service records its own audit
trail via ingestion_watermark.last_gap_scan_at, which is enough to
answer "did a scan run, and when" for this component.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ingestion.timeframe import timeframe_to_timedelta
from core.ingestion.watermark import get_watermark, upsert_watermark


@dataclass
class DetectedGap:
    gap_start: datetime
    gap_end: datetime


@dataclass
class GapDetectionResult:
    gaps_found: list[DetectedGap]
    skipped_reason: str | None = None


class GapDetectionService:
    def __init__(self, db: Session):
        self.db = db

    def run(
        self, exchange: str, symbol: str, timeframe: str, now: datetime | None = None
    ) -> GapDetectionResult:
        now = now or datetime.now(UTC)
        watermark = get_watermark(self.db, exchange, symbol, timeframe)
        if (
            watermark is None
            or watermark.earliest_available_at is None
            or watermark.last_ingested_open_time is None
        ):
            return GapDetectionResult(
                gaps_found=[], skipped_reason="no watermark — backfill must run first"
            )

        interval = timeframe_to_timedelta(timeframe)
        missing = (
            self.db.execute(
                text("""
                SELECT expected.ts AS open_time
                FROM generate_series(:start, :end, CAST(:step AS interval)) AS expected(ts)
                LEFT JOIN raw_ohlcv actual
                    ON actual.exchange = :exchange AND actual.symbol = :symbol
                    AND actual.timeframe = :timeframe AND actual.open_time = expected.ts
                WHERE actual.open_time IS NULL
                ORDER BY expected.ts
                """),
                {
                    "start": watermark.earliest_available_at,
                    "end": watermark.last_ingested_open_time,
                    "step": f"{int(interval.total_seconds())} seconds",
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                },
            )
            .scalars()
            .all()
        )

        gaps = _collapse_into_ranges(list(missing), interval)

        for gap in gaps:
            self._record_gap(exchange, symbol, timeframe, gap)

        upsert_watermark(self.db, exchange, symbol, timeframe, last_gap_scan_at=now)

        return GapDetectionResult(gaps_found=gaps)

    def _record_gap(self, exchange: str, symbol: str, timeframe: str, gap: DetectedGap) -> None:
        # confirmed_absent is terminal: don't re-flag a gap the repair
        # service has already given up on (spec 4.8 step 4).
        already_confirmed_absent = self.db.execute(
            text("""
                SELECT 1 FROM ingestion_gap
                WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
                  AND gap_start = :gap_start AND gap_end = :gap_end AND status = 'confirmed_absent'
                """),
            {
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "gap_start": gap.gap_start,
                "gap_end": gap.gap_end,
            },
        ).first()
        if already_confirmed_absent:
            return

        self.db.execute(
            text("""
                INSERT INTO ingestion_gap
                    (exchange, symbol, timeframe, gap_start, gap_end, status, detected_at)
                VALUES (:exchange, :symbol, :timeframe, :gap_start, :gap_end, 'pending', now())
                ON CONFLICT (exchange, symbol, timeframe, gap_start, gap_end) DO NOTHING
                """),
            {
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "gap_start": gap.gap_start,
                "gap_end": gap.gap_end,
            },
        )
        self.db.commit()


def _collapse_into_ranges(
    missing_timestamps: list[datetime], interval: timedelta
) -> list[DetectedGap]:
    if not missing_timestamps:
        return []
    ranges: list[DetectedGap] = []
    start = prev = missing_timestamps[0]
    for ts in missing_timestamps[1:]:
        if ts - prev == interval:
            prev = ts
            continue
        ranges.append(DetectedGap(gap_start=start, gap_end=prev))
        start = prev = ts
    ranges.append(DetectedGap(gap_start=start, gap_end=prev))
    return ranges
