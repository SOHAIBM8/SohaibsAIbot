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

Design note (rule 9, docs/ai_assistant_spec.md step 5): that spec
requires DailySummaryJob to be "triggered by the existing Scheduler,
not a new scheduling mechanism" — decision #7 (nightly-scheduled only,
never synchronous from a trading event). The optional
`daily_summary_job` param below is the entire integration surface:
Scheduler gains no ai_assistant import at module scope (only inside
the branch that uses it, avoiding a hard dependency for every existing
ingestion-only caller/test), and every existing test that doesn't pass
this param is completely unaffected.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    # Import only for type checking — avoids ingestion (an already-
    # complete, ai_assistant-agnostic component) gaining a hard runtime
    # dependency on core.ai_assistant just to type-hint an optional param.
    from core.ai_assistant.daily_summary_job import DailySummaryJob

logger = structlog.get_logger(__name__)

NIGHTLY_INTERVAL = timedelta(hours=24)


@dataclass
class SweepSummary:
    backfills_run: list[str] = field(default_factory=list)
    incrementals_run: list[str] = field(default_factory=list)
    gap_scans_run: list[str] = field(default_factory=list)
    data_quality_runs: list[str] = field(default_factory=list)
    gap_repairs_run: list[str] = field(default_factory=list)
    daily_summaries_run: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        db: Session,
        adapters: dict[str, ExchangeAdapter],
        config: IngestionConfig,
        event_bus: EventBus | None = None,
        daily_summary_job: "DailySummaryJob | None" = None,
    ):
        self.db = db
        self.adapters = adapters
        self.config = config
        self.event_bus = event_bus
        self.daily_summary_job = daily_summary_job
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

        if self.daily_summary_job is not None:
            self._run_daily_summaries(summary, now)

        return summary

    def _run_daily_summaries(self, summary: SweepSummary, now: datetime) -> None:
        assert self.daily_summary_job is not None
        accounts = (
            self.db.execute(text("SELECT account_id, last_daily_summary_at FROM paper_accounts"))
            .mappings()
            .all()
        )

        for account in accounts:
            account_id = account["account_id"]
            if not self._due(
                account["last_daily_summary_at"], NIGHTLY_INTERVAL.total_seconds(), now
            ):
                continue
            try:
                self.daily_summary_job.run_for_account(account_id, now.date())
            except LookupError:
                # No account_snapshots data to summarize yet (Stage 1
                # has no snapshot-writer — see DailySummaryContext's
                # docstring). Not a scheduler failure; nothing to do
                # until that gap is closed elsewhere.
                logger.info("daily_summary_skipped_no_equity_data", account_id=account_id)
                continue
            self.db.execute(
                text("""
                    UPDATE paper_accounts SET last_daily_summary_at = :now
                    WHERE account_id = :account_id
                    """),
                {"now": now, "account_id": account_id},
            )
            self.db.commit()
            summary.daily_summaries_run.append(account_id)

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
