"""
No real network calls — a fake requests.Session returns a scripted
/exchangeInfo body shaped exactly like Binance's real response.
"""

import pytest

from core.execution.binance_symbol_filter_cache import FilterViolationError, SymbolFilterCache
from core.ingestion.errors import RetryableIngestionError

_EXCHANGE_INFO_RESPONSE = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.00001",
                    "maxQty": "9000",
                    "stepSize": "0.00001",
                },
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0.01",
                    "maxPrice": "1000000",
                    "tickSize": "0.01",
                },
                {"filterType": "MIN_NOTIONAL", "minNotional": "10.0"},
            ],
        },
        {
            "symbol": "ETHUSDT",
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.0001",
                    "maxQty": "9000",
                    "stepSize": "0.0001",
                },
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0.01",
                    "maxPrice": "1000000",
                    "tickSize": "0.01",
                },
                {"filterType": "NOTIONAL", "minNotional": "5.0"},
            ],
        },
    ]
}


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

    def get(self, url, timeout=None):
        return self._response


def make_cache(symbols=None) -> SymbolFilterCache:
    session = _FakeSession(_FakeResponse(200, json_body=_EXCHANGE_INFO_RESPONSE))
    cache = SymbolFilterCache(base_url="https://testnet.binance.vision", session=session)
    cache.refresh(symbols=symbols)
    return cache


def test_get_raises_before_refresh():
    cache = SymbolFilterCache(
        base_url="https://testnet.binance.vision", session=_FakeSession(_FakeResponse(200))
    )
    with pytest.raises(KeyError, match="call refresh"):
        cache.get("BTCUSDT")


def test_refresh_populates_both_min_notional_and_notional_filter_names():
    cache = make_cache()

    btc = cache.get("BTCUSDT")
    assert btc.min_qty == 0.00001
    assert btc.min_notional == 10.0

    eth = cache.get("ETHUSDT")
    assert eth.min_notional == 5.0  # parsed from the renamed "NOTIONAL" filter


def test_refresh_can_narrow_to_a_symbol_subset():
    cache = make_cache(symbols=["BTCUSDT"])
    cache.get("BTCUSDT")
    with pytest.raises(KeyError):
        cache.get("ETHUSDT")


def test_validate_passes_for_a_compliant_order():
    cache = make_cache()
    cache.validate("BTCUSDT", quantity=0.01, price=65000.0)  # must not raise


def test_validate_rejects_quantity_below_min_qty():
    cache = make_cache()
    with pytest.raises(FilterViolationError, match="outside"):
        cache.validate("BTCUSDT", quantity=0.000001, price=65000.0)


def test_validate_rejects_quantity_not_a_step_multiple():
    cache = make_cache()
    with pytest.raises(FilterViolationError, match="step_size"):
        cache.validate("BTCUSDT", quantity=0.010001, price=65000.0)


def test_validate_rejects_price_not_a_tick_multiple():
    cache = make_cache()
    with pytest.raises(FilterViolationError, match="tick_size"):
        cache.validate("BTCUSDT", quantity=0.01, price=65000.001)


def test_validate_rejects_notional_below_minimum():
    cache = make_cache()
    with pytest.raises(FilterViolationError, match="min_notional"):
        cache.validate("BTCUSDT", quantity=0.00001, price=1.0)  # notional = 0.00001


def test_validate_skips_price_and_notional_checks_for_market_orders():
    cache = make_cache()
    cache.validate("BTCUSDT", quantity=0.01, price=None)  # must not raise


def test_refresh_raises_retryable_on_network_failure():
    import requests

    class _RaisingSession:
        def get(self, url, timeout=None):
            raise requests.exceptions.Timeout("timed out")

    cache = SymbolFilterCache(base_url="https://testnet.binance.vision", session=_RaisingSession())
    with pytest.raises(RetryableIngestionError):
        cache.refresh()


def test_refresh_raises_retryable_on_server_error_response():
    session = _FakeSession(_FakeResponse(503, text_body="down"))
    cache = SymbolFilterCache(base_url="https://testnet.binance.vision", session=session)
    with pytest.raises(RetryableIngestionError):
        cache.refresh()
