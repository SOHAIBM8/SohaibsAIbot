from datetime import UTC, datetime

from sqlalchemy import text

from core.ingestion.backfill_service import BackfillService
from core.ingestion.config import IngestionConfig
from core.ingestion.retry_policy import RetryPolicy
from core.ingestion.testing import FakeExchangeAdapter
from core.ingestion.watermark import get_watermark
from tests.ingestion.conftest import hourly_candles


def test_backfill_stores_all_candles_and_completes_watermark(db, now):
    start = datetime(2024, 5, 1, tzinfo=UTC)
    candles = hourly_candles(start, 10)
    adapter = FakeExchangeAdapter(candles=candles, earliest=start)
    config = IngestionConfig(per_request_candle_limit=1000)
    service = BackfillService(db, adapter, config, retry_policy=_no_sleep_retry_policy())

    result = service.run("fake", "BTC/USDT", "1h", now=now)

    assert result.stored_count == 10
    assert result.received_count == 10
    assert result.skipped is False

    stored = db.execute(text("SELECT count(*) FROM raw_ohlcv")).scalar_one()
    assert stored == 10

    watermark = get_watermark(db, "fake", "BTC/USDT", "1h")
    assert watermark.backfill_complete is True
    assert watermark.last_ingested_open_time == candles[-1].open_time


def test_backfill_paginates_across_multiple_requests(db, now):
    start = datetime(2024, 5, 1, tzinfo=UTC)
    candles = hourly_candles(start, 25)
    adapter = FakeExchangeAdapter(candles=candles, earliest=start)
    config = IngestionConfig(per_request_candle_limit=10)
    service = BackfillService(db, adapter, config, retry_policy=_no_sleep_retry_policy())

    result = service.run("fake", "BTC/USDT", "1h", now=now)

    assert result.stored_count == 25
    assert len(adapter.calls) >= 3  # 25 candles / 10-per-request limit


def test_backfill_is_idempotent_on_rerun(db, now):
    start = datetime(2024, 5, 1, tzinfo=UTC)
    candles = hourly_candles(start, 10)
    adapter = FakeExchangeAdapter(candles=candles, earliest=start)
    config = IngestionConfig(per_request_candle_limit=1000)
    service = BackfillService(db, adapter, config, retry_policy=_no_sleep_retry_policy())

    first = service.run("fake", "BTC/USDT", "1h", now=now)
    second = service.run("fake", "BTC/USDT", "1h", now=now)

    assert first.stored_count == 10
    assert second.skipped is True
    assert second.stored_count == 0

    stored = db.execute(text("SELECT count(*) FROM raw_ohlcv")).scalar_one()
    assert stored == 10  # no duplicates


def test_backfill_falls_back_to_default_window_when_earliest_unknown(db, now):
    adapter = FakeExchangeAdapter(candles=[], earliest=None)
    config = IngestionConfig(default_backfill_years=1)
    service = BackfillService(db, adapter, config, retry_policy=_no_sleep_retry_policy())

    result = service.run("fake", "BTC/USDT", "1h", now=now)

    assert result.stored_count == 0
    assert result.skipped is False
    # fetch_klines was still called with a start_time ~1 year before `now`
    assert adapter.calls
    _, _, called_start, _ = adapter.calls[0]
    assert (now - called_start).days >= 364


def test_backfill_records_validation_failures(db, now):
    from core.ingestion.types import RawCandle

    start = datetime(2024, 5, 1, tzinfo=UTC)
    bad_candle = RawCandle(
        open_time=start,
        open=100,
        high=90,
        low=95,
        close=98,  # high < low, invalid
        volume=10,
        close_time=start,
    )
    adapter = FakeExchangeAdapter(candles=[bad_candle], earliest=start)
    config = IngestionConfig(per_request_candle_limit=1000)
    service = BackfillService(db, adapter, config, retry_policy=_no_sleep_retry_policy())

    result = service.run("fake", "BTC/USDT", "1h", now=now)

    assert result.stored_count == 0
    run_log = (
        db.execute(text("SELECT status, validation_failures FROM ingestion_run_log"))
        .mappings()
        .first()
    )
    assert run_log["status"] == "partial"
    assert len(run_log["validation_failures"]) == 1


def _no_sleep_retry_policy() -> RetryPolicy:
    return RetryPolicy(sleep=lambda s: None, rand=lambda: 0.0)
