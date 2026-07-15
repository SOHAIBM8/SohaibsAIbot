"""
DataQualityService (spec 4.11). Distinct from GapDetectionService:
that asks "is anything missing", this asks "is what's stored actually
correct" — duplicates, OHLC violations, timestamp drift, volume
anomalies, missing-candle cross-reference, cross-timeframe
reconciliation, and (given an adapter) drift against the exchange
itself. Each check is independent and reported separately so a single
bad check never masks the others.

Missing-candles check (docs/gap_audit_report.md P1): spec 4.11 item 1
asks this service to "cross-reference with GapDetectionService's
findings" rather than re-run gap detection itself — this check reads
the already-persisted `ingestion_gap` table (status='pending', the
only non-terminal state — 'confirmed_absent' is a deliberate
give-up per GapRepairService and must not be re-flagged here either)
scoped to this window, rather than duplicating GapDetectionService's
generate_series logic a second time.

Timeframe-consistency check (docs/gap_audit_report.md P1): spec 4.11
item 6's own example is literal — "summing 1m candles for an hour
should reconcile with the stored 1h candle" — so this reconciles every
non-1m timeframe directly against 1m candles (the base timeframe),
not against the next-adjacent-smaller timeframe (e.g. 15m for 1h):
1m is the one timeframe this platform ingests unconditionally for
every tracked instrument, whereas an intermediate timeframe like 15m
may not be tracked at all for a given symbol, which would silently
skip the check rather than actually reconcile anything. For each
checked candle, this pulls the exact set of 1m candles inside its
window; if that set isn't *complete* (some 1m candles are simply
missing), the check is skipped for that candle — that's a
completeness gap already surfaced by the missing-candles check /
GapDetectionService, not a consistency issue. Otherwise the two are
compared with a small tolerance (float rounding from exchange APIs,
not a config knob — rule 8, no premature configurability for a
tolerance nothing has ever needed to tune).
"""

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ingestion.candle_validator import validate_candles
from core.ingestion.config import IngestionConfig
from core.ingestion.event_bus import EventBus
from core.ingestion.events import DataQualityIssueFound
from core.ingestion.exchange_adapter import ExchangeAdapter
from core.ingestion.timeframe import is_aligned, timeframe_to_timedelta
from core.ingestion.types import RawCandle
from core.ingestion.watermark import upsert_watermark

logger = structlog.get_logger(__name__)

_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Issue:
    check: str
    severity: str  # 'info' | 'warning' | 'critical'
    detail: str


@dataclass
class DataQualityReport:
    checks_run: list[str]
    issues: list[Issue] = field(default_factory=list)
    candles_checked: int = 0
    cross_check_diffs: int = 0

    @property
    def summary(self) -> str:
        if not self.issues:
            return f"{self.candles_checked} candles checked, no issues found."
        by_severity: dict[str, int] = {}
        for issue in self.issues:
            by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1
        counts = ", ".join(f"{n} {sev}" for sev, n in sorted(by_severity.items()))
        return (
            f"{self.candles_checked} candles checked, {len(self.issues)} issues found ({counts})."
        )

    def passed(self, threshold: str = "warning") -> bool:
        """True if no issue's severity is at or above `threshold`.
        Added to fix docs/gap_audit_report.md P0 #2: run() itself only
        ever used `_SEVERITY_ORDER` internally to decide whether to
        publish a `DataQualityIssueFound` event — it never returned a
        pass/fail answer, so nothing outside this module could ask
        'is this data actually usable.' Reuses the exact same ordering
        `run()` already applies for alerting, not a second judgment."""
        threshold_rank = _SEVERITY_ORDER[threshold]
        return all(_SEVERITY_ORDER[issue.severity] < threshold_rank for issue in self.issues)


