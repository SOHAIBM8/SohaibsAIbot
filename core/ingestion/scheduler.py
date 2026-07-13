"""
Scheduler (spec 4.10). A single long-running process coordinating:
backfill on new tracked_instruments rows, incremental updates per
timeframe cadence, nightly gap detection + data quality checks, and gap
repair sweeps. Deliberately an in-process loop, not Airflow/Prefect —
that's the upgrade path if job orchestration complexity actually grows
across many exchanges/symbols, not a day-one requirement (spec
non-goals).

`run_once()` does one full sweep and is what tests call directly, for
determinism. `run_forever()` wraps it in a sleep loop for the real
containerized process.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ingestion.backfill_service import BackfillService
from core.ingestion.config import IngestionConfig
from core.ingestion.data_quality_service import DataQualityService
from core.ingestion.event_bus import EventBus
from core.ingestion.exchange_adapter import ExchangeAdapter
from core.ingestion.gap_detection_service import GapDetectionService
from core.ingestion.gap_repair_service import GapRepairService
from core.ingestion.incremental_update_service import IncrementalUpdateService
from core.ingestion.watermark import get_watermark

logger = structlog.get_logger(__name__)

NIGHTLY_INTERVAL = timedelta(hours=24)


@dataclass
class SweepSummary:
    backfills_run: list[str] = field(default_factory=list)
    incrementals_run: list[str] = field(default_factory=list)
    gap_scans_run: list[str] = field(default_factory=list)
    data_quality_runs: list[str] = field(default_factory=list)
    gap_repairs_run: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        db: Session,
        adapters: dict[str, ExchangeAdapter],
        config: IngestionConfig,
        event_bus: EventBus | None = None,
    ):
        self.db = db
        self.adapters = adapters
        self.config = config
        self.event_bus = event_bus
        self._stop = threading.Event()

    def run_once(self, now: datetime | None = None) -> SweepSummary:
        now = now or datetime.now(UTC)
        summary = SweepSummary()

        instruments = self.db.execute(text("""
                    SELECT exchange, symbol, timeframe FROM tracked_instruments
                    WHERE active = TRUE
                    """)).mappings().all()

        for instrument in instruments:
            exchange, symbol, timeframe = (
                instrument["exchange"],
                instrument["symbol"],
                instrument["timeframe"],
            )
            key = f"{exchange}:{symbol}:{timeframe}"
            adapter = self.adapters.get(exchange)
            if adapter is None:
                logger.warning("scheduler_no_adapter", exchange=exchange)
                continue

            watermark = get_watermark(self.db, exchange, symbol, timeframe)

            if watermark is None or not watermark.backfill_complete:
                BackfillService(self.db, adapter, self.config, event_bus=self.event_bus).run(
                    exchange, symbol, timeframe, now
                )
                summary.backfills_run.append(key)
                continue  # nothing else makes sense until backfill has run

            cadence = self.config.incremental_polling_seconds.get(timeframe, 3600)
            if self._due(watermark.last_ingested_open_time, cadence, now):
                IncrementalUpdateService(
                    self.db, adapter, self.config, event_bus=self.event_bus
                ).run(exchange, symbol, timeframe, now)
                summary.incrementals_run.append(key)

            if self._due(watermark.last_gap_scan_at, NIGHTLY_INTERVAL.total_seconds(), now):
                GapDetectionService(self.db).run(exchange, symbol, timeframe, now)
                summary.gap_scans_run.append(key)

            if self._due(
                watermark.last_data_quality_check_at, NIGHTLY_INTERVAL.total_seconds(), now
            ):
                DataQualityService(
                    self.db, self.config, adapter=adapter, event_bus=self.event_bus
                ).run(exchange, symbol, timeframe, now)
                summary.data_quality_runs.append(key)

            repair_summary = GapRepairService(
                self.db, adapter, self.config, event_bus=self.event_bus
            ).run(exchange, symbol, timeframe, now)
            if (
                repair_summary.repaired
                or repair_summary.still_missing
                or repair_summary.confirmed_absent
            ):
                summary.gap_repairs_run.append(key)

        return summary

    def run_forever(
        self, poll_interval_seconds: float = 30.0, sleep: Callable[[float], None] | None = None
    ) -> None:
        sleep = sleep or time.sleep
        self._stop.clear()
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("scheduler_sweep_failed")
            sleep(poll_interval_seconds)

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _due(last_run_at: datetime | None, interval_seconds: float, now: datetime) -> bool:
        if last_run_at is None:
            return True
        return (now - last_run_at).total_seconds() >= interval_seconds
