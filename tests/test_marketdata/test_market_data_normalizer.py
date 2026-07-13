import pytest

from core.marketdata.market_data_normalizer import MarketDataNormalizer, NormalizedTick


def test_normalizes_a_well_formed_payload():
    normalizer = MarketDataNormalizer()
    tick = normalizer.normalize(
        {"symbol": "BTC/USDT", "price": 65000.5, "timestamp": "2024-06-01T12:00:00+00:00"}
    )
    assert isinstance(tick, NormalizedTick)
    assert tick.symbol == "BTC/USDT"
    assert tick.price == 65000.5
    assert tick.timestamp.year == 2024


def test_price_is_coerced_to_float():
    normalizer = MarketDataNormalizer()
    tick = normalizer.normalize(
        {"symbol": "BTC/USDT", "price": "65000.5", "timestamp": "2024-06-01T12:00:00+00:00"}
    )
    assert tick.price == 65000.5


@pytest.mark.parametrize("missing_field", ["symbol", "price", "timestamp"])
def test_missing_field_raises(missing_field):
    payload = {"symbol": "BTC/USDT", "price": 100.0, "timestamp": "2024-06-01T12:00:00+00:00"}
    del payload[missing_field]
    normalizer = MarketDataNormalizer()
    with pytest.raises(ValueError, match="missing required field"):
        normalizer.normalize(payload)


def test_empty_symbol_raises():
    normalizer = MarketDataNormalizer()
    with pytest.raises(ValueError, match="invalid symbol"):
        normalizer.normalize(
            {"symbol": "", "price": 100.0, "timestamp": "2024-06-01T12:00:00+00:00"}
        )


def test_non_numeric_price_raises():
    normalizer = MarketDataNormalizer()
    with pytest.raises(ValueError, match="invalid price"):
        normalizer.normalize(
            {
                "symbol": "BTC/USDT",
                "price": "not-a-number",
                "timestamp": "2024-06-01T12:00:00+00:00",
            }
        )


def test_non_positive_price_raises():
    normalizer = MarketDataNormalizer()
    with pytest.raises(ValueError, match="non-positive price"):
        normalizer.normalize(
            {"symbol": "BTC/USDT", "price": 0.0, "timestamp": "2024-06-01T12:00:00+00:00"}
        )


def test_invalid_timestamp_raises():
    normalizer = MarketDataNormalizer()
    with pytest.raises(ValueError, match="invalid timestamp"):
        normalizer.normalize({"symbol": "BTC/USDT", "price": 100.0, "timestamp": "not-a-timestamp"})
