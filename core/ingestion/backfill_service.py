"""
BackfillService (spec 4.5). One invocation walks a (exchange, symbol,
timeframe) forward from its earliest available candle to now, paginated
by the exchange's per-request limit, and is idempotent: once
ingestion_watermark.backfill_complete is True, a re-run is a logged
no-op rather than re-fetching everything from the exchange again.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import structlog
from sqlalchemy.orm import Session

from core.ingestion.candle_validator import validate_candles
from core.ingestion.config import IngestionConfig
from core.ingestion.event_bus import EventBus
from core.ingestion.events import BackfillCompleted
from core.ingestion.exchange_adapter import ExchangeAdapter
from core.ingestion.rate_limiter import RateLimiter
from core.ingestion.raw_ohlcv_store import upsert_candles
from core.ingestion.retry_policy import RetryPolicy
from core.ingestion.run_log import RunLogEntry, record_run
from core.ingestion.timeframe import timeframe_to_timedelta
from core.ingestion.types import RawCandle
from core.ingestion.watermark import get_watermark, upsert_watermark

logger = structlog.get_logger(__name__)

T = TypeVar("T")


@dataclass
class BackfillResult:
    run_id: int
    stored_count: int
    received_count: int
    skipped: bool


class BackfillService:
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
    ) -> BackfillResult:
        now = now or datetime.now(UTC)
        started_at = now

        watermark = get_watermark(self.db, exchange, symbol, timeframe)
        if watermark is not None and watermark.backfill_complete:
            skip_run_id = record_run(
                self.db,
                RunLogEntry(
                    run_type="backfill",
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    status="success",
                    requested_range={},
                    skipped_reason="backfill already complete",
                ),
            )
            return BackfillResult(
                run_id=skip_run_id, stored_count=0, received_count=0, skipped=True
            )

        earliest = self._call_with_resilience(
            lambda: self.adapter.earliest_available(symbol, timeframe)
        )
        if earliest is None:
            earliest = now - timedelta(days=365 * self.config.default_backfill_years)

        interval = timeframe_to_timedelta(timeframe)
        cursor = earliest
        received_count = 0
        stored_count = 0
        validation_failures: list[dict] = []
        retries = 0
        last_stored_open_time: datetime | None = None
        run_id: int | None = None

        try:
            while cursor <= now:

                def fetch_next_batch(cursor: datetime = cursor) -> list[RawCandle]:
                    return self.adapter.fetch_klines(
                        symbol, timeframe, cursor, now, self.config.per_request_candle_limit
                    )

                batch = self._call_with_resilience(fetch_next_batch)
                if not batch:
                    break

                received_count += len(batch)
                result = validate_candles(batch, timeframe, now)
                validation_failures.extend(
                    {"open_time": f.open_time.isoformat(), "reason": f.reason}
                    for f in result.failures
                )

                inserted = upsert_candles(
                    self.db, exchange, symbol, timeframe, result.valid, run_id
                )
                stored_count += inserted
                if result.valid:
                    last_stored_open_time = max(
                        last_stored_open_time or result.valid[0].open_time,
                        max(c.open_time for c in result.valid),
                    )

                cursor = batch[-1].open_time + interval
                if len(batch) < self.config.per_request_candle_limit:
                    break
        except Exception as exc:
            run_id = record_run(
                self.db,
                RunLogEntry(
                    run_type="backfill",
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    status="failed",
                    requested_range={"start": earliest.isoformat(), "end": now.isoformat()},
                    received_count=received_count,
                    stored_count=stored_count,
                    validation_failures=validation_failures,
                    retries=retries,
                    error_message=str(exc),
                ),
            )
            raise

        upsert_watermark(
            self.db,
            exchange,
            symbol,
            timeframe,
            earliest_available_at=earliest,
            last_ingested_open_time=last_stored_open_time,
            backfill_complete=True,
        )

        status = "success" if not validation_failures else "partial"
        run_id = record_run(
            self.db,
            RunLogEntry(
                run_type="backfill",
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                status=status,
                requested_range={"start": earliest.isoformat(), "end": now.isoformat()},
                received_count=received_count,
                stored_count=stored_count,
                validation_failures=validation_failures,
                retries=retries,
            ),
        )

        if self.event_bus is not None:
            self.event_bus.publish(
                BackfillCompleted(
                    exchange=exchange, symbol=symbol, timeframe=timeframe, stored_count=stored_count
                )
            )

        logger.info(
            "backfill_completed",
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            stored_count=stored_count,
            received_count=received_count,
        )
        return BackfillResult(
            run_id=run_id, stored_count=stored_count, received_count=received_count, skipped=False
        )

    def _call_with_resilience(self, fn: Callable[[], T]) -> T:
        return self.retry_policy.execute(lambda: self._rate_limited(fn))

    def _rate_limited(self, fn: Callable[[], T]) -> T:
        self.rate_limiter.acquire()
        return fn()
