"""
Observability (spec 4.12): a metrics registry every service updates,
plus a /health and /metrics HTTP server for container/orchestration
liveness checks and the future dashboard. Metric names are a stable
interface once shipped — don't rename them casually later.
"""

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

candles_ingested_total = Counter(
    "ingestion_candles_ingested_total",
    "Candles successfully stored",
    ["exchange", "symbol", "timeframe"],
)
api_latency_seconds = Histogram(
    "ingestion_api_latency_seconds", "Exchange API call latency", ["exchange"]
)
retry_count_total = Counter("ingestion_retry_count_total", "Retries attempted", ["exchange"])
gap_repair_success_total = Counter("ingestion_gap_repair_success_total", "Gaps repaired")
gap_repair_failure_total = Counter(
    "ingestion_gap_repair_failure_total", "Gap repair attempts that found no data"
)
gap_count = Gauge(
    "ingestion_gap_count", "Currently pending gaps", ["exchange", "symbol", "timeframe"]
)
duplicate_count_total = Counter(
    "ingestion_duplicate_count_total",
    "Duplicate candles found by data quality checks (should stay 0)",
)
validation_failure_total = Counter(
    "ingestion_validation_failure_total", "Candles rejected by CandleValidator", ["exchange"]
)


def check_health(db: Session, staleness_seconds: int = 3600) -> dict:
    """DB connectivity + recency of the last successful run per active
    tracked instrument. `staleness_seconds` is deliberately a single
    generic threshold rather than per-timeframe — good enough for a
    liveness probe; per-timeframe freshness is what the metrics/
    dashboard are for."""
    result: dict[str, Any] = {"status": "ok", "checks": {}}

    try:
        db.execute(text("SELECT 1"))
        result["checks"]["database"] = "ok"
    except Exception as exc:
        result["status"] = "unhealthy"
        result["checks"]["database"] = f"error: {exc}"
        return result

    instruments = (
        db.execute(
            text("SELECT exchange, symbol, timeframe FROM tracked_instruments WHERE active = TRUE")
        )
        .mappings()
        .all()
    )

    stale = []
    now = datetime.now(UTC)
    for instrument in instruments:
        last_run = db.execute(
            text("""
                SELECT started_at FROM ingestion_run_log
                WHERE exchange = :exchange AND symbol = :symbol AND timeframe = :timeframe
                ORDER BY started_at DESC LIMIT 1
                """),
            dict(instrument),
        ).scalar()
        if last_run is None or (now - last_run).total_seconds() > staleness_seconds:
            stale.append(
                f"{instrument['exchange']}:{instrument['symbol']}:{instrument['timeframe']}"
            )

    result["checks"]["tracked_instruments"] = len(instruments)
    if stale:
        result["status"] = "degraded"
        result["checks"]["stale_instruments"] = stale
    return result


class ObservabilityServer:
    """/health and /metrics on a background HTTP server thread.
    `session_factory` is called fresh per health check (e.g.
    core.db.SessionLocal) so the server never holds a long-lived
    session across requests."""

    def __init__(
        self, session_factory: Callable[[], Session], host: str = "0.0.0.0", port: int = 9100
    ):
        self._session_factory = session_factory
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        session_factory = self._session_factory

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    session = session_factory()
                    try:
                        health = check_health(session)
                    finally:
                        session.close()
                    status_code = 200 if health["status"] == "ok" else 503
                    body = json.dumps(health).encode()
                    self.send_response(status_code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/metrics":
                    body = generate_latest()
                    self.send_response(200)
                    self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                logger.info("observability_http_request", message=format % args)

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
