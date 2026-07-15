from datetime import timedelta

from sqlalchemy import text

from core.ingestion.config import IngestionConfig
from core.ingestion.scheduler import Scheduler
from core.ingestion.testing import FakeExchangeAdapter
from core.ingestion.watermark import get_watermark
from tests.ingestion.conftest import hourly_candles


class _FakeExternalTradeDetectionService:
    """Duck-types ExternalTradeDetectionService's is_due()/run_once()
    shape — Scheduler only ever calls those two methods, so a full
    core.execution.external_trade_detection_service.ExternalTradeDetectionService
    (with its own real DB/order_lister dependencies) isn't needed here,
    matching how reconciliation_job's Scheduler wiring is exercised."""

    def __init__(self, results):
        self._results = results
        self.run_calls = 0

    def is_due(self, now=None):
        return True

    def run_once(self, now=None):
        self.run_calls += 1
        return self._results


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


def test_scheduler_runs_external_trade_detection_when_due(db, now):
    from dataclasses import dataclass

    @dataclass
    class _Result:
        newly_recorded: bool

    service = _FakeExternalTradeDetectionService([_Result(True), _Result(False)])

    summary = Scheduler(
        db, {}, IngestionConfig(), external_trade_detection_service=service
    ).run_once(now=now)

    assert service.run_calls == 1
    assert summary.external_trades_detected == 1  # only the newly-recorded one counted


def test_scheduler_skips_external_trade_detection_without_the_optional_param(db, now):
    """Backward compatibility: every existing caller/test that doesn't
    pass external_trade_detection_service is completely unaffected."""
    summary = Scheduler(db, {}, IngestionConfig()).run_once(now=now)

    assert summary.external_trades_detected == 0


class _FakeSignalScanner:
    """Duck-types SignalScanner's is_due()/run_once() shape — same
    reasoning as _FakeExternalTradeDetectionService above."""

    def __init__(self, results):
        self._results = results
        self.run_calls = 0

    def is_due(self, now=None):
        return True

    def run_once(self, now=None):
        self.run_calls += 1
        return self._results


def test_scheduler_runs_signal_scanner_when_due(db, now):
    from dataclasses import dataclass

    @dataclass
    class _Result:
        newly_recorded: bool

    scanner = _FakeSignalScanner([_Result(True), _Result(True), _Result(False)])

    summary = Scheduler(db, {}, IngestionConfig(), signal_scanner=scanner).run_once(now=now)

    assert scanner.run_calls == 1
    assert summary.trade_signals_generated == 2  # only the newly-recorded ones counted


def test_scheduler_skips_signal_scanner_without_the_optional_param(db, now):
    """Backward compatibility: every existing caller/test that doesn't
    pass signal_scanner is completely unaffected."""
    summary = Scheduler(db, {}, IngestionConfig()).run_once(now=now)

    assert summary.trade_signals_generated == 0
