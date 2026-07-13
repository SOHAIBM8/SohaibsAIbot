import asyncio
import time

from core.marketdata.websocket_connection import WebSocketConnection


def wait_until(predicate, timeout_s=5.0, interval_s=0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def test_receives_messages_and_reports_alive(fake_server):
    async def handler(ws):
        await ws.send("hello")
        await asyncio.Future()  # keep the connection open indefinitely

    server = fake_server(handler)
    received = []
    conn = WebSocketConnection(
        url=f"ws://localhost:{server.port}", on_message=received.append, heartbeat_timeout_s=5.0
    )
    conn.connect()
    try:
        assert wait_until(lambda: received == ["hello"])
        assert conn.is_alive() is True
    finally:
        conn.close()


def test_is_alive_false_before_connecting():
    conn = WebSocketConnection(
        url="ws://localhost:1", on_message=lambda m: None, heartbeat_timeout_s=1.0
    )
    assert conn.is_alive() is False


def test_reconnects_after_the_server_drops_the_connection(fake_server):
    """A handler that sends one message then returns closes the
    connection from the server side — the client must notice and
    reconnect, not just quietly stop."""

    async def handler(ws):
        await ws.send("tick")
        # returning ends this connection; the next connection gets a
        # fresh call to `handler`, proving a real reconnect happened.

    server = fake_server(handler)
    received = []
    conn = WebSocketConnection(
        url=f"ws://localhost:{server.port}",
        on_message=received.append,
        heartbeat_timeout_s=5.0,
        base_backoff_s=0.05,
        max_backoff_s=0.2,
        rand=lambda: 0.0,
    )
    conn.connect()
    try:
        assert wait_until(lambda: len(received) >= 3, timeout_s=15.0)
        assert conn.reconnect_count >= 2
    finally:
        conn.close()


def test_heartbeat_timeout_triggers_reconnect(fake_server):
    """The connection stays technically open but the server never
    sends anything — the client must treat this as stale and force a
    reconnect rather than sitting there believing it's fine."""

    async def handler(ws):
        await asyncio.Future()  # accept the connection, then go silent forever

    server = fake_server(handler)
    conn = WebSocketConnection(
        url=f"ws://localhost:{server.port}",
        on_message=lambda m: None,
        heartbeat_timeout_s=0.2,
        base_backoff_s=0.05,
        max_backoff_s=0.2,
        rand=lambda: 0.0,
    )
    conn.connect()
    try:
        assert wait_until(lambda: conn.reconnect_count >= 1, timeout_s=15.0)
    finally:
        conn.close()


def test_backoff_prevents_a_tight_reconnect_loop():
    """Nothing is listening on this port at all — every connection
    attempt fails. A broken (non-backing-off) implementation would
    reconnect as fast as connect_timeout_s allows, in a tight loop; a
    correct one keeps attempts bounded and delays growing between them."""
    conn = WebSocketConnection(
        url="ws://localhost:1",  # refused/unbound, no server there
        on_message=lambda m: None,
        heartbeat_timeout_s=1.0,
        base_backoff_s=0.05,
        max_backoff_s=1.0,
        connect_timeout_s=0.15,  # bound how long a failed attempt itself takes
        rand=lambda: 0.0,
    )
    conn.connect()
    try:
        assert wait_until(lambda: len(conn.backoff_delays) >= 2, timeout_s=15.0)
        # Backoff must actually grow between consecutive failures, not
        # stay flat — proof this isn't a disguised tight loop.
        assert conn.backoff_delays[1] > conn.backoff_delays[0]
    finally:
        conn.close()


def test_close_stops_the_background_thread(fake_server):
    async def handler(ws):
        await asyncio.Future()

    server = fake_server(handler)
    conn = WebSocketConnection(
        url=f"ws://localhost:{server.port}", on_message=lambda m: None, heartbeat_timeout_s=5.0
    )
    conn.connect()
    assert wait_until(lambda: conn.is_alive())

    conn.close()

    assert conn.is_alive() is False
    assert conn._thread is not None
    assert conn._thread.is_alive() is False
