"""
A real local WebSocket server (via the `websockets` library), not any
real exchange — spec section 5's explicit requirement for
test_websocket_connection.py. Runs in its own background thread with
its own asyncio event loop so the synchronous test functions can drive
it without themselves becoming async.
"""

import asyncio
import threading

import pytest


class FakeWebSocketServer:
    def __init__(self, handler):
        self.handler = handler
        self.port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise TimeoutError("fake websocket server did not start in time")

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        import websockets

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _serve() -> None:
            server = await websockets.serve(self.handler, "localhost", 0)
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()

        self._loop.run_until_complete(_serve())
        self._loop.run_forever()


@pytest.fixture
def fake_server():
    servers: list[FakeWebSocketServer] = []

    def _make(handler) -> FakeWebSocketServer:
        server = FakeWebSocketServer(handler)
        server.start()
        servers.append(server)
        return server

    yield _make

    for server in servers:
        server.stop()
