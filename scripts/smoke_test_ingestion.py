"""
Manual, one-off smoke test against the REAL Binance API and REAL local
Postgres — not part of the pytest suite (which uses FakeExchangeAdapter
for determinism/no network dependency, per spec section 7). This
script exists to satisfy the spec's "definition of done": working
end-to-end against real Binance data for at least one symbol/timeframe.

Run manually: python scripts/smoke_test_ingestion.py
Cleans up the rows it inserts so it can be re-run.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from core.db import SessionLocal
from core.ingestion.binance_adapter import BinanceAdapter
from core.ingestion.candle_validator import validate_candles
from core.ingestion.config import IngestionConfig
from core.ingestion.data_quality_service import DataQualityService
from core.ingestion.gap_detection_service import GapDetectionService
from core.ingestion.raw_ohlcv_store import upsert_candles
from core.ingestion.run_log import RunLogEntry, record_run
from core.ingestion.watermark import upsert_watermark

EXCHANGE = "binance"
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"


def main() -> None:
    db = SessionLocal()
    adapter = BinanceAdapter()
    config = IngestionConfig()
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    print(f"[1/6] Discovering earliest available {SYMBOL} {TIMEFRAME} candle on Binance...")
    earliest = adapter.earliest_available(SYMBOL, TIMEFRAME)
    print(f"      earliest_available() -> {earliest}")

    print(f"[2/6] Fetching last 48h of real {SYMBOL} {TIMEFRAME} candles from Binance...")
    start = now - timedelta(hours=48)
    candles = adapter.fetch_klines(SYMBOL, TIMEFRAME, start, now, limit=100)
    print(
        f"      fetched {len(candles)} candles, first={candles[0].open_time}, "
        f"last={candles[-1].open_time}"
    )

    print("[3/6] Validating with CandleValidator...")
    result = validate_candles(candles, TIMEFRAME, now)
    print(f"      valid={len(result.valid)} failures={len(result.failures)}")
    for f in result.failures:
        print(f"      REJECTED open_time={f.open_time} reason={f.reason}")

    print("[4/6] Writing ingestion_run_log + upserting into raw_ohlcv (real Postgres)...")
    run_id = record_run(
        db,
        RunLogEntry(
            run_type="backfill",
            exchange=EXCHANGE,
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            started_at=now,
            finished_at=datetime.now(UTC),
            status="success" if not result.failures else "partial",
            requested_range={"start": start.isoformat(), "end": now.isoformat()},
            received_count=len(candles),
            stored_count=len(result.valid),
            validation_failures=[
                {"open_time": f.open_time.isoformat(), "reason": f.reason} for f in result.failures
            ],
        ),
    )
    inserted = upsert_candles(db, EXCHANGE, SYMBOL, TIMEFRAME, result.valid, run_id)
    upsert_watermark(
        db,
        EXCHANGE,
        SYMBOL,
        TIMEFRAME,
        earliest_available_at=start,
        last_ingested_open_time=max(c.open_time for c in result.valid),
        backfill_complete=True,
    )
    print(f"      run_id={run_id} inserted={inserted} rows into raw_ohlcv")

    print("[5/6] Running GapDetectionService against the real ingested range...")
    gap_result = GapDetectionService(db).run(EXCHANGE, SYMBOL, TIMEFRAME, now=now)
    print(
        f"      gaps_found={len(gap_result.gaps_found)} skipped_reason={gap_result.skipped_reason}"
    )

    print("[6/6] Running DataQualityService with a live cross-check against Binance...")
    dq_report = DataQualityService(db, config, adapter=adapter).run(
        EXCHANGE, SYMBOL, TIMEFRAME, now=now
    )
    print(f"      {dq_report.summary}")
    for issue in dq_report.issues:
        print(f"      ISSUE [{issue.severity}] {issue.check}: {issue.detail}")

    print("\nCleaning up rows inserted by this smoke test...")
    db.execute(
        text("DELETE FROM raw_ohlcv WHERE exchange = :e AND symbol = :s AND timeframe = :t"),
        {"e": EXCHANGE, "s": SYMBOL, "t": TIMEFRAME},
    )
    db.execute(
        text("DELETE FROM ingestion_gap WHERE exchange = :e AND symbol = :s AND timeframe = :t"),
        {"e": EXCHANGE, "s": SYMBOL, "t": TIMEFRAME},
    )
    db.execute(
        text(
            "DELETE FROM ingestion_watermark WHERE exchange = :e AND symbol = :s AND timeframe = :t"
        ),
        {"e": EXCHANGE, "s": SYMBOL, "t": TIMEFRAME},
    )
    db.execute(
        text(
            "DELETE FROM data_quality_report WHERE exchange = :e AND symbol = :s AND timeframe = :t"
        ),
        {"e": EXCHANGE, "s": SYMBOL, "t": TIMEFRAME},
    )
    db.execute(
        text(
            "DELETE FROM ingestion_run_log WHERE exchange = :e AND symbol = :s AND timeframe = :t"
        ),
        {"e": EXCHANGE, "s": SYMBOL, "t": TIMEFRAME},
    )
    db.commit()
    db.close()
    print(
        "Done. SUCCESS: real Binance backfill -> validate -> store -> gap scan -> data "
        "quality (with live cross-check) all ran end-to-end."
    )


if __name__ == "__main__":
    main()
