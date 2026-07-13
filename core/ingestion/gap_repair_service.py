"""
GapRepairService (spec 4.8). For each pending gap whose
next_attempt_after has elapsed, re-fetch that exact range. Data found ->
repaired. Still missing -> attempts += 1, next_attempt_after = now +
retry_interval; after gap_repair_max_attempts, the gap becomes
confirmed_absent — a terminal state GapDetectionService will not
re-flag.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ingestion.candle_validator import validate_candles
from core.ingestion.config import IngestionConfig
from core.ingestion.event_bus import EventBus
from core.ingestion.events import GapRepaired
from core.ingestion.exchange_adapter import ExchangeAdapter
from core.ingestion.rate_limiter import RateLimiter
from core.ingestion.raw_ohlcv_store import upsert_candles
from core.ingestion.retry_policy import RetryPolicy
from core.ingestion.run_log import RunLogEntry, record_run
from core.ingestion.timeframe import timeframe_to_timedelta
from core.ingestion.types import RawCandle

logger = structlog.get_logger(__name__)


@dataclass
class GapRepairSummary:
    run_id: int
    repaired: int
    still_missing: int
    confirmed_absent: int


class GapRepairService:
    def __init__(
        self,
        db: Session,
        adapter: ExchangeAdapter,
        config: IngestionConfig,
        rate_limiter: RateLimiter | None = None,
        retry_policy: RetryPolicy | None = None,
        event_bus: EventBus | None = None,
    ):
        self.db = db
        self.adapter = adapter
        self.config = config
        self.rate_limiter = rate_limiter or RateLimiter(adapter.rate_limit_config)
        self.retry_policy = retry_policy or RetryPolicy()
        self.event_bus = event_bus

    def run(
        self, exchange: str, symbol: str, timeframe: str, now: datetime | None = None
    ) -> GapRepairSummary:
        now = now or datetime.now(UTC)
        started_at = now

        pending_gaps = (
            self.db.execute(
                text("""
                SELECT gap_id, gap_start, gap_end, attempts
                FROM ingestion_gap
                WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
                  AND status = 'pending'
                  AND (next_attempt_after IS NULL OR next_attempt_after <= :now)
                ORDER BY gap_start
                """),
                {"exchange": exchange, "symbol": symbol, "timeframe": timeframe, "now": now},
            )
            .mappings()
            .all()
        )

        repaired = still_missing = confirmed_absent = 0
        received_count = stored_count = 0
        validation_failures: list[dict] = []

        for gap in pending_gaps:
            self.rate_limiter.acquire()
            interval = timeframe_to_timedelta(timeframe)

            def fetch_gap_range(
                gap: Mapping = gap, interval: timedelta = interval
            ) -> list[RawCandle]:
                return self.adapter.fetch_klines(
                    symbol,
                    timeframe,
                    gap["gap_start"],
                    gap["gap_end"] + interval,
                    self.config.per_request_candle_limit,
                )

            batch = self.retry_policy.execute(fetch_gap_range)
            received_count += len(batch)
            result = validate_candles(batch, timeframe, now)
            validation_failures.extend(
                {"open_time": f.open_time.isoformat(), "reason": f.reason} for f in result.failures
            )
            inserted = upsert_candles(self.db, exchange, symbol, timeframe, result.valid, None)
            stored_count += inserted

            if self._range_fully_covered(
                exchange, symbol, timeframe, gap["gap_start"], gap["gap_end"], interval
            ):
                self._mark_repaired(gap["gap_id"], now)
                repaired += 1
                if self.event_bus is not None:
                    self.event_bus.publish(
                        GapRepaired(
                            exchange=exchange,
                            symbol=symbol,
                            timeframe=timeframe,
                            gap_start=gap["gap_start"].isoformat(),
                            gap_end=gap["gap_end"].isoformat(),
                        )
                    )
            else:
                attempts = gap["attempts"] + 1
                if attempts >= self.config.gap_repair_max_attempts:
                    self._mark_confirmed_absent(gap["gap_id"], attempts, now)
                    confirmed_absent += 1
                else:
                    next_attempt_after = now + timedelta(
                        hours=self.config.gap_repair_retry_interval_hours
                    )
                    self._mark_still_pending(gap["gap_id"], attempts, now, next_attempt_after)
                    still_missing += 1

        status = "success" if not validation_failures else "partial"
        run_id = record_run(
            self.db,
            RunLogEntry(
                run_type="gap_repair",
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                status=status,
                requested_range={"gaps_processed": len(pending_gaps)},
                received_count=received_count,
                stored_count=stored_count,
                validation_failures=validation_failures,
                skipped_reason="no pending gaps due for repair" if not pending_gaps else None,
            ),
        )

        return GapRepairSummary(
            run_id=run_id,
            repaired=repaired,
            still_missing=still_missing,
            confirmed_absent=confirmed_absent,
        )

    def _range_fully_covered(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        gap_start: datetime,
        gap_end: datetime,
        interval: timedelta,
    ) -> bool:
        missing: int = self.db.execute(
            text("""
                SELECT count(*)
                FROM generate_series(:start, :end, CAST(:step AS interval)) AS expected(ts)
                LEFT JOIN raw_ohlcv actual
                    ON actual.exchange = :exchange AND actual.symbol = :symbol
                    AND actual.timeframe = :timeframe AND actual.open_time = expected.ts
                WHERE actual.open_time IS NULL
                """),
            {
                "start": gap_start,
                "end": gap_end,
                "step": f"{int(interval.total_seconds())} seconds",
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
            },
        ).scalar_one()
        return missing == 0

    def _mark_repaired(self, gap_id: int, now: datetime) -> None:
        self.db.execute(
            text("""
                UPDATE ingestion_gap SET status = 'repaired', resolved_at = :now
                WHERE gap_id = :gap_id
                """),
            {"now": now, "gap_id": gap_id},
        )
        self.db.commit()

    def _mark_confirmed_absent(self, gap_id: int, attempts: int, now: datetime) -> None:
        self.db.execute(
            text("""
                UPDATE ingestion_gap
                SET status = 'confirmed_absent', attempts = :attempts, last_attempt_at = :now,
                    resolved_at = :now
                WHERE gap_id = :gap_id
                """),
            {"attempts": attempts, "now": now, "gap_id": gap_id},
        )
        self.db.commit()

    def _mark_still_pending(
        self, gap_id: int, attempts: int, now: datetime, next_attempt_after: datetime
    ) -> None:
        self.db.execute(
            text("""
                UPDATE ingestion_gap
                SET attempts = :attempts, last_attempt_at = :now,
                    next_attempt_after = :next_attempt_after
                WHERE gap_id = :gap_id
                """),
            {
                "attempts": attempts,
                "now": now,
                "next_attempt_after": next_attempt_after,
                "gap_id": gap_id,
            },
        )
        self.db.commit()
