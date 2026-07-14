"""
Pure unit tests — a fake socket double with an async send_json spy, no
real WebSocket needed.
"""

import pytest

from api.websocket.connection_manager import ConnectionManager


class _FakeSocket:
    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self._fail = fail

    async def send_json(self, message: dict) -> None:
        if self._fail:
            raise RuntimeError("connection closed")
        self.sent.append(message)


@pytest.mark.anyio
async def test_broadcast_delivers_only_to_the_target_account():
    manager = ConnectionManager()
    socket_a = _FakeSocket()
    socket_b = _FakeSocket()
    manager.connect("account_a", socket_a)  # type: ignore[arg-type]
    manager.connect("account_b", socket_b)  # type: ignore[arg-type]

    await manager.broadcast_to_account("account_a", {"event_type": "Test"})

    assert socket_a.sent == [{"event_type": "Test"}]
    assert socket_b.sent == []


@pytest.mark.anyio
async def test_broadcast_reaches_multiple_connections_for_the_same_account():
    manager = ConnectionManager()
    socket_1 = _FakeSocket()
    socket_2 = _FakeSocket()
    manager.connect("account_a", socket_1)  # type: ignore[arg-type]
    manager.connect("account_a", socket_2)  # type: ignore[arg-type]

    await manager.broadcast_to_account("account_a", {"event_type": "Test"})

    assert socket_1.sent == [{"event_type": "Test"}]
    assert socket_2.sent == [{"event_type": "Test"}]


@pytest.mark.anyio
async def test_broadcast_to_an_account_with_no_connections_is_a_no_op():
    manager = ConnectionManager()
    await manager.broadcast_to_account("nobody_connected", {"event_type": "Test"})
    # must not raise


@pytest.mark.anyio
async def test_a_dead_socket_is_dropped_and_does_not_block_other_sockets():
    manager = ConnectionManager()
    dead_socket = _FakeSocket(fail=True)
    live_socket = _FakeSocket()
    manager.connect("account_a", dead_socket)  # type: ignore[arg-type]
    manager.connect("account_a", live_socket)  # type: ignore[arg-type]

    await manager.broadcast_to_account("account_a", {"event_type": "Test"})

    assert live_socket.sent == [{"event_type": "Test"}]
    assert manager.connection_count("account_a") == 1  # dead one was dropped


def test_disconnect_removes_the_connection():
    manager = ConnectionManager()
    socket = _FakeSocket()
    manager.connect("account_a", socket)  # type: ignore[arg-type]
    assert manager.connection_count("account_a") == 1

    manager.disconnect("account_a", socket)  # type: ignore[arg-type]

    assert manager.connection_count("account_a") == 0


def test_disconnect_of_an_unknown_socket_is_a_no_op():
    manager = ConnectionManager()
    socket = _FakeSocket()
    manager.disconnect("account_a", socket)  # type: ignore[arg-type]
    # must not raise
