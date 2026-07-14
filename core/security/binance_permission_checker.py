"""
Real Binance implementation of ExchangePermissionChecker — calls the
signed GET /sapi/v1/account/apiRestrictions endpoint, whose
`enableWithdrawals` field is exactly the "is this key more powerful
than a trading-only key should be" signal decision #2 exists to catch.

Reuses Stage 2's ClockSyncService (core.execution.binance_clock_sync)
rather than building a second clock-offset mechanism — the signing
requirement here is identical to every other Binance signed endpoint.
"""

import hashlib
import hmac
import urllib.parse

import requests
import structlog

from core.execution.binance_clock_sync import ClockSyncService
from core.ingestion.errors import FatalIngestionError, RetryableIngestionError
from core.security.permission_checker import PermissionCheckResult

logger = structlog.get_logger(__name__)


class BinancePermissionChecker:
    def __init__(
        self,
        base_url: str,
        clock_sync: ClockSyncService,
        session: requests.Session | None = None,
        timeout_seconds: float = 10.0,
        recv_window_ms: int = 5000,
    ):
        self._base_url = base_url
        self._clock_sync = clock_sync
        self._session = session or requests.Session()
        self._timeout = timeout_seconds
        self._recv_window_ms = recv_window_ms

    def check_permissions(self, api_key: str, api_secret: str) -> PermissionCheckResult:
        params: dict[str, int | str] = {
            "timestamp": self._clock_sync.corrected_timestamp_ms(),
            "recvWindow": self._recv_window_ms,
        }
        query = urllib.parse.urlencode(params)
        params["signature"] = hmac.new(
            api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()

        try:
            response = self._session.get(
                f"{self._base_url}/sapi/v1/account/apiRestrictions",
                params=params,
                headers={"X-MBX-APIKEY": api_key},
                timeout=self._timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise RetryableIngestionError(f"permission check failed: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableIngestionError(
                f"permission check returned {response.status_code}: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise FatalIngestionError(
                f"permission check returned {response.status_code}: {response.text[:200]}"
            )

        body = response.json()
        return PermissionCheckResult(
            withdrawals_enabled=bool(body.get("enableWithdrawals", False)), raw=body
        )
