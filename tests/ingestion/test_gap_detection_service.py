from datetime import UTC, datetime

from sqlalchemy import text

from core.ingestion.backfill_service import BackfillService
from core.ingestion.config import IngestionConfig
from core.ingestion.gap_detection_service import GapDetectionService
from core.ingestion.retry_policy import RetryPolicy
from core.ingestion.testing import FakeExchangeAdapter
from core.ingestion.watermark import get_watermark
from tests.ingestion.conftest import hourly_candles


def _policy() -> RetryPolicy:
    return RetryPolicy(sleep=lambda s: None, rand=lambda: 0.0)


def test_gap_detection_skips_without_watermark(db, now):
    result = GapDetectionService(db).run("fake", "BTC/USDT", "1h", now=now)
    assert result.gaps_found == []
    assert result.skipped_reason is not None


def test_gap_detection_finds_missing_range(db, now):
    start = datetime(2024, 5, 1, tzinfo=UTC)
    all_candles = hourly_candles(start, 10)
    # deliberately remove hours 3,4,5 to create one gap
    incomplete = [c for i, c in enumerate(all_candles) if i not in (3, 4, 5)]
    adapter = FakeExchangeAdapter(candles=incomplete, earliest=start)
    config = IngestionConfig()
    BackfillService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    result = GapDetectionService(db).run("fake", "BTC/USDT", "1h", now=now)

    assert len(result.gaps_found) == 1
    assert result.gaps_found[0].gap_start == all_candles[3].open_time
    assert result.gaps_found[0].gap_end == all_candles[5].open_time

    gap_row = db.execute(text("SELECT status FROM ingestion_gap")).mappings().first()
    assert gap_row["status"] == "pending"


def test_gap_detection_updates_watermark_scan_time(db, now):
    start = datetime(2024, 5, 1, tzinfo=UTC)
    candles = hourly_candles(start, 5)
    adapter = FakeExchangeAdapter(candles=candles, earliest=start)
    config = IngestionConfig()
    BackfillService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    GapDetectionService(db).run("fake", "BTC/USDT", "1h", now=now)

    watermark = get_watermark(db, "fake", "BTC/USDT", "1h")
    assert watermark.last_gap_scan_at == now


def test_gap_detection_does_not_reflag_confirmed_absent_gap(db, now):
    start = datetime(2024, 5, 1, tzinfo=UTC)
    all_candles = hourly_candles(start, 5)
    incomplete = [c for i, c in enumerate(all_candles) if i != 2]
    adapter = FakeExchangeAdapter(candles=incomplete, earliest=start)
    config = IngestionConfig()
    BackfillService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    GapDetectionService(db).run("fake", "BTC/USDT", "1h", now=now)
    db.execute(text("UPDATE ingestion_gap SET status = 'confirmed_absent'"))
    db.commit()

    GapDetectionService(db).run("fake", "BTC/USDT", "1h", now=now)

    rows = db.execute(text("SELECT status FROM ingestion_gap")).mappings().all()
    assert len(rows) == 1
    assert rows[0]["status"] == "confirmed_absent"
