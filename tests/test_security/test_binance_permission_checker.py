"""
No real network calls — a fake requests.Session stands in, matching
every other Binance-facing component's test pattern in this project.
"""

import pytest

from core.execution.binance_clock_sync import ClockSyncService
from core.ingestion.errors import FatalIngestionError, RetryableIngestionError
from core.security.binance_permission_checker import BinancePermissionChecker


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text_body: str = ""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text_body

    def json(self):
        return self._json_body


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.last_params = None
        self.last_headers = None

    def get(self, url, params=None, headers=None, timeout=None):
        self.last_params = params
        self.last_headers = headers
        return self._response


def make_checker(session) -> BinancePermissionChecker:
    clock_sync = ClockSyncService(
        base_url="https://testnet.binance.vision", session=session, clock=lambda: 1_700_000_000.0
    )
    return BinancePermissionChecker(
        base_url="https://testnet.binance.vision", clock_sync=clock_sync, session=session
    )


def test_withdrawals_disabled_is_reported_correctly():
    session = _FakeSession(_FakeResponse(200, json_body={"enableWithdrawals": False}))
    checker = make_checker(session)

    result = checker.check_permissions(api_key="k", api_secret="s")

    assert result.withdrawals_enabled is False
    assert result.raw == {"enableWithdrawals": False}


def test_withdrawals_enabled_is_reported_correctly():
    session = _FakeSession(_FakeResponse(200, json_body={"enableWithdrawals": True}))
    checker = make_checker(session)

    result = checker.check_permissions(api_key="k", api_secret="s")

    assert result.withdrawals_enabled is True


def test_request_carries_the_api_key_header_not_the_secret():
    session = _FakeSession(_FakeResponse(200, json_body={"enableWithdrawals": False}))
    checker = make_checker(session)

    checker.check_permissions(api_key="my-api-key", api_secret="my-secret")

    assert session.last_headers["X-MBX-APIKEY"] == "my-api-key"
    assert "my-secret" not in session.last_headers.values()
    assert "my-secret" not in str(session.last_params.values())


def test_request_is_signed():
    session = _FakeSession(_FakeResponse(200, json_body={"enableWithdrawals": False}))
    checker = make_checker(session)

    checker.check_permissions(api_key="k", api_secret="s")

    assert "signature" in session.last_params
    assert "timestamp" in session.last_params


def test_server_error_raises_retryable():
    session = _FakeSession(_FakeResponse(503, text_body="down"))
    checker = make_checker(session)

    with pytest.raises(RetryableIngestionError):
        checker.check_permissions(api_key="k", api_secret="s")


def test_client_error_raises_fatal():
    session = _FakeSession(_FakeResponse(401, text_body="unauthorized"))
    checker = make_checker(session)

    with pytest.raises(FatalIngestionError):
        checker.check_permissions(api_key="k", api_secret="s")


def test_network_failure_raises_retryable():
    import requests

    class _RaisingSession:
        def get(self, url, params=None, headers=None, timeout=None):
            raise requests.exceptions.ConnectionError("refused")

    clock_sync = ClockSyncService(
        base_url="https://testnet.binance.vision",
        session=_RaisingSession(),
        clock=lambda: 1_700_000_000.0,
    )
    checker = BinancePermissionChecker(
        base_url="https://testnet.binance.vision", clock_sync=clock_sync, session=_RaisingSession()
    )
    with pytest.raises(RetryableIngestionError):
        checker.check_permissions(api_key="k", api_secret="s")
