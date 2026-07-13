from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from core.ingestion.backfill_service import BackfillService
from core.ingestion.config import IngestionConfig
from core.ingestion.incremental_update_service import IncrementalUpdateService
from core.ingestion.retry_policy import RetryPolicy
from core.ingestion.testing import FakeExchangeAdapter
from core.ingestion.watermark import get_watermark
from tests.ingestion.conftest import hourly_candles


def _policy() -> RetryPolicy:
    return RetryPolicy(sleep=lambda s: None, rand=lambda: 0.0)


def test_incremental_skips_without_prior_backfill(db, now):
    adapter = FakeExchangeAdapter(candles=[])
    config = IngestionConfig()
    service = IncrementalUpdateService(db, adapter, config, retry_policy=_policy())

    result = service.run("fake", "BTC/USDT", "1h", now=now)

    assert result.stored_count == 0
    assert "backfill" in result.skipped_reason


def test_incremental_fetches_only_new_candles_after_watermark(db, now):
    start = datetime(2024, 5, 1, tzinfo=UTC)
    initial = hourly_candles(start, 5)  # ends at start+4h
    adapter = FakeExchangeAdapter(candles=initial, earliest=start)
    config = IngestionConfig()
    BackfillService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    new_candles = hourly_candles(start + timedelta(hours=5), 3)
    adapter._candles = sorted(initial + new_candles, key=lambda c: c.open_time)

    result = IncrementalUpdateService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    assert result.stored_count == 3
    stored = db.execute(text("SELECT count(*) FROM raw_ohlcv")).scalar_one()
    assert stored == 8

    watermark = get_watermark(db, "fake", "BTC/USDT", "1h")
    assert watermark.last_ingested_open_time == new_candles[-1].open_time


def test_incremental_no_new_candles_logs_skip_not_silence(db, now):
    start = datetime(2024, 5, 1, tzinfo=UTC)
    candles = hourly_candles(start, 3)
    adapter = FakeExchangeAdapter(candles=candles, earliest=start)
    config = IngestionConfig()
    # Backfill with `now` one bar past the last candle so the watermark
    # lands exactly on the last candle's open_time; then re-run
    # incremental with `now` AT that same open_time, so
    # start_time (watermark + 1 bar) is strictly after `now` — the
    # early-exit "nothing new yet" branch, not an empty fetch result
    # for an unrelated reason (e.g. the fixed `now` fixture being a
    # month away from these candles).
    last_candle_time = candles[-1].open_time
    BackfillService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=last_candle_time + timedelta(hours=1)
    )

    result = IncrementalUpdateService(db, adapter, config, retry_policy=_policy()).run(
        "fake", "BTC/USDT", "1h", now=last_candle_time
    )

    assert result.stored_count == 0
    assert result.skipped_reason == "no new candles since watermark"
    run_log_count = db.execute(
        text("SELECT count(*) FROM ingestion_run_log WHERE run_type = 'incremental'")
    ).scalar_one()
    assert run_log_count == 1  # the skip itself was logged, not dropped
