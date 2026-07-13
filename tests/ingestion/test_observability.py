from datetime import UTC, datetime

from sqlalchemy import text

from core.ingestion.observability import check_health


def _track(db, exchange, symbol, timeframe):
    db.execute(
        text("""
            INSERT INTO tracked_instruments (exchange, symbol, timeframe, active)
            VALUES (:e, :s, :t, TRUE)
            """),
        {"e": exchange, "s": symbol, "t": timeframe},
    )
    db.commit()


def _log_run(db, exchange, symbol, timeframe, started_at):
    db.execute(
        text("""
            INSERT INTO ingestion_run_log
                (run_type, exchange, symbol, timeframe, started_at, finished_at, status)
            VALUES ('incremental', :e, :s, :t, :started_at, :started_at, 'success')
            """),
        {"e": exchange, "s": symbol, "t": timeframe, "started_at": started_at},
    )
    db.commit()


def test_health_ok_with_no_tracked_instruments(db):
    health = check_health(db)
    assert health["status"] == "ok"
    assert health["checks"]["database"] == "ok"
    assert health["checks"]["tracked_instruments"] == 0


def test_health_degraded_when_instrument_has_no_recent_run(db):
    _track(db, "fake", "BTC/USDT", "1h")
    health = check_health(db, staleness_seconds=3600)
    assert health["status"] == "degraded"
    assert "fake:BTC/USDT:1h" in health["checks"]["stale_instruments"]


def test_health_ok_when_run_is_recent(db):
    _track(db, "fake", "BTC/USDT", "1h")
    _log_run(db, "fake", "BTC/USDT", "1h", datetime.now(UTC))
    health = check_health(db, staleness_seconds=3600)
    assert health["status"] == "ok"
