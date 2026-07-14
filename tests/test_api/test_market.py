"""
Live Market API integration tests against real local Postgres. Seeds
via the real upsert_candles() write path, same as
tests/ingestion/test_ohlcv_reader.py.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core.ingestion.raw_ohlcv_store import upsert_candles
from core.ingestion.types import RawCandle
from tests.test_api.conftest import TEST_OPERATOR_PASSWORD, TEST_OPERATOR_USERNAME

EXCHANGE = "test_api_exchange"
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"


def _logged_in(client):
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_OPERATOR_PASSWORD},
    )
    assert response.status_code == 200
    return client


@pytest.fixture
def seeded_candles(db):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    candles = [
        RawCandle(
            open_time=base + timedelta(hours=i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=5.0,
            close_time=base + timedelta(hours=i + 1),
        )
        for i in range(3)
    ]
    upsert_candles(db, EXCHANGE, SYMBOL, TIMEFRAME, candles, source_run_id=None)
    yield
    db.execute(text("DELETE FROM raw_ohlcv WHERE exchange = :e"), {"e": EXCHANGE})
    db.commit()


def test_list_candles_requires_auth(client):
    response = client.get(
        "/api/market/candles",
        params={"exchange": EXCHANGE, "symbol": SYMBOL, "timeframe": TIMEFRAME},
    )
    assert response.status_code == 401


def test_list_candles_returns_seeded_data_in_order(client, seeded_candles):
    _logged_in(client)

    response = client.get(
        "/api/market/candles",
        params={"exchange": EXCHANGE, "symbol": SYMBOL, "timeframe": TIMEFRAME},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    closes = [c["close"] for c in body]
    assert closes == sorted(closes)


def test_list_candles_returns_empty_for_unknown_symbol(client):
    _logged_in(client)
    response = client.get(
        "/api/market/candles",
        params={"exchange": EXCHANGE, "symbol": "NO/SUCH", "timeframe": TIMEFRAME},
    )
    assert response.status_code == 200
    assert response.json() == []


def test_list_candles_requires_query_params(client):
    _logged_in(client)
    response = client.get("/api/market/candles")
    assert response.status_code == 422
