from datetime import timedelta

from sqlalchemy import text

from core.ingestion.config import IngestionConfig
from core.ingestion.data_quality_service import DataQualityReport, DataQualityService, Issue
from core.ingestion.testing import FakeExchangeAdapter
from tests.ingestion.conftest import hourly_candles, make_candle


def _insert_raw(db, exchange, symbol, timeframe, candle):
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES (:exchange, :symbol, :timeframe, :open_time, :open, :high, :low, :close, :volume)
            ON CONFLICT DO NOTHING
            """),
        {
            "exchange": exchange,
            "symbol": symbol,
            "timeframe": timeframe,
            "open_time": candle.open_time,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        },
    )
    db.commit()


def test_clean_data_produces_no_issues(db, now):
    start = now - timedelta(hours=20)
    for candle in hourly_candles(start, 15):
        _insert_raw(db, "fake", "BTC/USDT", "1h", candle)

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert report.candles_checked == 15
    assert report.issues == []


def test_duplicate_candle_check_runs_and_finds_none(db, now):
    # Duplicates are structurally impossible given the PK on
    # (exchange, symbol, timeframe, open_time) — this just confirms the
    # check actually runs and correctly reports zero for clean data.
    start = now - timedelta(hours=5)
    for candle in hourly_candles(start, 3):
        _insert_raw(db, "fake", "BTC/USDT", "1h", candle)

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert "duplicate_candles" in report.checks_run
    assert not any(i.check == "duplicate_candles" for i in report.issues)


def test_invalid_ohlc_detected(db, now):
    start = now - timedelta(hours=5)
    bad = make_candle(start)
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES ('fake', 'BTC/USDT', '1h', :open_time, 100, 90, 95, 98, 10)
            """),
        {"open_time": bad.open_time},
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert any(i.check == "invalid_ohlc" for i in report.issues)


def test_timestamp_misalignment_detected(db, now):
    misaligned_time = (now - timedelta(hours=5)).replace(minute=17)
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES ('fake', 'BTC/USDT', '1h', :open_time, 100, 101, 99, 100, 10)
            """),
        {"open_time": misaligned_time},
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert any(i.check == "timestamp_alignment" for i in report.issues)


def test_volume_anomaly_detected(db, now):
    start = now - timedelta(hours=20)
    normal = hourly_candles(start, 19, price=100)
    for c in normal:
        _insert_raw(db, "fake", "BTC/USDT", "1h", c)
    # one wildly anomalous volume candle
    spike = make_candle(start + timedelta(hours=19), price=100, volume=100_000)
    _insert_raw(db, "fake", "BTC/USDT", "1h", spike)

    report = DataQualityService(db, IngestionConfig(volume_anomaly_zscore_threshold=3.0)).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    assert any(i.check == "volume_anomalies" for i in report.issues)


def test_cross_check_against_exchange_detects_drift(db, now):
    start = now - timedelta(hours=3)
    candle = make_candle(start, price=100)
    _insert_raw(db, "fake", "BTC/USDT", "1h", candle)

    # exchange "actually" has a different close than what's stored
    drifted = make_candle(start, price=100)
    from dataclasses import replace

    drifted = replace(drifted, close=candle.close + 50)
    adapter = FakeExchangeAdapter(candles=[drifted])

    report = DataQualityService(db, IngestionConfig(), adapter=adapter).run(
        "fake", "BTC/USDT", "1h", now=now
    )

    assert report.cross_check_diffs >= 1
    assert any(i.check == "cross_check_exchange" for i in report.issues)


# --- missing_candles cross-reference (docs/gap_audit_report.md P1) ---------


def test_missing_candles_reports_pending_gap_in_window(db, now):
    start = now - timedelta(hours=5)
    for c in hourly_candles(start, 5):
        _insert_raw(db, "fake", "BTC/USDT", "1h", c)
    gap_start = start + timedelta(hours=2)
    gap_end = start + timedelta(hours=3)
    db.execute(
        text("""
            INSERT INTO ingestion_gap
                (exchange, symbol, timeframe, gap_start, gap_end, status)
            VALUES ('fake', 'BTC/USDT', '1h', :gap_start, :gap_end, 'pending')
            """),
        {"gap_start": gap_start, "gap_end": gap_end},
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert "missing_candles" in report.checks_run
    assert any(i.check == "missing_candles" for i in report.issues)


def test_missing_candles_ignores_confirmed_absent_gap(db, now):
    start = now - timedelta(hours=5)
    for c in hourly_candles(start, 5):
        _insert_raw(db, "fake", "BTC/USDT", "1h", c)
    db.execute(
        text("""
            INSERT INTO ingestion_gap
                (exchange, symbol, timeframe, gap_start, gap_end, status)
            VALUES ('fake', 'BTC/USDT', '1h', :gap_start, :gap_end, 'confirmed_absent')
            """),
        {"gap_start": start + timedelta(hours=2), "gap_end": start + timedelta(hours=3)},
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert not any(i.check == "missing_candles" for i in report.issues)


def test_missing_candles_none_when_no_gaps_recorded(db, now):
    start = now - timedelta(hours=5)
    for c in hourly_candles(start, 5):
        _insert_raw(db, "fake", "BTC/USDT", "1h", c)

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert "missing_candles" in report.checks_run
    assert not any(i.check == "missing_candles" for i in report.issues)


# --- timeframe_consistency reconciliation (docs/gap_audit_report.md P1) ----


def test_timeframe_consistency_passes_when_sub_candles_reconcile(db, now):
    hour_start = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    minute_candles = [
        make_candle(hour_start + timedelta(minutes=i), price=100.0 + i, volume=1.0)
        for i in range(60)
    ]
    for c in minute_candles:
        _insert_raw(db, "fake", "BTC/USDT", "1m", c)

    agg_open = minute_candles[0].open
    agg_close = minute_candles[-1].close
    agg_high = max(c.high for c in minute_candles)
    agg_low = min(c.low for c in minute_candles)
    agg_volume = sum(c.volume for c in minute_candles)
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES ('fake', 'BTC/USDT', '1h', :open_time, :open, :high, :low, :close, :volume)
            """),
        {
            "open_time": hour_start,
            "open": agg_open,
            "high": agg_high,
            "low": agg_low,
            "close": agg_close,
            "volume": agg_volume,
        },
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert "timeframe_consistency" in report.checks_run
    assert not any(i.check == "timeframe_consistency" for i in report.issues)


