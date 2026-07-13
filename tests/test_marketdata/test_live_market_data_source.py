"""
Unit tests use a fake connection_factory — no real sockets — to
isolate LiveMarketDataSource's own logic (caching, malformed-message
handling) from WebSocketConnection, which already has its own
dedicated real-socket tests in test_websocket_connection.py. The
end-to-end wiring (real WebSocket -> normalizer -> PaperExecutionAdapter
fill) is covered separately in test_paper_adapter_live_market_data.py.
"""

import pytest

from core.marketdata.live_market_data_source import LiveMarketDataSource


class _FakeConnection:
    """Captures the on_message callback so a test can drive it
    directly, instead of needing a real WebSocket round-trip."""

    def __init__(self, url, on_message, heartbeat_timeout_s, **kwargs):
        self.url = url
        self.on_message = on_message
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.kwargs = kwargs
        self.connected = False
        self.closed = False
        self._alive = False

    def connect(self):
        self.connected = True
        self._alive = True

    def close(self):
        self.closed = True
        self._alive = False

    def is_alive(self):
        return self._alive


def make_source():
    connection_holder = {}

    def factory(**kwargs):
        conn = _FakeConnection(**kwargs)
        connection_holder["conn"] = conn
        return conn

    source = LiveMarketDataSource(
        url="ws://fake", heartbeat_timeout_s=5.0, connection_factory=factory
    )
    return source, connection_holder["conn"]


def test_get_last_price_raises_before_any_message_received():
    source, _ = make_source()
    with pytest.raises(KeyError, match="no market data received yet"):
        source.get_last_price("BTC/USDT")


def test_valid_message_updates_last_price():
    source, conn = make_source()
    conn.on_message(
        '{"symbol": "BTC/USDT", "price": 65000.5, "timestamp": "2024-06-01T12:00:00+00:00"}'
    )

    assert source.get_last_price("BTC/USDT") == 65000.5


def test_later_messages_overwrite_the_cached_price():
    source, conn = make_source()
    conn.on_message(
        '{"symbol": "BTC/USDT", "price": 100.0, "timestamp": "2024-06-01T12:00:00+00:00"}'
    )
    conn.on_message(
        '{"symbol": "BTC/USDT", "price": 200.0, "timestamp": "2024-06-01T12:00:01+00:00"}'
    )

    assert source.get_last_price("BTC/USDT") == 200.0


def test_prices_are_tracked_independently_per_symbol():
    source, conn = make_source()
    conn.on_message(
        '{"symbol": "BTC/USDT", "price": 65000.0, "timestamp": "2024-06-01T12:00:00+00:00"}'
    )
    conn.on_message(
        '{"symbol": "ETH/USDT", "price": 3200.0, "timestamp": "2024-06-01T12:00:00+00:00"}'
    )

    assert source.get_last_price("BTC/USDT") == 65000.0
    assert source.get_last_price("ETH/USDT") == 3200.0


def test_malformed_json_is_ignored_not_raised():
    source, conn = make_source()
    conn.on_message("not valid json{{{")  # must not raise out of the callback

    with pytest.raises(KeyError):
        source.get_last_price("BTC/USDT")


def test_valid_json_failing_normalization_is_ignored_not_raised():
    source, conn = make_source()
    conn.on_message(
        '{"symbol": "BTC/USDT", "price": -5.0, "timestamp": "2024-06-01T12:00:00+00:00"}'
    )

    with pytest.raises(KeyError):
        source.get_last_price("BTC/USDT")


def test_start_and_stop_delegate_to_the_connection():
    source, conn = make_source()
    assert conn.connected is False

    source.start()
    assert conn.connected is True

    source.stop()
    assert conn.closed is True


def test_is_connected_delegates_to_the_connection():
    source, conn = make_source()
    assert source.is_connected() is False

    source.start()
    assert source.is_connected() is True
