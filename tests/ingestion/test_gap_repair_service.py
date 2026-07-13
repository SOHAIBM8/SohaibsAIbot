from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from core.ingestion.backfill_service import BackfillService
from core.ingestion.config import IngestionConfig
from core.ingestion.gap_detection_service import GapDetectionService
from core.ingestion.gap_repair_service import GapRepairService
from core.ingestion.retry_policy import RetryPolicy
from core.ingestion.testing import FakeExchangeAdapter
from tests.ingestion.conftest import hourly_candles, make_candle


def _policy() -> RetryPolicy:
    return RetryPolicy(sleep=lambda s: None, rand=lambda: 0.0)


def _setup_gap(db, now):
    start = datetime(2024, 5, 1, tzinfo=UTC)
    all_candles = hourly_candles(start, 5)
    incomplete = [c for i, c in enumerate(all_candles) if i != 2]
    adapter = FakeExchangeAdapter(candles=incomplete, earliest=start)
    config = IngestionConfig(gap_repair_max_attempts=3, gap_repair_retry_interval_hours=24)
    BackfillService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=now
    )
    GapDetectionService(db).run("fake", "BTC/USDT", "1h", now=now)
    missing_open_time = all_candles[2].open_time
    return adapter, config, missing_open_time


def test_gap_repair_finds_data_and_marks_repaired(db, now):
    adapter, config, missing_open_time = _setup_gap(db, now)
    adapter._candles.append(make_candle(missing_open_time, price=999))
    adapter._candles.sort(key=lambda c: c.open_time)

    summary = GapRepairService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    assert summary.repaired == 1
    assert summary.still_missing == 0
    gap_status = (
        db.execute(text("SELECT status, resolved_at FROM ingestion_gap")).mappings().first()
    )
    assert gap_status["status"] == "repaired"
    assert gap_status["resolved_at"] is not None


def test_gap_repair_still_missing_schedules_next_attempt(db, now):
    adapter, config, _ = _setup_gap(db, now)

    summary = GapRepairService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    assert summary.still_missing == 1
    gap = (
        db.execute(text("SELECT status, attempts, next_attempt_after FROM ingestion_gap"))
        .mappings()
        .first()
    )
    assert gap["status"] == "pending"
    assert gap["attempts"] == 1
    assert gap["next_attempt_after"] == now + timedelta(hours=24)


def test_gap_repair_respects_24h_spacing(db, now):
    adapter, config, _ = _setup_gap(db, now)
    service = GapRepairService(db, adapter, config, retry_policy=_policy())

    service.run("fake", "BTC/USDT", "1h", now=now)
    too_soon = now + timedelta(hours=1)
    summary = service.run("fake", "BTC/USDT", "1h", now=too_soon)

    assert summary.still_missing == 0  # gap not due yet, so it wasn't processed
    assert summary.repaired == 0
    assert summary.confirmed_absent == 0


def test_gap_repair_confirmed_absent_after_max_attempts(db, now):
    adapter, config, _ = _setup_gap(db, now)
    service = GapRepairService(db, adapter, config, retry_policy=_policy())

    t = now
    for _ in range(3):
        service.run("fake", "BTC/USDT", "1h", now=t)
        t = t + timedelta(hours=24, seconds=1)

    gap = db.execute(text("SELECT status, attempts FROM ingestion_gap")).mappings().first()
    assert gap["status"] == "confirmed_absent"
    assert gap["attempts"] == 3

    # a later scan must not resurrect a confirmed_absent gap
    GapDetectionService(db).run("fake", "BTC/USDT", "1h", now=t)
    rows = db.execute(text("SELECT status FROM ingestion_gap")).mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "confirmed_absent"