class DataQualityService:
    def __init__(
        self,
        db: Session,
        config: IngestionConfig,
        adapter: ExchangeAdapter | None = None,
        event_bus: EventBus | None = None,
        lookback_days: int = 7,
    ):
        self.db = db
        self.config = config
        self.adapter = adapter
        self.event_bus = event_bus
        self.lookback_days = lookback_days

    def run(
        self, exchange: str, symbol: str, timeframe: str, now: datetime | None = None
    ) -> DataQualityReport:
        now = now or datetime.now(UTC)
        window_start = now - timedelta(days=self.lookback_days)

        rows = self._fetch_rows(exchange, symbol, timeframe, window_start, now)

        report = DataQualityReport(checks_run=[])
        report.candles_checked = len(rows)

        self._check_duplicates(exchange, symbol, timeframe, window_start, now, report)
        self._check_ohlc_validity(rows, timeframe, now, report)
        self._check_timestamp_alignment(rows, timeframe, report)
        self._check_volume_anomalies(rows, report)
        self._check_missing_candles(exchange, symbol, timeframe, window_start, now, report)
        self._check_timeframe_consistency(exchange, symbol, timeframe, rows, report)
        if self.adapter is not None:
            self._check_cross_exchange(exchange, symbol, timeframe, rows, now, report)

        self._store_report(exchange, symbol, timeframe, now, report)
        upsert_watermark(self.db, exchange, symbol, timeframe, last_data_quality_check_at=now)

        threshold = _SEVERITY_ORDER[self.config.data_quality_issue_severity_threshold]
        if self.event_bus is not None:
            for issue in report.issues:
                if _SEVERITY_ORDER[issue.severity] >= threshold:
                    self.event_bus.publish(
                        DataQualityIssueFound(
                            exchange=exchange,
                            symbol=symbol,
                            timeframe=timeframe,
                            severity=issue.severity,
                            detail=f"{issue.check}: {issue.detail}",
                        )
                    )

        return report

    # --- checks ----------------------------------------------------

    def _check_duplicates(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        report: DataQualityReport,
    ) -> None:
        report.checks_run.append("duplicate_candles")
        dupes = (
            self.db.execute(
                text("""
                SELECT open_time, count(*) AS n
                FROM raw_ohlcv
                WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
                  AND open_time BETWEEN :start AND :end
                GROUP BY open_time
                HAVING count(*) > 1
                """),
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "start": start,
                    "end": end,
                },
            )
            .mappings()
            .all()
        )
        for row in dupes:
            report.issues.append(
                Issue(
                    check="duplicate_candles",
                    severity="critical",
                    detail=f"open_time={row['open_time'].isoformat()} appears {row['n']} times",
                )
            )

    def _check_ohlc_validity(
        self, rows: list[dict], timeframe: str, now: datetime, report: DataQualityReport
    ) -> None:
        report.checks_run.append("invalid_ohlc")
        candles = [_row_to_candle(r) for r in rows]
        result = validate_candles(candles, timeframe, now + timedelta(seconds=1))
        for failure in result.failures:
            if failure.reason == "duplicate open_time within batch":
                continue  # already covered by _check_duplicates
            report.issues.append(
                Issue(
                    check="invalid_ohlc",
                    severity="critical",
                    detail=f"open_time={failure.open_time.isoformat()}: {failure.reason}",
                )
            )

    def _check_timestamp_alignment(
        self, rows: list[dict], timeframe: str, report: DataQualityReport
    ) -> None:
        report.checks_run.append("timestamp_alignment")
        for row in rows:
            if not is_aligned(row["open_time"], timeframe):
                report.issues.append(
                    Issue(
                        check="timestamp_alignment",
                        severity="warning",
                        detail=(
                            f"open_time={row['open_time'].isoformat()} "
                            f"not aligned to {timeframe}"
                        ),
                    )
                )

    def _check_volume_anomalies(self, rows: list[dict], report: DataQualityReport) -> None:
        report.checks_run.append("volume_anomalies")
        volumes = [float(r["volume"]) for r in rows]
        if len(volumes) < 10:
            return
        mean = statistics.mean(volumes)
        stdev = statistics.pstdev(volumes)
        if stdev == 0:
            return
        threshold = self.config.volume_anomaly_zscore_threshold
        for row, volume in zip(rows, volumes, strict=True):
            z = (volume - mean) / stdev
            if abs(z) >= threshold:
                report.issues.append(
                    Issue(
                        check="volume_anomalies",
                        severity="info",
                        detail=(
                            f"open_time={row['open_time'].isoformat()} "
                            f"volume={volume} z={z:.2f}"
                        ),
                    )
                )

    def _check_missing_candles(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        report: DataQualityReport,
    ) -> None:
        report.checks_run.append("missing_candles")
        gaps = (
            self.db.execute(
                text("""
                SELECT gap_start, gap_end
                FROM ingestion_gap
                WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
                  AND status = 'pending'
                  AND gap_start <= :end AND gap_end >= :start
                ORDER BY gap_start
                """),
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "start": start,
                    "end": end,
                },
            )
            .mappings()
            .all()
        )
        for gap in gaps:
            report.issues.append(
                Issue(
                    check="missing_candles",
                    severity="warning",
                    detail=(
                        f"unresolved gap from {gap['gap_start'].isoformat()} "
                        f"to {gap['gap_end'].isoformat()} (per GapDetectionService)"
                    ),
                )
            )

    def _check_timeframe_consistency(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        rows: list[dict],
        report: DataQualityReport,
    ) -> None:
        report.checks_run.append("timeframe_consistency")
        if timeframe == _BASE_TIMEFRAME or not rows:
            return

        interval = timeframe_to_timedelta(timeframe)
        base_interval = timeframe_to_timedelta(_BASE_TIMEFRAME)
        expected_sub_candles = int(interval / base_interval)

        for row in rows:
            window_start = row["open_time"]
            window_end = window_start + interval
            sub_rows = (
                self.db.execute(
                    text("""
                    SELECT open_time, open, high, low, close, volume
                    FROM raw_ohlcv
                    WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :base_timeframe
                      AND open_time >= :window_start AND open_time < :window_end
                    ORDER BY open_time
                    """),
                    {
                        "exchange": exchange,
                        "symbol": symbol,
                        "base_timeframe": _BASE_TIMEFRAME,
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
                .mappings()
                .all()
            )
            if len(sub_rows) != expected_sub_candles:
                # Incomplete 1m coverage for this window is a
                # completeness gap (already surfaced elsewhere), not a
                # consistency mismatch — skip rather than compare partial data.
                continue

            agg_open = float(sub_rows[0]["open"])
            agg_close = float(sub_rows[-1]["close"])
            agg_high = max(float(r["high"]) for r in sub_rows)
            agg_low = min(float(r["low"]) for r in sub_rows)
            agg_volume = sum(float(r["volume"]) for r in sub_rows)

            mismatches = [
                name
                for name, stored, aggregated in (
                    ("open", float(row["open"]), agg_open),
                    ("high", float(row["high"]), agg_high),
                    ("low", float(row["low"]), agg_low),
                    ("close", float(row["close"]), agg_close),
                    ("volume", float(row["volume"]), agg_volume),
                )
                if not math.isclose(stored, aggregated, rel_tol=1e-6, abs_tol=1e-8)
            ]
            if mismatches:
                report.issues.append(
                    Issue(
                        check="timeframe_consistency",
                        severity="warning",
                        detail=(
                            f"open_time={row['open_time'].isoformat()} {timeframe} candle "
                            f"disagrees with its {expected_sub_candles} {_BASE_TIMEFRAME} "
                            f"sub-candles on: {', '.join(mismatches)}"
                        ),
                    )
                )

    def _check_cross_exchange(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        rows: list[dict],
        now: datetime,
        report: DataQualityReport,
    ) -> None:
        report.checks_run.append("cross_check_exchange")
        if not rows or self.adapter is None:
            return
        recent = rows[-min(len(rows), 20) :]
        start = recent[0]["open_time"]
        end = recent[-1]["open_time"]
        try:
            fetched = self.adapter.fetch_klines(
                symbol, timeframe, start, end, limit=len(recent) + 1
            )
        except Exception as exc:
            logger.warning("cross_check_fetch_failed", error=str(exc))
            return

        fetched_by_time = {c.open_time: c for c in fetched}
        for row in recent:
            candle = fetched_by_time.get(row["open_time"])
            if candle is None:
                continue
            if (
                float(row["open"]) != candle.open
                or float(row["high"]) != candle.high
                or float(row["low"]) != candle.low
                or float(row["close"]) != candle.close
            ):
                report.cross_check_diffs += 1
                report.issues.append(
                    Issue(
                        check="cross_check_exchange",
                        severity="critical",
                        detail=(
                            f"open_time={row['open_time'].isoformat()} "
                            "stored value differs from exchange"
                        ),
                    )
                )

    # --- persistence -------------------------------------------------

    def _fetch_rows(
        self, exchange: str, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[dict]:
        rows = (
            self.db.execute(
                text("""
                SELECT open_time, open, high, low, close, volume
                FROM raw_ohlcv
                WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
                  AND open_time BETWEEN :start AND :end
                ORDER BY open_time
                """),
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "start": start,
                    "end": end,
                },
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    def _store_report(
        self, exchange: str, symbol: str, timeframe: str, now: datetime, report: DataQualityReport
    ) -> None:
        self.db.execute(
            text("""
                INSERT INTO data_quality_report (
                    exchange, symbol, timeframe, run_at, checks_run, issues_found,
                    candles_checked, cross_check_diffs, summary
                ) VALUES (
                    :exchange, :symbol, :timeframe, :run_at, :checks_run, :issues_found,
                    :candles_checked, :cross_check_diffs, :summary
                )
                """),
            {
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "run_at": now,
                "checks_run": json.dumps(report.checks_run),
                "issues_found": json.dumps(
                    [
                        {"check": i.check, "severity": i.severity, "detail": i.detail}
                        for i in report.issues
                    ]
                ),
                "candles_checked": report.candles_checked,
                "cross_check_diffs": report.cross_check_diffs,
                "summary": report.summary,
            },
        )
        self.db.commit()


_BASE_TIMEFRAME = "1m"  # the one timeframe every tracked instrument ingests unconditionally


def _row_to_candle(row: dict) -> RawCandle:
    return RawCandle(
        open_time=row["open_time"],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        close_time=row["open_time"],
        is_closed=True,
    )
