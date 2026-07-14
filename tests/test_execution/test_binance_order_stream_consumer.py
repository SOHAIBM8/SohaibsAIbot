"""
No real network/websocket — a fake connection_factory captures the
on_message AND on_open callbacks so a test can drive them directly,
exactly like tests/test_marketdata/test_live_market_data_source.py
does for LiveMarketDataSource. A fake OrderManager double records
handle_fill() calls without touching Postgres — this suite is about
the consumer's own auth/parsing/dispatch logic, not OrderManager's
persistence.

Auth flow (rewritten after discovering Binance's listenKey REST
endpoint returns 410 Gone on real testnet — see the module's own
docstring): on every connection, the consumer must send a signed
userDataStream.subscribe.signature message BEFORE anything else.
"""

import json
from datetime import UTC, datetime

import pytest

from core.execution.binance_order_stream_consumer import BinanceOrderStreamConsumer
from core.execution.order import Fill

API_KEY_ENV = "TEST_STREAM_BINANCE_API_KEY"
API_SECRET_ENV = "TEST_STREAM_BINANCE_API_SECRET"


@pytest.fixture(autouse=True)
def _fake_credentials(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "fake-api-key")
    monkeypatch.setenv(API_SECRET_ENV, "fake-api-secret")


class _FakeConnection:
    def __init__(self, url, on_message, heartbeat_timeout_s, on_open=None, **kwargs):
        self.url = url
        self.on_message = on_message
        self.on_open = on_open
        self.connected = False
        self.closed = False
        self._alive = False
        self.sent_on_open: list[str] = []

    def connect(self):
        self.connected = True
        self._alive = True
        if self.on_open is not None:
            self.sent_on_open.append(self.on_open())

    def close(self):
        self.closed = True
        self._alive = False

    def is_alive(self):
        return self._alive


class _FakeOrderManager:
    def __init__(self, raise_for_client_order_ids=()):
        self.handled_fills: list[Fill] = []
        self._raise_for = set(raise_for_client_order_ids)

    def handle_fill(self, fill: Fill) -> None:
        if fill.client_order_id in self._raise_for:
            raise KeyError(f"unknown client_order_id: {fill.client_order_id}")
        self.handled_fills.append(fill)


def make_consumer(order_manager, timestamp_fn=None, request_id_fn=None):
    connection_holder = {}

    def factory(**kwargs):
        conn = _FakeConnection(**kwargs)
        connection_holder["conn"] = conn
        return conn

    consumer = BinanceOrderStreamConsumer(
        ws_url="wss://ws-api.testnet.binance.vision/ws-api/v3",
        order_manager=order_manager,
        api_key_env_var=API_KEY_ENV,
        api_secret_env_var=API_SECRET_ENV,
        connection_factory=factory,
        timestamp_fn=timestamp_fn or (lambda: 1_700_000_000_000),
        request_id_fn=request_id_fn or (lambda: "req-1"),
    )
    return consumer, connection_holder["conn"]


def execution_event(
    client_order_id="co-1",
    exec_type="TRADE",
    order_status="FILLED",
    last_qty="0.01",
    last_price="65000.0",
    commission="0.0001",
    transact_time_ms=1_717_200_000_000,
) -> dict:
    return {
        "e": "executionReport",
        "s": "BTCUSDT",
        "c": client_order_id,
        "x": exec_type,
        "X": order_status,
        "l": last_qty,
        "L": last_price,
        "n": commission,
        "T": transact_time_ms,
    }


def wrapped_event(**kwargs) -> str:
    return json.dumps({"subscriptionId": 0, "event": execution_event(**kwargs)})


# --- subscribe-on-connect auth flow -----------------------------------


def test_start_sends_a_signed_subscribe_message_on_connect():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    consumer.start()

    assert len(conn.sent_on_open) == 1
    message = json.loads(conn.sent_on_open[0])
    assert message["method"] == "userDataStream.subscribe.signature"
    assert message["params"]["apiKey"] == "fake-api-key"
    assert message["params"]["timestamp"] == 1_700_000_000_000
    assert "signature" in message["params"]
    assert message["id"] == "req-1"


def test_subscribe_signature_is_deterministic_for_the_same_params():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)
    consumer.start()
    signature_1 = json.loads(conn.sent_on_open[0])["params"]["signature"]

    consumer2, conn2 = make_consumer(order_manager)
    consumer2.start()
    signature_2 = json.loads(conn2.sent_on_open[0])["params"]["signature"]

    assert signature_1 == signature_2  # same key/secret/timestamp -> same signature


def test_a_reconnect_resends_the_subscribe_message():
    """Every reconnect needs a fresh subscribe — there is no persistent
    listenKey anymore, the subscription lives only as long as this
    specific connection does."""
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    conn.connect()  # simulate WebSocketConnection's internal reconnect calling on_open again
    conn.connect()

    assert len(conn.sent_on_open) == 2


# --- event parsing ------------------------------------------------------


def test_trade_execution_report_is_forwarded_to_handle_fill():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    conn.on_message(wrapped_event())

    assert len(order_manager.handled_fills) == 1
    fill = order_manager.handled_fills[0]
    assert fill.client_order_id == "co-1"
    assert fill.fill_price == 65000.0
    assert fill.quantity == 0.01
    assert fill.fee == 0.0001
    assert fill.is_partial is False
    assert fill.filled_at == datetime.fromtimestamp(1_717_200_000_000 / 1000, tz=UTC)


def test_partial_fill_sets_is_partial_true():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    conn.on_message(wrapped_event(order_status="PARTIALLY_FILLED"))

    assert order_manager.handled_fills[0].is_partial is True


def test_non_trade_execution_types_are_ignored():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    for exec_type in ["NEW", "CANCELED", "REJECTED", "EXPIRED"]:
        conn.on_message(wrapped_event(exec_type=exec_type))

    assert order_manager.handled_fills == []


def test_non_execution_report_events_are_ignored():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    conn.on_message(
        json.dumps({"subscriptionId": 0, "event": {"e": "outboundAccountPosition", "B": []}})
    )

    assert order_manager.handled_fills == []


def test_subscribe_ack_response_is_ignored_not_treated_as_an_event():
    """The response to our own subscribe request arrives on the same
    socket — it has no 'event' key and must be silently skipped."""
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    conn.on_message(json.dumps({"id": "req-1", "status": 200, "result": {"subscriptionId": 0}}))

    assert order_manager.handled_fills == []


def test_malformed_json_is_ignored_not_raised():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    conn.on_message("not valid json{{{")  # must not raise out of the callback

    assert order_manager.handled_fills == []


def test_malformed_execution_report_is_ignored_not_raised():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    # Missing required fields (l, L, T) for a TRADE report.
    conn.on_message(json.dumps({"event": {"e": "executionReport", "x": "TRADE", "c": "co-1"}}))

    assert order_manager.handled_fills == []


def test_fill_for_unknown_order_is_logged_not_raised():
    order_manager = _FakeOrderManager(raise_for_client_order_ids={"co-unknown"})
    consumer, conn = make_consumer(order_manager)

    conn.on_message(
        wrapped_event(client_order_id="co-unknown")
    )  # must not raise out of the callback

    assert order_manager.handled_fills == []


def test_start_and_stop_delegate_to_the_connection():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    assert conn.connected is False
    consumer.start()
    assert conn.connected is True

    consumer.stop()
    assert conn.closed is True


def test_is_connected_delegates_to_the_connection():
    order_manager = _FakeOrderManager()
    consumer, conn = make_consumer(order_manager)

    assert consumer.is_connected() is False
    consumer.start()
    assert consumer.is_connected() is True
