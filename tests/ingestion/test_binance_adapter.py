"""
No real network calls — a fake requests.Session stands in so this
exercises BinanceAdapter's parsing and error-classification logic
deterministically (spec section 7: no real network call needed for
component tests). The live end-to-end Binance check is a separate,
manually-run smoke test, not part of this suite.
"""

from datetime import UTC, datetime

import pytest

from core.ingestion.binance_adapter import BinanceAdapter
from core.ingestion.errors import FatalIngestionError, RetryableIngestionError


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text_body: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text_body

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.last_params = params
        return self._response


_SAMPLE_ROW = [
    1717200000000,
    "100.0",
    "101.0",
    "99.0",
    "100.5",
    "10.0",
    1717203599999,
    "1000.0",
    50,
    "5.0",
    "500.0",
    "0",
]


def test_fetch_klines_parses_rows():
    session = _FakeSession(_FakeResponse(200, json_body=[_SAMPLE_ROW]))
    adapter = BinanceAdapter(session=session)

    candles = adapter.fetch_klines(
        "BTC/USDT", "1h", datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 6, 2, tzinfo=UTC), 1000
    )

    assert len(candles) == 1
    assert candles[0].open == 100.0
    assert candles[0].close == 100.5
    assert session.last_params["symbol"] == "BTCUSDT"


def test_earliest_available_returns_none_on_empty_response():
    session = _FakeSession(_FakeResponse(200, json_body=[]))
    adapter = BinanceAdapter(session=session)

    assert adapter.earliest_available("BTC/USDT", "1h") is None


def test_429_raises_retryable():
    session = _FakeSession(_FakeResponse(429, text_body="rate limited"))
    adapter = BinanceAdapter(session=session)

    with pytest.raises(RetryableIngestionError):
        adapter.fetch_klines(
            "BTC/USDT",
            "1h",
            datetime(2024, 6, 1, tzinfo=UTC),
            datetime(2024, 6, 2, tzinfo=UTC),
            1000,
        )


def test_5xx_raises_retryable():
    session = _FakeSession(_FakeResponse(503, text_body="unavailable"))
    adapter = BinanceAdapter(session=session)

    with pytest.raises(RetryableIngestionError):
        adapter.fetch_klines(
            "BTC/USDT",
            "1h",
            datetime(2024, 6, 1, tzinfo=UTC),
            datetime(2024, 6, 2, tzinfo=UTC),
            1000,
        )


def test_400_raises_fatal():
    session = _FakeSession(_FakeResponse(400, text_body="bad symbol"))
    adapter = BinanceAdapter(session=session)

    with pytest.raises(FatalIngestionError):
        adapter.fetch_klines(
            "BTC/USDT",
            "1h",
            datetime(2024, 6, 1, tzinfo=UTC),
            datetime(2024, 6, 2, tzinfo=UTC),
            1000,
        )


def test_malformed_response_shape_raises_fatal():
    session = _FakeSession(_FakeResponse(200, json_body={"not": "a list"}))
    adapter = BinanceAdapter(session=session)

    with pytest.raises(FatalIngestionError):
        adapter.fetch_klines(
            "BTC/USDT",
            "1h",
            datetime(2024, 6, 1, tzinfo=UTC),
            datetime(2024, 6, 2, tzinfo=UTC),
            1000,
        )


def test_normalize_symbol():
    adapter = BinanceAdapter(session=_FakeSession(_FakeResponse(200, json_body=[])))
    assert adapter.normalize_symbol("BTC/USDT") == "BTCUSDT"
    assert adapter.normalize_symbol("btc-usdt") == "BTCUSDT"
