"""
No real network calls — a fake requests.Session stands in, exactly like
BinanceAdapter's own tests (tests/ingestion/test_binance_adapter.py).
"""

import pytest

from core.execution.binance_clock_sync import ClockSyncService
from core.ingestion.errors import RetryableIngestionError


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text_body: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text_body

    def json(self):
        return self._json_body


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_url = None

    def get(self, url, timeout=None):
        self.last_url = url
        return self._response


def test_sync_computes_positive_offset_when_server_is_ahead():
    # Server thinks it's 5000ms later than our local clock.
    session = _FakeSession(_FakeResponse(200, json_body={"serverTime": 1_700_000_005_000}))
    service = ClockSyncService(
        base_url="https://testnet.binance.vision",
        session=session,
        clock=lambda: 1_700_000_000.0,
    )

    offset = service.sync()

    assert offset == 5000
    assert service.offset_ms == 5000


def test_sync_computes_negative_offset_when_server_is_behind():
    session = _FakeSession(_FakeResponse(200, json_body={"serverTime": 1_699_999_997_000}))
    service = ClockSyncService(
        base_url="https://testnet.binance.vision",
        session=session,
        clock=lambda: 1_700_000_000.0,
    )

    offset = service.sync()
    assert offset == -3000


def test_corrected_timestamp_applies_the_offset():
    session = _FakeSession(_FakeResponse(200, json_body={"serverTime": 1_700_000_005_000}))
    service = ClockSyncService(
        base_url="https://testnet.binance.vision",
        session=session,
        clock=lambda: 1_700_000_000.0,
    )
    service.sync()

    # Same fixed clock -> corrected timestamp is local + offset.
    assert service.corrected_timestamp_ms() == 1_700_000_000_000 + 5000


def test_corrected_timestamp_before_any_sync_is_uncorrected():
    session = _FakeSession(_FakeResponse(200, json_body={"serverTime": 0}))
    service = ClockSyncService(
        base_url="https://testnet.binance.vision",
        session=session,
        clock=lambda: 1_700_000_000.0,
    )
    assert service.offset_ms == 0
    assert service.corrected_timestamp_ms() == 1_700_000_000_000


def test_sync_raises_retryable_on_server_error_response():
    session = _FakeSession(_FakeResponse(503, text_body="Service Unavailable"))
    service = ClockSyncService(base_url="https://testnet.binance.vision", session=session)

    with pytest.raises(RetryableIngestionError):
        service.sync()


def test_sync_raises_retryable_on_network_failure():
    import requests

    class _RaisingSession:
        def get(self, url, timeout=None):
            raise requests.exceptions.ConnectionError("refused")

    service = ClockSyncService(base_url="https://testnet.binance.vision", session=_RaisingSession())

    with pytest.raises(RetryableIngestionError):
        service.sync()
