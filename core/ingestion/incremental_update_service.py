"""
IncrementalUpdateService (spec 4.6). Fetches only candles after
ingestion_watermark.last_ingested_open_time — requires a prior
successful backfill to have set that watermark; if none exists, the
run is a logged no-op rather than silently guessing a start point.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from core.ingestion.candle_validator import validate_candles
from core.ingestion.config import IngestionConfig
from core.ingestion.event_bus import EventBus
from core.ingestion.events import CandlesIngested
from core.ingestion.exchange_adapter import ExchangeAdapter
from core.ingestion.rate_limiter import RateLimiter
from core.ingestion.raw_ohlcv_store import upsert_candles
from core.ingestion.retry_policy import RetryPolicy
from core.ingestion.run_log import RunLogEntry, record_run
from core.ingestion.timeframe import timeframe_to_timedelta
from core.ingestion.watermark import get_watermark, upsert_watermark

logger = structlog.get_logger(__name__)


@dataclass
class IncrementalUpdateResult:
    run_id: int
    stored_count: int
    received_count: int
    skipped_reason: str | None


class IncrementalUpdateService:
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
    ) -> IncrementalUpdateResult:
        now = now or datetime.now(UTC)
        started_at = now

        watermark = get_watermark(self.db, exchange, symbol, timeframe)
        if watermark is None or watermark.last_ingested_open_time is None:
            run_id = record_run(
                self.db,
                RunLogEntry(
                    run_type="incremental",
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    status="success",
                    requested_range={},
                    skipped_reason="no watermark — backfill must run first",
                ),
            )
            return IncrementalUpdateResult(
                run_id=run_id,
                stored_count=0,
                received_count=0,
                skipped_reason="no watermark — backfill must run first",
            )

        interval = timeframe_to_timedelta(timeframe)
        start_time = watermark.last_ingested_open_time + interval

        if start_time > now:
            run_id = record_run(
                self.db,
                RunLogEntry(
                    run_type="incremental",
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    status="success",
                    requested_range={"start": start_time.isoformat(), "end": now.isoformat()},
                    skipped_reason="no new candles since watermark",
                ),
            )
            return IncrementalUpdateResult(
                run_id=run_id,
                stored_count=0,
                received_count=0,
                skipped_reason="no new candles since watermark",
            )

        self.rate_limiter.acquire()
        batch = self.retry_policy.execute(
            lambda: self.adapter.fetch_klines(
                symbol, timeframe, start_time, now, self.config.per_request_candle_limit
            )
        )

        result = validate_candles(batch, timeframe, now)
        validation_failures = [
            {"open_time": f.open_time.isoformat(), "reason": f.reason} for f in result.failures
        ]
        stored_count = upsert_candles(self.db, exchange, symbol, timeframe, result.valid, None)

        if result.valid:
            latest = max(c.open_time for c in result.valid)
            upsert_watermark(self.db, exchange, symbol, timeframe, last_ingested_open_time=latest)

        status = "success" if not validation_failures else "partial"
        run_id = record_run(
            self.db,
            RunLogEntry(
                run_type="incremental",
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                status=status,
                requested_range={"start": start_time.isoformat(), "end": now.isoformat()},
                received_count=len(batch),
                stored_count=stored_count,
                validation_failures=validation_failures,
            ),
        )

        if self.event_bus is not None and stored_count > 0:
            self.event_bus.publish(
                CandlesIngested(
                    exchange=exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    count=stored_count,
                    run_id=run_id,
                )
            )

        return IncrementalUpdateResult(
            run_id=run_id, stored_count=stored_count, received_count=len(batch), skipped_reason=None
        )
