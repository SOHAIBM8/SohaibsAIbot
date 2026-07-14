"""
Tracks system clock offset against Binance server time (decision #7).
Every signed request must carry a corrected timestamp — Binance rejects
a signed request whose timestamp deviates too far from its own clock
(-1021 "Timestamp for this request is outside of the recvWindow"), and
assuming the local clock is accurate is exactly the "works in dev,
fails once the host clock drifts" trap this exists to avoid.

sync() is a deliberate, explicit call — not automatic on construction —
so BinanceExecutionAdapter/a scheduled keepalive controls exactly when
a sync roundtrip happens, rather than paying for one on every
instantiation (e.g. in a test).
"""

import time
from collections.abc import Callable

import requests
import structlog

from core.ingestion.errors import RetryableIngestionError

logger = structlog.get_logger(__name__)


class ClockSyncService:
    def __init__(
        self,
        base_url: str,
        session: requests.Session | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] | None = None,
    ):
        self._base_url = base_url
        self._session = session or requests.Session()
        self._timeout = timeout_seconds
        self._clock = clock or time.time
        self._offset_ms = 0

    def sync(self) -> int:
        """Fetch Binance server time, compute and store the offset
        (server_time_ms - local_time_ms). Returns the new offset."""
        try:
            response = self._session.get(f"{self._base_url}/api/v3/time", timeout=self._timeout)
        except requests.exceptions.RequestException as exc:
            raise RetryableIngestionError(f"clock sync failed: {exc}") from exc

        if response.status_code >= 400:
            raise RetryableIngestionError(
                f"clock sync returned {response.status_code}: {response.text[:200]}"
            )

        server_time_ms = response.json()["serverTime"]
        local_time_ms = int(self._clock() * 1000)
        self._offset_ms = server_time_ms - local_time_ms
        logger.info("clock_sync_updated", offset_ms=self._offset_ms)
        return self._offset_ms

    @property
    def offset_ms(self) -> int:
        return self._offset_ms

    def corrected_timestamp_ms(self) -> int:
        """What every signed request's `timestamp` param must use —
        never the raw local clock."""
        return int(self._clock() * 1000) + self._offset_ms
