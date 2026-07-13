from datetime import timedelta

from sqlalchemy import text

from core.ingestion.config import IngestionConfig
from core.ingestion.scheduler import Scheduler
from core.ingestion.testing import FakeExchangeAdapter
from core.ingestion.watermark import get_watermark
from tests.ingestion.conftest import hourly_candles


def _track(db, exchange, symbol, timeframe):
    db.execute(
        text("""
            INSERT INTO tracked_instruments (exchange, symbol, timeframe, active)
            VALUES (:exchange, :symbol, :timeframe, TRUE)
            """),
        {"exchange": exchange, "symbol": symbol, "timeframe": timeframe},
    )
    db.commit()


def test_scheduler_backfills_new_tracked_instrument(db, now):
    start = now - timedelta(hours=5)
    candles = hourly_candles(start, 5)
    adapter = FakeExchangeAdapter(candles=candles, earliest=start)
    _track(db, "fake", "BTC/USDT", "1h")

    summary = Scheduler(db, {"fake": adapter}, IngestionConfig()).run_once(now=now)

    assert "fake:BTC/USDT:1h" in summary.backfills_run
    watermark = get_watermark(db, "fake", "BTC/USDT", "1h")
    assert watermark.backfill_complete is True


def test_scheduler_skips_untracked_exchange_gracefully(db, now):
    _track(db, "unknown_exchange", "BTC/USDT", "1h")

    summary = Scheduler(db, {"fake": FakeExchangeAdapter(candles=[])}, IngestionConfig()).run_once(
        now=now
    )

    assert summary.backfills_run == []


def test_scheduler_second_sweep_runs_incremental_not_backfill_again(db, now):
    start = now - timedelta(hours=5)
    candles = hourly_candles(start, 5)
    adapter = FakeExchangeAdapter(candles=candles, earliest=start)
    _track(db, "fake", "BTC/USDT", "1h")
    config = IngestionConfig()

    scheduler = Scheduler(db, {"fake": adapter}, config)
    scheduler.run_once(now=now)

    later = now + timedelta(hours=2)
    summary = scheduler.run_once(now=later)

    assert summary.backfills_run == []
    assert "fake:BTC/USDT:1h" in summary.incrementals_run
