"""
DataQualityService (spec 4.11). Distinct from GapDetectionService:
that asks "is anything missing", this asks "is what's stored actually
correct" — duplicates, OHLC violations, timestamp drift, volume
anomalies, cross-timeframe reconciliation, and (given an adapter)
drift against the exchange itself. Each check is independent and
reported separately so a single bad check never masks the others.
"""

import json
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
from core.ingestion.timeframe import is_aligned
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