def test_timeframe_consistency_flags_a_real_mismatch(db, now):
    hour_start = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    minute_candles = [
        make_candle(hour_start + timedelta(minutes=i), price=100.0 + i, volume=1.0)
        for i in range(60)
    ]
    for c in minute_candles:
        _insert_raw(db, "fake", "BTC/USDT", "1m", c)

    # Stored 1h close deliberately disagrees with the last 1m candle's close.
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES ('fake', 'BTC/USDT', '1h', :open_time, 100, 200, 90, 999, 60)
            """),
        {"open_time": hour_start},
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert any(i.check == "timeframe_consistency" for i in report.issues)


def test_timeframe_consistency_skips_incomplete_sub_candle_coverage(db, now):
    hour_start = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    # Only 3 of the 60 expected 1m candles exist — an incomplete set,
    # not a mismatch, so it must be skipped rather than flagged.
    for i in range(3):
        _insert_raw(db, "fake", "BTC/USDT", "1m", make_candle(hour_start + timedelta(minutes=i)))
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES ('fake', 'BTC/USDT', '1h', :open_time, 100, 101, 99, 100, 10)
            """),
        {"open_time": hour_start},
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert not any(i.check == "timeframe_consistency" for i in report.issues)


def test_timeframe_consistency_is_a_noop_for_the_smallest_timeframe(db, now):
    start = now - timedelta(minutes=5)
    for c in [make_candle(start + timedelta(minutes=i), interval_seconds=60) for i in range(5)]:
        _insert_raw(db, "fake", "BTC/USDT", "1m", c)

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1m", now=now)

    assert "timeframe_consistency" in report.checks_run
    assert not any(i.check == "timeframe_consistency" for i in report.issues)


def test_report_is_persisted(db, now):
    start = now - timedelta(hours=2)
    for c in hourly_candles(start, 2):
        _insert_raw(db, "fake", "BTC/USDT", "1h", c)

    DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    row = db.execute(text("SELECT * FROM data_quality_report")).mappings().first()
    assert row is not None
    assert row["candles_checked"] == 2


# --- DataQualityReport.passed() (docs/gap_audit_report.md P0 #2) ------------


def test_passed_is_true_with_no_issues():
    report = DataQualityReport(checks_run=["duplicate_candles"], issues=[])
    assert report.passed() is True


def test_passed_is_true_when_only_info_issues_exist_at_default_warning_threshold():
    report = DataQualityReport(
        checks_run=["duplicate_candles"],
        issues=[Issue(check="duplicate_candles", severity="info", detail="cosmetic")],
    )
    assert report.passed() is True


def test_passed_is_false_when_a_warning_issue_exists_at_default_threshold():
    report = DataQualityReport(
        checks_run=["ohlc_validity"],
        issues=[Issue(check="ohlc_validity", severity="warning", detail="high < low")],
    )
    assert report.passed() is False


def test_passed_is_false_when_a_critical_issue_exists():
    report = DataQualityReport(
        checks_run=["volume_anomaly"],
        issues=[Issue(check="volume_anomaly", severity="critical", detail="huge spike")],
    )
    assert report.passed() is False


def test_passed_respects_a_looser_explicit_threshold():
    report = DataQualityReport(
        checks_run=["ohlc_validity"],
        issues=[Issue(check="ohlc_validity", severity="warning", detail="high < low")],
    )
    # Explicitly asking only for 'critical' issues to fail the gate —
    # a warning-level issue no longer trips it.
    assert report.passed(threshold="critical") is True


def test_passed_end_to_end_against_a_real_invalid_ohlc_report(db, now):
    """Real DataQualityService.run() output, not a hand-built report —
    proves passed() works against genuine severity values the service
    actually assigns, not just the values a test author guesses."""
    start = now - timedelta(hours=5)
    bad = make_candle(start)
    db.execute(
        text("""
            INSERT INTO raw_ohlcv
                (exchange, symbol, timeframe, open_time, open, high, low, close, volume)
            VALUES ('fake', 'BTC/USDT', '1h', :open_time, 100, 90, 95, 98, 10)
            """),
        {"open_time": bad.open_time},
    )
    db.commit()

    report = DataQualityService(db, IngestionConfig()).run("fake", "BTC/USDT", "1h", now=now)

    assert report.issues  # sanity: the bad candle was actually caught
    assert report.passed() is False
